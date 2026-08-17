"""Authenticated remote execution boundary for Proof Runner workloads.

The API process sends source files and a command to a dedicated runner over a
small, versioned protocol.  Both request and response bodies are bound to the
same input digest and authenticated with HMAC-SHA256.  The runner still relies
on :class:`RepairVerifier` for the final container boundary; it never executes
pull-request code in the HTTP server process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .ports import ProofArtifactStorePort, ProofExecutorPort, ProofReplayStorePort
from .proof import LocalProofExecutor
from .proof_artifacts import (
    ContentAddressedArtifactStore,
    S3ObjectLockArtifactStore,
    validate_s3_object_lock_settings,
)
from .verifier import RepairVerifier

PROTOCOL_VERSION = 1
EXECUTE_PATH = "/v1/execute"
HEADER_REQUEST_ID = "X-EvoAgent-Request-Id"
HEADER_ISSUED_AT = "X-EvoAgent-Issued-At"
HEADER_BODY_SHA256 = "X-EvoAgent-Body-SHA256"
HEADER_SIGNATURE = "X-EvoAgent-Signature"
HEADER_INPUT_SHA256 = "X-EvoAgent-Input-SHA256"
HEADER_KEY_ID = "X-EvoAgent-Key-Id"
_ALLOWED_OUTCOMES = frozenset({"passed", "failed", "timeout", "error", "skipped"})
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProofProtocolError(RuntimeError):
    """A remote proof request or attestation violated the protocol contract."""


class ProofRunnerUnavailableError(ProofProtocolError):
    """The request was authentic but runner infrastructure could not serve it."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sign(
    secret: bytes,
    kind: str,
    request_id: str,
    issued_at: str,
    body_sha256: str,
    input_sha256: str,
) -> str:
    signed = "\n".join(
        (
            "evoagent-proof-v1",
            kind,
            request_id,
            issued_at,
            body_sha256,
            input_sha256,
        )
    ).encode("utf-8")
    return "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()


def _validated_secret(value: str | bytes) -> bytes:
    secret = value.encode("utf-8") if isinstance(value, str) else value
    if len(secret) < 32:
        raise ValueError("proof runner signing key must contain at least 32 bytes")
    return secret


def _validated_key_id(value: str) -> str:
    key_id = str(value).strip()
    if not _KEY_ID.fullmatch(key_id):
        raise ValueError("proof runner signing key id is invalid")
    return key_id


def _header(headers: Mapping[str, str], name: str) -> str:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return str(value).strip()
    return ""


def _validate_hex_digest(value: str, field: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProofProtocolError("invalid %s" % field)
    return value


def _validate_request_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProofProtocolError("invalid proof request id") from exc
    if parsed.version != 4:
        raise ProofProtocolError("proof request id must be UUIDv4")
    return str(parsed)


def _validate_timestamp(value: str, now: int, max_age_seconds: int) -> str:
    try:
        issued_at = int(value)
    except (TypeError, ValueError) as exc:
        raise ProofProtocolError("invalid proof request timestamp") from exc
    if abs(now - issued_at) > max_age_seconds:
        raise ProofProtocolError("proof request timestamp is outside the replay window")
    return str(issued_at)


def _input_document(files: dict[str, str], command: str) -> dict[str, Any]:
    return {"command": command, "files": files}


class InMemoryProofReplayStore:
    """Bounded single-process replay adapter retained for local development."""

    backend = "memory"

    def __init__(
        self,
        max_entries: int = 100_000,
        clock: Any = time.time,
    ):
        if max_entries <= 0:
            raise ValueError("proof replay entry limit must be positive")
        self.max_entries = max_entries
        self._clock = clock
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def claim(self, request_id: str, expires_at: int) -> bool:
        now = int(self._clock())
        with self._lock:
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            if request_id in self._seen:
                return False
            if len(self._seen) >= self.max_entries:
                raise ProofRunnerUnavailableError("proof replay guard capacity is exhausted")
            self._seen[request_id] = max(now + 1, int(expires_at))
        return True

    def health(self) -> bool:
        return True

    def close(self) -> None:
        return None


class RedisProofReplayStore:
    """Cross-replica nonce adapter using Redis atomic SET NX with expiry."""

    backend = "redis"

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "evoagent:proof-replay:v1",
        client: Any | None = None,
        clock: Any = time.time,
    ):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("proof replay Redis URL must use redis:// or rediss://")
        if (
            not prefix
            or len(prefix) > 160
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:._-"
                for character in prefix
            )
        ):
            raise ValueError("proof replay Redis prefix is invalid")
        if client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - declared runtime dependency
                raise RuntimeError("Redis proof replay mode requires the redis package") from exc
            client = redis.Redis.from_url(
                url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
                decode_responses=True,
            )
        self._client = client
        self._prefix = prefix
        self._clock = clock

    def claim(self, request_id: str, expires_at: int) -> bool:
        ttl = max(1, int(expires_at) - int(self._clock()))
        try:
            claimed = self._client.set(
                "%s:%s" % (self._prefix, request_id),
                "1",
                nx=True,
                ex=ttl,
            )
        except Exception as exc:
            raise ProofRunnerUnavailableError("proof replay store is unavailable") from exc
        return bool(claimed)

    def health(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteProofExecutor:
    """Proof executor adapter used by the API/application process."""

    def __init__(
        self,
        endpoint: str,
        signing_key: str | bytes,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: int = 150,
        max_request_bytes: int = 10 * 1024 * 1024,
        max_response_bytes: int = 128 * 1024,
        replay_window_seconds: int = 300,
        signing_key_id: str = "default",
        opener: Any | None = None,
        clock: Any = time.time,
    ):
        self.endpoint = self._validate_endpoint(endpoint, allowed_hosts)
        self._secret = _validated_secret(signing_key)
        self.signing_key_id = _validated_key_id(signing_key_id)
        self.timeout_seconds = timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.replay_window_seconds = replay_window_seconds
        if max_request_bytes < 1024 or max_response_bytes < 1024:
            raise ValueError("proof runner byte limits must be at least 1024")
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        self._clock = clock

    @staticmethod
    def _validate_endpoint(endpoint: str, allowed_hosts: tuple[str, ...]) -> str:
        parsed = urllib.parse.urlsplit(endpoint)
        host = (parsed.hostname or "").lower()
        allowlist = {item.strip().lower().rstrip(".") for item in allowed_hosts if item.strip()}
        if not host or host.rstrip(".") not in allowlist:
            raise ValueError("proof runner endpoint host is not in the exact allowlist")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "proof runner endpoint must not contain credentials, query, or fragment"
            )
        if parsed.scheme == "http" and host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("remote proof runner requires HTTPS outside loopback")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("proof runner endpoint must use HTTPS (or HTTP on loopback)")
        path = parsed.path.rstrip("/")
        if path and path != EXECUTE_PATH:
            raise ValueError("proof runner endpoint path must be %s" % EXECUTE_PATH)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, EXECUTE_PATH, "", ""))

    def execute(self, files: dict[str, str], command: str) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        issued_at = str(int(self._clock()))
        input_bytes = canonical_json(_input_document(files, command))
        input_sha256 = sha256_hex(input_bytes)
        document = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "issued_at": int(issued_at),
            "key_id": self.signing_key_id,
            "input_sha256": input_sha256,
            "files": files,
            "command": command,
        }
        body = canonical_json(document)
        if len(body) > self.max_request_bytes:
            raise ProofProtocolError("proof request exceeds the configured byte limit")
        body_sha256 = sha256_hex(body)
        signature = _sign(
            self._secret,
            "request",
            request_id,
            issued_at,
            body_sha256,
            input_sha256,
        )
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                HEADER_REQUEST_ID: request_id,
                HEADER_ISSUED_AT: issued_at,
                HEADER_BODY_SHA256: body_sha256,
                HEADER_INPUT_SHA256: input_sha256,
                HEADER_KEY_ID: self.signing_key_id,
                HEADER_SIGNATURE: signature,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                response_body = self._read_bounded(response)
                response_headers = dict(response.headers.items())
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            message = "rejected the request" if 400 <= exc.code < 500 else "is unavailable"
            raise ProofProtocolError("proof runner %s (HTTP %d)" % (message, exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProofProtocolError("proof runner transport failed") from exc
        if status != 200:
            raise ProofProtocolError("proof runner returned HTTP %d" % status)
        return self._verify_response(
            response_body,
            response_headers,
            request_id,
            input_sha256,
        )

    def health(self) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.endpoint)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/readyz", "", ""))
        request = urllib.request.Request(url, method="GET")
        try:
            with self._opener.open(request, timeout=min(self.timeout_seconds, 3)) as response:
                status = int(getattr(response, "status", 200))
                body = response.read(1025)
            document = json.loads(body) if len(body) <= 1024 else {}
            healthy = status == 200 and document.get("status") == "ready"
        except Exception:
            healthy = False
            document = {}
        return {
            "healthy": healthy,
            "mode": "remote",
            "endpoint_host": parsed.hostname or "",
            "replay_backend": str(document.get("replay_backend", "unknown")),
            "artifact_backend": str(document.get("artifact_backend", "unknown")),
            "artifact_ready": bool(document.get("artifact_ready", False)),
            "signing_key_id": self.signing_key_id,
        }

    def _read_bounded(self, response: Any) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > self.max_response_bytes:
                    raise ProofProtocolError("proof runner response exceeds the byte limit")
            except ValueError as exc:
                raise ProofProtocolError("proof runner returned an invalid content length") from exc
        body = response.read(self.max_response_bytes + 1)
        if len(body) > self.max_response_bytes:
            raise ProofProtocolError("proof runner response exceeds the byte limit")
        return body

    def _verify_response(
        self,
        body: bytes,
        headers: Mapping[str, str],
        request_id: str,
        input_sha256: str,
    ) -> dict[str, Any]:
        response_id = _validate_request_id(_header(headers, HEADER_REQUEST_ID))
        if not hmac.compare_digest(response_id, request_id):
            raise ProofProtocolError("proof runner response request id mismatch")
        issued_at = _validate_timestamp(
            _header(headers, HEADER_ISSUED_AT),
            int(self._clock()),
            self.replay_window_seconds,
        )
        claimed_body_sha = _validate_hex_digest(
            _header(headers, HEADER_BODY_SHA256), "response body digest"
        )
        actual_body_sha = sha256_hex(body)
        if not hmac.compare_digest(claimed_body_sha, actual_body_sha):
            raise ProofProtocolError("proof runner response body digest mismatch")
        claimed_input_sha = _validate_hex_digest(
            _header(headers, HEADER_INPUT_SHA256), "response input digest"
        )
        if not hmac.compare_digest(claimed_input_sha, input_sha256):
            raise ProofProtocolError("proof runner response input digest mismatch")
        response_key_id = _header(headers, HEADER_KEY_ID) or "default"
        try:
            response_key_id = _validated_key_id(response_key_id)
        except ValueError as exc:
            raise ProofProtocolError("proof runner response key id is invalid") from exc
        if not hmac.compare_digest(response_key_id, self.signing_key_id):
            raise ProofProtocolError("proof runner response key id mismatch")
        expected = _sign(
            self._secret,
            "response",
            request_id,
            issued_at,
            claimed_body_sha,
            input_sha256,
        )
        if not hmac.compare_digest(_header(headers, HEADER_SIGNATURE), expected):
            raise ProofProtocolError("proof runner response signature is invalid")
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProofProtocolError("proof runner returned invalid JSON") from exc
        if not isinstance(document, dict) or document.get("version") != PROTOCOL_VERSION:
            raise ProofProtocolError("unsupported proof runner response version")
        if document.get("request_id") != request_id:
            raise ProofProtocolError("proof runner response body request id mismatch")
        if document.get("input_sha256") != input_sha256:
            raise ProofProtocolError("proof runner response body input digest mismatch")
        document_key_id = document.get("key_id", "default")
        if document_key_id != self.signing_key_id:
            raise ProofProtocolError("proof runner response body key id mismatch")
        outcome = document.get("outcome")
        if not isinstance(outcome, dict):
            raise ProofProtocolError("proof runner response has no outcome")
        evidence_sha = _validate_hex_digest(
            str(document.get("evidence_sha256", "")), "evidence digest"
        )
        if not hmac.compare_digest(evidence_sha, sha256_hex(canonical_json(outcome))):
            raise ProofProtocolError("proof runner evidence digest mismatch")
        _validate_outcome(outcome)
        artifacts = document.get("artifacts") or {}
        if not isinstance(artifacts, dict) or set(artifacts) - {"input", "evidence"}:
            raise ProofProtocolError("proof runner returned invalid artifact references")
        expected_artifacts = {
            "input": "sha256:" + input_sha256,
            "evidence": "sha256:" + evidence_sha,
        }
        for name, reference in artifacts.items():
            if reference != expected_artifacts[name]:
                raise ProofProtocolError("proof runner artifact digest mismatch")
        verified = dict(outcome)
        verified["attestation"] = {
            "request_id": request_id,
            "key_id": self.signing_key_id,
            "input_sha256": input_sha256,
            "evidence_sha256": evidence_sha,
            "artifacts": artifacts,
        }
        return verified


def _validate_outcome(outcome: dict[str, Any]) -> None:
    status = outcome.get("status")
    if status not in _ALLOWED_OUTCOMES:
        raise ProofProtocolError("proof runner returned an invalid outcome status")
    if not isinstance(outcome.get("passed"), bool):
        raise ProofProtocolError("proof runner returned an invalid passed flag")
    if outcome["passed"] != (status == "passed"):
        raise ProofProtocolError("proof runner returned an inconsistent outcome")
    if not isinstance(outcome.get("checks", []), list):
        raise ProofProtocolError("proof runner returned invalid checks")
    duration = outcome.get("duration_seconds", 0.0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise ProofProtocolError("proof runner returned an invalid duration")
    for check in outcome.get("checks", []):
        if not isinstance(check, dict):
            raise ProofProtocolError("proof runner returned an invalid check")
        if "detail" in check and not isinstance(check["detail"], str):
            raise ProofProtocolError("proof runner returned an invalid check detail")


@dataclass(frozen=True)
class SignedProofResponse:
    body: bytes
    headers: dict[str, str]


class ProofRunnerServer:
    """Protocol service hosted in the isolated runner deployment."""

    def __init__(
        self,
        executor: ProofExecutorPort,
        signing_key: str | bytes,
        *,
        signing_key_id: str = "default",
        verification_keys: Mapping[str, str | bytes] | None = None,
        max_request_bytes: int = 10 * 1024 * 1024,
        max_response_bytes: int = 128 * 1024,
        replay_window_seconds: int = 300,
        max_replay_entries: int = 100_000,
        max_files: int = 5000,
        max_source_bytes: int = 8 * 1024 * 1024,
        max_concurrency: int = 2,
        artifact_store: ProofArtifactStorePort | None = None,
        require_artifacts: bool = False,
        replay_store: ProofReplayStorePort | None = None,
        clock: Any = time.time,
    ):
        self.executor = executor
        self.signing_key_id = _validated_key_id(signing_key_id)
        self._signing_keys = {self.signing_key_id: _validated_secret(signing_key)}
        additional_keys = verification_keys or {}
        if len(additional_keys) > 1:
            raise ValueError("proof runner accepts at most one previous signing key")
        for key_id, secret in additional_keys.items():
            validated_id = _validated_key_id(key_id)
            if validated_id in self._signing_keys:
                raise ValueError("proof runner signing key ids must be unique")
            self._signing_keys[validated_id] = _validated_secret(secret)
        if max_request_bytes < 1024 or max_response_bytes < 1024:
            raise ValueError("proof runner byte limits must be at least 1024")
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.replay_window_seconds = replay_window_seconds
        self.max_replay_entries = max_replay_entries
        self.max_files = max_files
        self.max_source_bytes = max_source_bytes
        self.artifact_store = artifact_store
        self.require_artifacts = require_artifacts
        if require_artifacts and artifact_store is None:
            raise ValueError("required proof artifact storage is not configured")
        self._clock = clock
        self.replay_store = replay_store or InMemoryProofReplayStore(
            max_replay_entries,
            clock,
        )
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def execute(self, body: bytes, headers: Mapping[str, str]) -> SignedProofResponse:
        if len(body) > self.max_request_bytes:
            raise ProofProtocolError("proof request exceeds the configured byte limit")
        request_id = _validate_request_id(_header(headers, HEADER_REQUEST_ID))
        now = int(self._clock())
        issued_at = _validate_timestamp(
            _header(headers, HEADER_ISSUED_AT), now, self.replay_window_seconds
        )
        body_sha = _validate_hex_digest(_header(headers, HEADER_BODY_SHA256), "request body digest")
        input_sha = _validate_hex_digest(
            _header(headers, HEADER_INPUT_SHA256), "request input digest"
        )
        if not hmac.compare_digest(body_sha, sha256_hex(body)):
            raise ProofProtocolError("proof request body digest mismatch")
        provided_key_id = _header(headers, HEADER_KEY_ID)
        try:
            key_id = _validated_key_id(provided_key_id or "default")
        except ValueError as exc:
            raise ProofProtocolError("proof request signature is invalid") from exc
        secret = self._signing_keys.get(key_id)
        if secret is None:
            raise ProofProtocolError("proof request signature is invalid")
        expected = _sign(
            secret,
            "request",
            request_id,
            issued_at,
            body_sha,
            input_sha,
        )
        if not hmac.compare_digest(_header(headers, HEADER_SIGNATURE), expected):
            raise ProofProtocolError("proof request signature is invalid")
        self._claim_request(request_id, int(issued_at), now)
        document, files, command = self._validate_document(
            body,
            request_id,
            issued_at,
            input_sha,
            key_id,
            allow_legacy_key_id=not provided_key_id,
        )
        input_bytes = canonical_json(_input_document(files, command))
        input_artifact = self._store_artifact("inputs", input_bytes)
        if not self._slots.acquire(blocking=False):
            outcome = _error_outcome("proof runner capacity is exhausted")
        else:
            try:
                try:
                    outcome = self.executor.execute(files, command)
                    _validate_outcome(outcome)
                    canonical_json(outcome)
                except Exception:
                    outcome = _error_outcome("proof execution failed inside the runner")
            finally:
                self._slots.release()
        evidence_bytes = canonical_json(outcome)
        evidence_artifact = self._store_artifact("evidence", evidence_bytes)
        artifacts = {
            key: value
            for key, value in {"input": input_artifact, "evidence": evidence_artifact}.items()
            if value
        }
        response_document = {
            "version": PROTOCOL_VERSION,
            "request_id": document["request_id"],
            "key_id": key_id,
            "input_sha256": input_sha,
            "evidence_sha256": sha256_hex(evidence_bytes),
            "outcome": outcome,
            "artifacts": artifacts,
        }
        return self._signed_response(response_document, request_id, input_sha, key_id, secret)

    def _validate_document(
        self,
        body: bytes,
        request_id: str,
        issued_at: str,
        input_sha: str,
        key_id: str,
        *,
        allow_legacy_key_id: bool,
    ) -> tuple[dict[str, Any], dict[str, str], str]:
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProofProtocolError("proof request body is invalid JSON") from exc
        if not isinstance(document, dict) or document.get("version") != PROTOCOL_VERSION:
            raise ProofProtocolError("unsupported proof request version")
        if document.get("request_id") != request_id or str(document.get("issued_at")) != issued_at:
            raise ProofProtocolError("proof request metadata mismatch")
        document_key_id = document.get("key_id")
        if document_key_id is None and allow_legacy_key_id:
            document_key_id = "default"
        if document_key_id != key_id:
            raise ProofProtocolError("proof request key id mismatch")
        if document.get("input_sha256") != input_sha:
            raise ProofProtocolError("proof request input digest mismatch")
        files = document.get("files")
        command = document.get("command")
        if not isinstance(files, dict) or not isinstance(command, str) or not command.strip():
            raise ProofProtocolError("proof request requires files and a command")
        if len(files) > self.max_files:
            raise ProofProtocolError("proof request contains too many files")
        typed_files: dict[str, str] = {}
        source_bytes = 0
        for path, content in files.items():
            if not isinstance(path, str) or not isinstance(content, str):
                raise ProofProtocolError("proof files must map string paths to string contents")
            source_bytes += len(path.encode("utf-8")) + len(content.encode("utf-8"))
            typed_files[path] = content
        if source_bytes > self.max_source_bytes:
            raise ProofProtocolError("proof source exceeds the configured byte limit")
        actual_input_sha = sha256_hex(canonical_json(_input_document(typed_files, command)))
        if not hmac.compare_digest(actual_input_sha, input_sha):
            raise ProofProtocolError("proof request canonical input digest mismatch")
        return document, typed_files, command

    def _claim_request(self, request_id: str, issued_at: int, now: int) -> None:
        expires_at = max(now + 1, issued_at + self.replay_window_seconds + 1)
        try:
            claimed = self.replay_store.claim(request_id, expires_at)
        except ProofRunnerUnavailableError:
            raise
        except Exception as exc:
            raise ProofRunnerUnavailableError("proof replay store is unavailable") from exc
        if not claimed:
            raise ProofProtocolError("proof request replay detected")

    def _store_artifact(self, namespace: str, content: bytes) -> str:
        if self.artifact_store is None:
            return ""
        try:
            return self.artifact_store.put(namespace, content)
        except Exception as exc:
            if self.require_artifacts:
                raise ProofRunnerUnavailableError(
                    "required proof artifact could not be persisted"
                ) from exc
            return ""

    def _signed_response(
        self,
        document: dict[str, Any],
        request_id: str,
        input_sha: str,
        key_id: str,
        secret: bytes,
    ) -> SignedProofResponse:
        body = canonical_json(document)
        if len(body) > self.max_response_bytes:
            outcome = _error_outcome("proof evidence exceeds the configured response byte limit")
            document = {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "key_id": key_id,
                "input_sha256": input_sha,
                "evidence_sha256": sha256_hex(canonical_json(outcome)),
                "outcome": outcome,
                "artifacts": {},
            }
            body = canonical_json(document)
        issued_at = str(int(self._clock()))
        body_sha = sha256_hex(body)
        return SignedProofResponse(
            body,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                HEADER_REQUEST_ID: request_id,
                HEADER_ISSUED_AT: issued_at,
                HEADER_BODY_SHA256: body_sha,
                HEADER_INPUT_SHA256: input_sha,
                HEADER_KEY_ID: key_id,
                HEADER_SIGNATURE: _sign(
                    secret,
                    "response",
                    request_id,
                    issued_at,
                    body_sha,
                    input_sha,
                ),
            },
        )

    def readiness(self) -> dict[str, Any]:
        try:
            replay_ready = bool(self.replay_store.health())
        except Exception:
            replay_ready = False
        artifact_backend = "none"
        artifact_ready = self.artifact_store is None
        if self.artifact_store is not None:
            artifact_backend = self.artifact_store.backend
            try:
                artifact_ready = bool(self.artifact_store.health())
            except Exception:
                artifact_ready = False
        ready = replay_ready and (artifact_ready or not self.require_artifacts)
        return {
            "status": "ready" if ready else "not-ready",
            "replay_backend": self.replay_store.backend,
            "artifact_backend": artifact_backend,
            "artifact_ready": artifact_ready,
            "signing_key_ids": len(self._signing_keys),
        }

    def close(self) -> None:
        try:
            self.replay_store.close()
        finally:
            if self.artifact_store is not None:
                self.artifact_store.close()


def _error_outcome(detail: str) -> dict[str, Any]:
    return {
        "passed": False,
        "status": "error",
        "checks": [{"name": "proof-runner", "passed": False, "detail": detail}],
        "duration_seconds": 0.0,
    }


@dataclass(frozen=True)
class ProofRunnerSettings:
    host: str
    port: int
    signing_key: str = field(repr=False)
    container_image: str
    signing_key_id: str = "default"
    previous_signing_key_id: str = ""
    previous_signing_key: str = field(default="", repr=False)
    replay_redis_url: str = field(default="", repr=False)
    require_shared_replay: bool = False
    timeout_seconds: int = 120
    memory_mb: int = 1024
    pids_limit: int = 256
    cpus: float = 1.0
    max_output_bytes: int = 16000
    max_request_bytes: int = 10 * 1024 * 1024
    max_response_bytes: int = 128 * 1024
    max_source_bytes: int = 8 * 1024 * 1024
    max_files: int = 5000
    replay_window_seconds: int = 300
    max_replay_entries: int = 100_000
    max_concurrency: int = 2
    max_connections: int = 32
    artifact_dir: str = ""
    artifact_s3_bucket: str = ""
    artifact_s3_prefix: str = "evoagent/proof-artifacts/v1"
    artifact_s3_region: str = ""
    artifact_s3_endpoint_url: str = field(default="", repr=False)
    artifact_s3_retention_mode: str = "COMPLIANCE"
    artifact_s3_retention_days: int = 2555
    artifact_s3_kms_key_id: str = field(default="", repr=False)
    require_artifacts: bool = False
    tls_cert_file: str = ""
    tls_key_file: str = ""

    @classmethod
    def from_env(cls) -> ProofRunnerSettings:
        prefix = "EVOAGENT_PROOF_RUNNER_"

        def integer(name: str, default: int) -> int:
            value = int(os.getenv(prefix + name, str(default)))
            if value <= 0:
                raise ValueError("%s%s must be positive" % (prefix, name))
            return value

        settings = cls(
            host=os.getenv(prefix + "HOST", "127.0.0.1"),
            port=integer("PORT", 8091),
            signing_key=os.getenv(prefix + "SIGNING_KEY", ""),
            container_image=os.getenv(prefix + "CONTAINER_IMAGE", ""),
            signing_key_id=os.getenv(prefix + "SIGNING_KEY_ID", "default"),
            previous_signing_key_id=os.getenv(prefix + "PREVIOUS_SIGNING_KEY_ID", ""),
            previous_signing_key=os.getenv(prefix + "PREVIOUS_SIGNING_KEY", ""),
            replay_redis_url=os.getenv(prefix + "REPLAY_REDIS_URL", ""),
            require_shared_replay=os.getenv(prefix + "REQUIRE_SHARED_REPLAY", "false").lower()
            in {"1", "true", "yes", "on"},
            timeout_seconds=integer("TIMEOUT_SECONDS", 120),
            memory_mb=integer("MEMORY_MB", 1024),
            pids_limit=integer("PIDS_LIMIT", 256),
            cpus=float(os.getenv(prefix + "CPUS", "1.0")),
            max_output_bytes=integer("MAX_OUTPUT_BYTES", 16000),
            max_request_bytes=integer("MAX_REQUEST_BYTES", 10 * 1024 * 1024),
            max_response_bytes=integer("MAX_RESPONSE_BYTES", 128 * 1024),
            max_source_bytes=integer("MAX_SOURCE_BYTES", 8 * 1024 * 1024),
            max_files=integer("MAX_FILES", 5000),
            replay_window_seconds=integer("REPLAY_WINDOW_SECONDS", 300),
            max_replay_entries=integer("MAX_REPLAY_ENTRIES", 100_000),
            max_concurrency=integer("MAX_CONCURRENCY", 2),
            max_connections=integer("MAX_CONNECTIONS", 32),
            artifact_dir=os.getenv(prefix + "ARTIFACT_DIR", ""),
            artifact_s3_bucket=os.getenv(prefix + "ARTIFACT_S3_BUCKET", ""),
            artifact_s3_prefix=os.getenv(
                prefix + "ARTIFACT_S3_PREFIX", "evoagent/proof-artifacts/v1"
            ),
            artifact_s3_region=os.getenv(prefix + "ARTIFACT_S3_REGION", ""),
            artifact_s3_endpoint_url=os.getenv(prefix + "ARTIFACT_S3_ENDPOINT_URL", ""),
            artifact_s3_retention_mode=os.getenv(
                prefix + "ARTIFACT_S3_RETENTION_MODE", "COMPLIANCE"
            ),
            artifact_s3_retention_days=integer("ARTIFACT_S3_RETENTION_DAYS", 2555),
            artifact_s3_kms_key_id=os.getenv(prefix + "ARTIFACT_S3_KMS_KEY_ID", ""),
            require_artifacts=os.getenv(prefix + "REQUIRE_ARTIFACTS", "false").lower()
            in {"1", "true", "yes", "on"},
            tls_cert_file=os.getenv(prefix + "TLS_CERT_FILE", ""),
            tls_key_file=os.getenv(prefix + "TLS_KEY_FILE", ""),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        _validated_secret(self.signing_key)
        _validated_key_id(self.signing_key_id)
        if bool(self.previous_signing_key_id) != bool(self.previous_signing_key):
            raise ValueError(
                "proof runner previous signing key id and key must be configured together"
            )
        if self.previous_signing_key_id:
            _validated_key_id(self.previous_signing_key_id)
            _validated_secret(self.previous_signing_key)
            if self.previous_signing_key_id == self.signing_key_id:
                raise ValueError("proof runner current and previous signing key ids must differ")
        if self.require_shared_replay and not self.replay_redis_url:
            raise ValueError(
                "EVOAGENT_PROOF_RUNNER_REPLAY_REDIS_URL is required when shared replay is mandatory"
            )
        if not self.container_image:
            raise ValueError("EVOAGENT_PROOF_RUNNER_CONTAINER_IMAGE is required")
        if self.cpus <= 0:
            raise ValueError("EVOAGENT_PROOF_RUNNER_CPUS must be positive")
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("proof runner TLS certificate and key must be configured together")
        if self.host not in {"127.0.0.1", "::1", "localhost"} and not self.tls_cert_file:
            raise ValueError("proof runner TLS is required when binding outside loopback")
        if self.artifact_dir and self.artifact_s3_bucket:
            raise ValueError(
                "proof runner filesystem and S3 artifact stores are mutually exclusive"
            )
        if self.artifact_s3_bucket:
            if not self.artifact_s3_region:
                raise ValueError("EVOAGENT_PROOF_RUNNER_ARTIFACT_S3_REGION is required")
            validate_s3_object_lock_settings(
                self.artifact_s3_bucket,
                self.artifact_s3_prefix,
                self.artifact_s3_endpoint_url,
                self.artifact_s3_retention_mode,
                self.artifact_s3_retention_days,
                region=self.artifact_s3_region,
                kms_key_id=self.artifact_s3_kms_key_id,
            )
        if self.require_artifacts and not (self.artifact_dir or self.artifact_s3_bucket):
            raise ValueError("required proof artifact storage is not configured")


def build_runner_service(settings: ProofRunnerSettings) -> ProofRunnerServer:
    executor = LocalProofExecutor(
        lambda command: RepairVerifier(
            command,
            settings.timeout_seconds,
            container_image=settings.container_image,
            memory_mb=settings.memory_mb,
            pids_limit=settings.pids_limit,
            cpus=settings.cpus,
            require_container=True,
            max_output_bytes=settings.max_output_bytes,
        )
    )
    artifacts: ProofArtifactStorePort | None
    if settings.artifact_s3_bucket:
        artifacts = S3ObjectLockArtifactStore(
            settings.artifact_s3_bucket,
            prefix=settings.artifact_s3_prefix,
            region=settings.artifact_s3_region,
            endpoint_url=settings.artifact_s3_endpoint_url,
            retention_mode=settings.artifact_s3_retention_mode,
            retention_days=settings.artifact_s3_retention_days,
            kms_key_id=settings.artifact_s3_kms_key_id,
        )
    elif settings.artifact_dir:
        artifacts = ContentAddressedArtifactStore(settings.artifact_dir)
    else:
        artifacts = None
    replay_store: ProofReplayStorePort | None = None
    try:
        replay_store = (
            RedisProofReplayStore(settings.replay_redis_url)
            if settings.replay_redis_url
            else InMemoryProofReplayStore(settings.max_replay_entries)
        )
        verification_keys = (
            {settings.previous_signing_key_id: settings.previous_signing_key}
            if settings.previous_signing_key_id
            else {}
        )
        return ProofRunnerServer(
            executor,
            settings.signing_key,
            signing_key_id=settings.signing_key_id,
            verification_keys=verification_keys,
            max_request_bytes=settings.max_request_bytes,
            max_response_bytes=settings.max_response_bytes,
            replay_window_seconds=settings.replay_window_seconds,
            max_replay_entries=settings.max_replay_entries,
            max_files=settings.max_files,
            max_source_bytes=settings.max_source_bytes,
            max_concurrency=settings.max_concurrency,
            artifact_store=artifacts,
            require_artifacts=settings.require_artifacts,
            replay_store=replay_store,
        )
    except Exception:
        for resource in (replay_store, artifacts):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        raise


def _handler(service: ProofRunnerServer, max_request_bytes: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EvoAgentProofRunner/1"
        sys_version = ""

        def do_GET(self) -> None:
            if self.path == "/healthz":
                document = {"status": "ok"}
                status = 200
            elif self.path == "/readyz":
                document = service.readiness()
                status = 200 if document["status"] == "ready" else 503
            else:
                self.send_error(404)
                return
            payload = canonical_json(document)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            if self.path != EXECUTE_PATH:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(411)
                return
            if length <= 0 or length > max_request_bytes:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            try:
                response = service.execute(body, dict(self.headers.items()))
            except ProofRunnerUnavailableError:
                self.send_error(503, "proof runner unavailable")
                return
            except ProofProtocolError:
                self.send_error(401, "proof request rejected")
                return
            self.send_response(200)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: Any) -> None:
            # Deliberately excludes request headers and bodies, which can contain
            # source code and authentication material.
            print("proof-runner: %s - %s" % (self.address_string(), format % args))

    return Handler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound connection threads so sockets cannot grow runner memory unbounded."""

    daemon_threads = False

    def __init__(self, server_address: Any, handler: Any, max_connections: int):
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def _tls_server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


def run() -> None:
    settings = ProofRunnerSettings.from_env()
    service = build_runner_service(settings)
    server = BoundedThreadingHTTPServer(
        (settings.host, settings.port),
        _handler(service, settings.max_request_bytes),
        settings.max_connections,
    )
    if settings.tls_cert_file:
        context = _tls_server_context(settings.tls_cert_file, settings.tls_key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    previous_handlers = {}

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signal_number] = signal.signal(signal_number, request_shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)


if __name__ == "__main__":
    run()
