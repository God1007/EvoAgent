"""Immutable content-addressed storage adapters for Proof Runner evidence."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

_NAMESPACES = frozenset({"inputs", "evidence"})
_RETENTION_MODES = frozenset({"COMPLIANCE", "GOVERNANCE"})
_PRECONDITION_FAILED = frozenset({"412", "PreconditionFailed"})
_CONCURRENT_CONFLICT = frozenset({"409", "ConditionalRequestConflict"})


class ProofArtifactStoreError(RuntimeError):
    """An artifact could not be persisted with the required integrity policy."""


def validate_s3_object_lock_settings(
    bucket: str,
    prefix: str,
    endpoint_url: str,
    retention_mode: str,
    retention_days: int,
    *,
    region: str = "",
    kms_key_id: str = "",
) -> tuple[str, str]:
    """Validate deployment-controlled S3 settings without creating a client."""
    if (
        not bucket
        or len(bucket) > 255
        or any(ord(character) < 33 or ord(character) > 126 for character in bucket)
    ):
        raise ValueError("proof artifact S3 bucket is invalid")
    normalized_prefix = prefix.strip("/")
    if (
        not normalized_prefix
        or len(normalized_prefix) > 512
        or any(ord(character) < 33 or ord(character) > 126 for character in normalized_prefix)
    ):
        raise ValueError("proof artifact S3 prefix is invalid")
    mode = retention_mode.strip().upper()
    if mode not in _RETENTION_MODES:
        raise ValueError("proof artifact retention mode must be COMPLIANCE or GOVERNANCE")
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or not 1 <= retention_days <= 36_500
    ):
        raise ValueError("proof artifact retention days must be between 1 and 36500")
    if region and (
        len(region) > 100
        or any(not (character.isalnum() or character == "-") for character in region)
    ):
        raise ValueError("proof artifact S3 region is invalid")
    if kms_key_id and (
        len(kms_key_id) > 2048
        or any(ord(character) < 33 or ord(character) > 126 for character in kms_key_id)
    ):
        raise ValueError("proof artifact S3 KMS key id is invalid")
    if endpoint_url:
        parsed = urllib.parse.urlsplit(endpoint_url)
        loopback = (parsed.hostname or "").lower() in {"127.0.0.1", "::1", "localhost"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        ):
            raise ValueError("proof artifact S3 endpoint must be credential-free HTTPS")
    return normalized_prefix, mode


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_path(namespace: str, digest: str) -> tuple[str, str]:
    if namespace not in _NAMESPACES:
        raise ValueError("proof artifact namespace is invalid")
    return namespace, "%s/%s.json" % (digest[:2], digest)


class ContentAddressedArtifactStore:
    """Append-only local adapter for single-node deployments and development."""

    backend = "filesystem"

    def __init__(self, root: str):
        if not root:
            raise ValueError("artifact root is required")
        self.root = os.path.abspath(root)
        os.makedirs(self.root, mode=0o700, exist_ok=True)

    def put(self, namespace: str, content: bytes) -> str:
        digest = _sha256_hex(content)
        namespace, suffix = _artifact_path(namespace, digest)
        directory = os.path.join(self.root, namespace, os.path.dirname(suffix))
        path = os.path.join(directory, os.path.basename(suffix))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.exists(path):
            if self._read_existing(path) != content:
                raise ProofArtifactStoreError("proof artifact digest collision detected")
            return "sha256:" + digest
        descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self._read_existing(path) != content:
                    raise ProofArtifactStoreError(
                        "proof artifact digest collision detected"
                    ) from None
            else:
                directory_descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)
        return "sha256:" + digest

    def health(self) -> bool:
        return os.path.isdir(self.root) and os.access(self.root, os.W_OK | os.X_OK)

    def close(self) -> None:
        return None

    @staticmethod
    def _read_existing(path: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as existing:
            return existing.read()


class S3ObjectLockArtifactStore:
    """S3 WORM adapter with checksum verification and explicit retention."""

    backend = "s3-object-lock"

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "evoagent/proof-artifacts/v1",
        region: str = "",
        endpoint_url: str = "",
        retention_mode: str = "COMPLIANCE",
        retention_days: int = 2555,
        kms_key_id: str = "",
        client: Any | None = None,
        clock: Any = datetime.now,
        sleeper: Any = time.sleep,
    ):
        normalized_prefix, mode = validate_s3_object_lock_settings(
            bucket,
            prefix,
            endpoint_url,
            retention_mode,
            retention_days,
            region=region,
            kms_key_id=kms_key_id,
        )
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - declared runtime dependency
                raise RuntimeError("S3 proof artifacts require the boto3 package") from exc
            client = boto3.client(
                "s3",
                region_name=region or None,
                endpoint_url=endpoint_url or None,
            )
        self.bucket = bucket
        self.prefix = normalized_prefix
        self.region = region
        self.retention_mode = mode
        self.retention_days = retention_days
        self.kms_key_id = kms_key_id
        self._client = client
        self._clock = clock
        self._sleeper = sleeper

    def put(self, namespace: str, content: bytes) -> str:
        digest = _sha256_hex(content)
        namespace, suffix = _artifact_path(namespace, digest)
        key = "%s/%s/%s" % (self.prefix, namespace, suffix)
        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        retain_until = self._now() + timedelta(days=self.retention_days)
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": content,
            "ContentType": "application/json",
            "Metadata": {"sha256": digest, "evoagent-protocol": "proof-v1"},
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "IfNoneMatch": "*",
            "ObjectLockMode": self.retention_mode,
            "ObjectLockRetainUntilDate": retain_until,
        }
        if self.kms_key_id:
            arguments.update(
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self.kms_key_id,
            )
        else:
            arguments["ServerSideEncryption"] = "AES256"
        response: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                response = dict(self._client.put_object(**arguments))
                break
            except Exception as exc:
                code = self._error_code(exc)
                if code in _PRECONDITION_FAILED:
                    self._verify_and_extend(key, digest, checksum, len(content), retain_until)
                    return "sha256:" + digest
                if code in _CONCURRENT_CONFLICT and attempt < 2:
                    self._sleeper(0.05 * (attempt + 1))
                    continue
                raise ProofArtifactStoreError("proof artifact object-lock write failed") from exc
        if response is None:  # pragma: no cover - bounded loop invariant
            raise ProofArtifactStoreError("proof artifact object-lock write failed")
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id:
            raise ProofArtifactStoreError("proof artifact object-lock version is missing")
        self._verify_and_extend(
            key,
            digest,
            checksum,
            len(content),
            retain_until,
            version_id=version_id,
        )
        return "sha256:" + digest

    def health(self) -> bool:
        try:
            lock = self._client.get_object_lock_configuration(Bucket=self.bucket)
            versioning = self._client.get_bucket_versioning(Bucket=self.bucket)
            configuration = lock.get("ObjectLockConfiguration") or {}
            return (
                isinstance(configuration, dict)
                and configuration.get("ObjectLockEnabled") == "Enabled"
                and versioning.get("Status") == "Enabled"
            )
        except Exception:
            return False

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _verify_and_extend(
        self,
        key: str,
        digest: str,
        checksum: str,
        size: int,
        retain_until: datetime,
        *,
        version_id: str = "",
    ) -> None:
        head = self._head(key, version_id)
        actual_version = head.get("VersionId")
        if not isinstance(actual_version, str) or not actual_version:
            raise ProofArtifactStoreError("proof artifact object-lock version is missing")
        metadata = head.get("Metadata") or {}
        actual_retention = head.get("ObjectLockRetainUntilDate")
        if (
            head.get("ContentLength") != size
            or not isinstance(metadata, dict)
            or metadata.get("sha256") != digest
            or head.get("ChecksumSHA256") != checksum
            or head.get("ObjectLockMode") != self.retention_mode
            or not isinstance(actual_retention, datetime)
        ):
            raise ProofArtifactStoreError("proof artifact object-lock verification failed")
        if self._as_utc(actual_retention) >= retain_until:
            return
        try:
            self._client.put_object_retention(
                Bucket=self.bucket,
                Key=key,
                VersionId=actual_version,
                Retention={
                    "Mode": self.retention_mode,
                    "RetainUntilDate": retain_until,
                },
            )
        except Exception as exc:
            concurrent = self._head(key, actual_version)
            concurrent_until = concurrent.get("ObjectLockRetainUntilDate")
            if (
                concurrent.get("ObjectLockMode") == self.retention_mode
                and isinstance(concurrent_until, datetime)
                and self._as_utc(concurrent_until) >= retain_until
            ):
                return
            raise ProofArtifactStoreError("proof artifact retention extension failed") from exc
        verified = self._head(key, actual_version)
        extended_until = verified.get("ObjectLockRetainUntilDate")
        if (
            verified.get("ObjectLockMode") != self.retention_mode
            or not isinstance(extended_until, datetime)
            or self._as_utc(extended_until) < retain_until
        ):
            raise ProofArtifactStoreError("proof artifact retention extension was not verified")

    def _head(self, key: str, version_id: str = "") -> dict[str, Any]:
        arguments = {
            "Bucket": self.bucket,
            "Key": key,
            "ChecksumMode": "ENABLED",
        }
        if version_id:
            arguments["VersionId"] = version_id
        try:
            return dict(self._client.head_object(**arguments))
        except Exception as exc:
            raise ProofArtifactStoreError("proof artifact object-lock verification failed") from exc

    def _now(self) -> datetime:
        try:
            value = self._clock(UTC)
        except TypeError:
            value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("proof artifact clock must return datetime")
        return self._as_utc(value).replace(microsecond=0)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return ""
        error = response.get("Error")
        return str(error.get("Code", "")) if isinstance(error, dict) else ""
