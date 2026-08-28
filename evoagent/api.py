import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .agents import WorkflowFactory
from .application.webhooks import github_pull_request_updated_at
from .auth import Principal
from .backpressure import ClientIdentity, ConcurrencyLimiter
from .config import Settings
from .console_view import console_error, console_response
from .errors import (
    AccessDeniedError,
    ClientInputError,
    ResourceNotFoundError,
    StateConflictError,
    TenantReviewCapacityError,
    safe_exception_fields,
)
from .github import verify_signature
from .harness import TaskCancelled
from .json_boundary import strict_json_loads
from .metrics import metrics
from .report import to_markdown
from .repository import canonical_repository
from .review_extensions import ReviewerContribution
from .service import ReviewService
from .studio import compile_workflow, document_id, draft_definition, revision_number

RESOURCE_ID = r"(?:[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})"
TASK = re.compile(r"^/v1/tasks/(%s)$" % RESOURCE_ID)
REPORT = re.compile(r"^/v1/tasks/(%s)/report$" % RESOURCE_ID)
WORKFLOW = re.compile(r"^/v1/tasks/(%s)/workflow$" % RESOURCE_ID)
WORKFLOW_ARTIFACT = re.compile(
    r"^/v1/tasks/(%s)/workflow/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})$" % RESOURCE_ID
)
STUDIO_DOCUMENT = re.compile(r"^/v1/studio/(agents|workflows)/([0-9a-f]{32})(/publish)?$")
STUDIO_VERSION = re.compile(
    r"^/v1/studio/(agents|workflows)/([0-9a-f]{32})/versions/([1-9][0-9]{0,8})$"
)
FIX = re.compile(r"^/v1/tasks/(%s)/fix$" % RESOURCE_ID)
FEEDBACK = re.compile(r"^/v1/tasks/(%s)/feedback$" % RESOURCE_ID)
CANCEL = re.compile(r"^/v1/tasks/(%s)/cancel$" % RESOURCE_ID)
RESUME = re.compile(r"^/v1/tasks/(%s)/resume$" % RESOURCE_ID)
SESSION = re.compile(r"^/v1/sessions/(%s)$" % RESOURCE_ID)
SESSION_INPUT = re.compile(r"^/v1/sessions/(%s)/input$" % RESOURCE_ID)
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# CPU/sandbox-heavy POST endpoints guarded by the bounded-concurrency gate.
HEAVY_PATHS = frozenset(
    {
        "/v1/auth/login",
        "/v1/auth/password",
        "/v1/users",
        "/v1/reviews",
        "/v1/proofs",
        "/v1/codegraph/impact",
        "/v1/evolution/auto",
        "/v1/evolution/propose",
    }
)
# Liveness/readiness are never rate-limited. Authenticated metrics pass the
# pre-auth limiter so invalid JWT floods cannot amplify into user-store reads.
PROBE_PATHS = frozenset({"/health", "/ready", "/metrics"})
SOURCE_WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
INSTALLED_WEB_ROOT = os.path.join(sys.prefix, "share", "evoagent", "web")
WEB_ROOT = SOURCE_WEB_ROOT if os.path.isdir(SOURCE_WEB_ROOT) else INSTALLED_WEB_ROOT
# Set on SIGTERM so /ready starts failing (load balancers drain us) while
# in-flight requests finish before the process exits.
DRAINING = threading.Event()


def _async_review_requested(query: dict[str, list[str]]) -> bool:
    if set(query).difference({"async"}):
        raise ClientInputError("review query accepts only async")
    values = query.get("async")
    if values is None:
        return False
    if len(values) != 1 or values[0].lower() not in {"true", "false"}:
        raise ClientInputError("async must be one true or false value")
    return values[0].lower() == "true"


def _request_target(value: str) -> urllib.parse.ParseResult:
    try:
        return urllib.parse.urlparse(value)
    except ValueError:
        raise ClientInputError("invalid request target") from None


def _query_parameters(value: str) -> dict[str, list[str]]:
    try:
        return urllib.parse.parse_qs(value, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise ClientInputError("query string has too many fields") from None


def _heavy_request(method: str, path: str) -> bool:
    return method == "POST" and (path in HEAVY_PATHS or FIX.fullmatch(path) is not None)


class ApiHandler(BaseHTTPRequestHandler):
    service: ReviewService
    settings: Settings
    _client_identity_cache: ClientIdentity | None = None
    server_version = "EvoAgent/%s" % __version__
    sys_version = ""

    def version_string(self) -> str:
        # Do not disclose the interpreter version at the public HTTP boundary.
        return self.server_version

    def send_response(self, code: int, message: str | None = None) -> None:
        if getattr(self, "_metric_counted", False) and not getattr(
            self, "_metric_response_recorded", False
        ):
            request_class = getattr(self, "_metric_class", "other")
            family = "%dxx" % (code // 100)
            metrics.inc("http_responses_total")
            metrics.inc("http_responses_%s_total" % family)
            metrics.inc("http_%s_responses_total" % request_class)
            metrics.inc("http_%s_responses_%s_total" % (request_class, family))
            if request_class != "probe":
                metrics.inc("http_nonprobe_responses_total")
                metrics.inc("http_nonprobe_responses_%s_total" % family)
            self._metric_response_recorded = True
        self._response_started = True
        super().send_response(code, message)

    def end_headers(self) -> None:
        # Apply the edge policy to every response, including redirects, overload
        # responses, and errors generated by BaseHTTPRequestHandler itself.
        # Keep a final sink-level CR/LF guard even though _request_id_value uses
        # an allowlist. Besides being defense in depth, the explicit replacement
        # makes the response-splitting invariant visible to static analyzers.
        request_id = self._request_id_value().replace("\n", "").replace("\r", "")
        self.send_header("X-Request-ID", request_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'",
        )
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        self._structured_log(
            "http_access",
            level="info",
            status=code,
            response_bytes=size,
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        # The base handler may interpolate attacker-controlled request targets or
        # exception messages. Keep the signal without copying that text to logs.
        self._structured_log("http_handler_message", level="warning")

    def _structured_log(self, event: str, **fields: Any) -> None:
        identity = self._client_identity_value()
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "request_id": self._request_id_value(),
            "method": getattr(self, "command", ""),
            "path": self._safe_request_path(),
            "client": identity.address,
            "peer": identity.peer,
            "client_source": identity.source,
            "forwarded_hops": identity.forwarded_hops,
            **fields,
        }
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        try:
            print(encoded, flush=True)
        except (OSError, ValueError):
            # A detached/closed console must not abort the HTTP response. Database
            # audit writes keep their existing failure policy; this is console I/O only.
            metrics.inc("http_log_failures_total")

    def _client_identity_value(self) -> ClientIdentity:
        cached = getattr(self, "_client_identity_cache", None)
        if isinstance(cached, ClientIdentity):
            return cached
        peer = self.client_address[0] if self.client_address else "unknown"
        headers = getattr(self, "headers", None)
        forwarded_values = headers.get_all("X-Forwarded-For", []) if headers is not None else []
        identity = self.service.client_identity_resolver.resolve(peer, ",".join(forwarded_values))
        self._client_identity_cache = identity
        if identity.source == "forwarded":
            metrics.inc("http_forwarded_client_total")
        elif identity.source == "ignored":
            metrics.inc("http_forwarded_ignored_total")
        elif identity.source == "invalid":
            metrics.inc("http_forwarded_invalid_total")
        return identity

    def _request_id_value(self) -> str:
        cached = getattr(self, "_request_id_cache", "")
        if cached:
            return cached
        headers = getattr(self, "headers", None)
        values = headers.get_all("X-Request-ID", []) if headers is not None else []
        supplied = values[0] if len(values) == 1 else ""
        request_id = supplied if REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        self._request_id_cache = request_id
        return request_id

    def _safe_request_path(self) -> str:
        try:
            path = urllib.parse.urlparse(getattr(self, "path", "")).path
        except ValueError:
            path = "/invalid-request-target"
        return path[:512]

    def _send_internal_error(self, exc: Exception) -> None:
        metrics.inc("http_errors_total")
        fields = safe_exception_fields(exc)
        self._structured_log(
            "http_internal_error",
            level="error",
            **fields,
        )
        if getattr(self, "_response_started", False):
            # A second status line would corrupt a response already on the wire.
            self.close_connection = True
            return
        self._send_json(
            500,
            {
                "error": "internal server error",
                "request_id": self._request_id_value(),
            },
        )

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _single_header(self, name: str) -> str:
        values = self.headers.get_all(name, []) or []
        if len(values) > 1:
            raise ClientInputError("%s must appear at most once" % name)
        return values[0] if values else ""

    def _principal(self, permission: str = "read") -> Principal:
        if not self.settings.auth_required:
            return Principal(
                "local",
                "local-development",
                self.settings.default_tenant_id,
                "platform_admin",
            )
        principal = self.service.auth.authenticate(self._single_header("Authorization"))
        self.service.auth.require(principal, (permission,))
        return principal

    def _authenticate_or_send(self, permission: str = "read"):
        try:
            return self._principal(permission)
        except AccessDeniedError as exc:
            self._send_json(401, {"error": str(exc)})
            return None

    def _send_json(
        self, status: int, value: dict[str, Any], *, retry_after: float | None = None
    ) -> None:
        if getattr(self, "_console_view", False):
            if status >= 400:
                value = console_error(status, value)
            elif 200 <= status < 300:
                projected = console_response(self.command, self._safe_request_path(), value)
                assert projected is not None
                value = projected
        body = json.dumps(value, ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Vary", "X-EvoAgent-View")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(
        self, status: int, text: str, content_type: str = "text/plain; charset=utf-8"
    ) -> None:
        body = text.encode("utf-8")
        self._headers(status, content_type, len(body))
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_file(self, filename: str) -> None:
        path = os.path.abspath(os.path.join(WEB_ROOT, filename))
        if not path.startswith(WEB_ROOT + os.sep) and path != WEB_ROOT:
            self._send_json(404, {"error": "not found"})
            return
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _declared_body_length(self) -> int:
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) > 1:
            self.close_connection = True
            raise ClientInputError("ambiguous request body framing")
        raw_length = lengths[0] if lengths else "0"
        if len(raw_length) > 20 or not raw_length.isascii() or not raw_length.isdigit():
            self.close_connection = True
            raise ClientInputError("invalid Content-Length")
        return int(raw_length)

    def _read_body(self) -> bytes:
        length = self._declared_body_length()
        limit = self.settings.max_diff_bytes + 256 * 1024
        if length <= 0 or length > limit:
            self.close_connection = length > limit
            raise ClientInputError("request body is empty or too large")
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            raise ClientInputError("request body is incomplete")
        return body

    @staticmethod
    def _read_json(body: bytes) -> dict[str, Any]:
        try:
            value = strict_json_loads(body)
        except (UnicodeError, ValueError, RecursionError):
            raise ClientInputError("request body must be valid UTF-8 JSON") from None
        if not isinstance(value, dict):
            raise ClientInputError("JSON root must be an object")
        return value

    @staticmethod
    def _string_field(payload: dict[str, Any], name: str, default: str = "") -> str:
        value = payload.get(name, default)
        if not isinstance(value, str):
            raise ClientInputError("%s must be a string" % name)
        return value

    @staticmethod
    def _query_limit(query: dict[str, list[str]], default: int) -> int:
        raw = query.get("limit", [str(default)])[0]
        try:
            value = int(raw)
        except ValueError:
            raise ClientInputError("limit must be an integer between 1 and 1000") from None
        if not 1 <= value <= 1000:
            raise ClientInputError("limit must be an integer between 1 and 1000")
        return value

    def do_GET(self) -> None:
        self._dispatch("GET", self._do_GET)

    def do_POST(self) -> None:
        self._dispatch("POST", self._do_POST)

    def _dispatch(self, method: str, handler) -> None:
        """Establish one request identity and one safe exception boundary."""
        self._request_id_cache = ""
        self._client_identity_cache = None
        self._response_started = False
        self._metric_counted = False
        self._metric_response_recorded = False
        self._console_view = False
        self._request_id_value()
        try:
            view = self._single_header("X-EvoAgent-View")
            if view:
                self._console_view = True
                if view != "console":
                    raise ClientInputError("X-EvoAgent-View must be console")
                console_response(method, _request_target(self.path).path)
            self._dispatch_with_admission(method, handler)
        except TenantReviewCapacityError:
            if not self._response_started:
                self._reject(
                    429,
                    self.settings.tenant_capacity_retry_seconds,
                    "tenant review capacity is exhausted",
                    drain_body=False,
                )
            else:
                self.close_connection = True
        except TaskCancelled:
            if not self._response_started:
                self._send_json(409, {"error": "review was cancelled"})
            else:
                self.close_connection = True
        except ResourceNotFoundError as exc:
            if not self._response_started:
                self._send_json(404, {"error": str(exc)})
            else:
                self.close_connection = True
        except StateConflictError as exc:
            if not self._response_started:
                self._send_json(409, {"error": str(exc)})
            else:
                self.close_connection = True
        except ClientInputError as exc:
            if not self._response_started:
                self._send_json(400, {"error": str(exc)})
            else:
                self.close_connection = True
        except AccessDeniedError as exc:
            if not self._response_started:
                self._send_json(403, {"error": str(exc)})
            else:
                self.close_connection = True
        except Exception as exc:
            self._send_internal_error(exc)

    def _dispatch_with_admission(self, method: str, handler) -> None:
        """Single choke point for admission control and request-level
        observability: per-client rate limiting, a bounded concurrency gate for
        heavy endpoints, a latency histogram, and an in-flight gauge. Overload is
        shed here (429/503 + Retry-After) rather than allowed to pile up."""
        parsed = _request_target(self.path)
        path = parsed.path
        request_class = self._request_metric_class(method, parsed)
        self._metric_class = request_class
        self._metric_counted = True
        self._metric_response_recorded = False
        metrics.inc("http_requests_total")
        metrics.inc("http_%s_requests_total" % request_class)
        if request_class != "probe":
            metrics.inc("http_nonprobe_requests_total")
        if DRAINING.is_set() and path not in PROBE_PATHS:
            self._reject(503, 1.0, "service is draining")
            return
        if path not in PROBE_PATHS or (path == "/metrics" and self.settings.auth_required):
            client = self._client_identity_value().address
            allowed, retry_after = self.service.rate_limiter.check(client)
            if not allowed:
                self._reject(429, retry_after, "rate limit exceeded")
                return
        # The concurrency gate protects synchronous CPU/sandbox work. Async
        # review intake (?async=true) only enqueues a task and is cheap + already
        # protected by the queue, so it is not gated.
        gated = _heavy_request(method, path)
        if (
            gated
            and path == "/v1/reviews"
            and _async_review_requested(_query_parameters(parsed.query))
        ):
            gated = False
        gate = self.service.heavy_gate if gated else None
        if gate is not None and not gate.try_acquire():
            self._reject(503, 1.0, "server is at capacity, retry shortly")
            return
        metrics.add_gauge("http_in_flight", 1)
        try:
            with (
                metrics.latency("http_request_%s" % method),
                metrics.latency("http_request_%s" % request_class),
            ):
                handler()
        finally:
            metrics.add_gauge("http_in_flight", -1)
            if gate is not None:
                gate.release()

    @staticmethod
    def _request_metric_class(method: str, parsed: urllib.parse.ParseResult) -> str:
        path = parsed.path
        if path in PROBE_PATHS:
            return "probe"
        if method == "POST" and path == "/webhooks/github":
            return "intake"
        if method == "POST" and path == "/v1/reviews":
            return "intake" if _async_review_requested(_query_parameters(parsed.query)) else "heavy"
        if path == "/v1/proofs":
            return "proof"
        if _heavy_request(method, path):
            return "heavy"
        if method == "GET":
            return "read"
        return "write"

    def _reject(
        self,
        status: int,
        retry_after: float,
        message: str,
        *,
        drain_body: bool = True,
    ) -> None:
        metrics.inc("http_rejected_total")
        # Admission rejection happens before body reads; never let a shed client
        # retain a worker by slowly sending its declared body.
        if drain_body and self._declared_body_length():
            self.close_connection = True
        self._send_json(status, {"error": message}, retry_after=retry_after)

    def _do_GET(self) -> None:
        parsed_url = _request_target(self.path)
        path = parsed_url.path
        query = _query_parameters(parsed_url.query)
        if path == "/":
            self._serve_file("index.html")
            return
        if path == "/assets/app.css":
            self._serve_file("app.css")
            return
        if path == "/assets/login.css":
            self._serve_file("login.css")
            return
        if path == "/assets/app.js":
            self._serve_file("app.js")
            return
        if path in {"/assets/studio.js", "/assets/studio.css"}:
            self._serve_file(path.rsplit("/", 1)[1])
            return
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/ready":
            if DRAINING.is_set():
                self._send_json(503, {"status": "draining"})
                return
            ready, detail = self.service.readiness()
            self._send_json(200 if ready else 503, detail)
            return
        if path == "/github/setup":
            self._redirect(
                self.service.authorize_github_installation(
                    query.get("state", [""])[0],
                    query.get("installation_id", [""])[0],
                )
            )
            return
        if path == "/github/oauth/callback":
            self.service.complete_github_installation(
                query.get("state", [""])[0],
                query.get("code", [""])[0],
            )
            self._redirect("/#github")
            return
        principal = self._authenticate_or_send("read")
        if principal is None:
            return
        # A view header selects less data, never more authority. Authoring data
        # and raw execution snapshots require manage before any resource lookup.
        if not principal.can("manage") and (
            path == "/v1/studio/catalog"
            or STUDIO_DOCUMENT.fullmatch(path)
            or (
                not getattr(self, "_console_view", False)
                and (
                    STUDIO_VERSION.fullmatch(path)
                    or TASK.fullmatch(path)
                    or WORKFLOW.fullmatch(path)
                    or WORKFLOW_ARTIFACT.fullmatch(path)
                )
            )
        ):
            raise AccessDeniedError("permission denied")
        if path == "/v1/studio/catalog":
            self._send_json(200, self.service.studio.catalog())
            return
        if path in {"/v1/studio/agents", "/v1/studio/workflows"}:
            if any(len(query.get(key, [])) > 1 for key in ("limit", "cursor")):
                raise ClientInputError("Studio pagination parameters must be unique")
            self._send_json(
                200,
                self.service.studio.list_documents(
                    principal.tenant_id,
                    path.rsplit("/", 1)[1],
                    limit=self._query_limit(query, 100),
                    cursor=query.get("cursor", [""])[0],
                ),
            )
            return
        studio_match = STUDIO_DOCUMENT.fullmatch(path)
        version_match = STUDIO_VERSION.fullmatch(path)
        if version_match:
            kind, key, version = version_match.groups()
            document = self.service.store.get_studio_version(
                principal.tenant_id, kind, key, int(version)
            )
            if document is None:
                raise ResourceNotFoundError("version not found")
            self._send_json(200, document)
            return
        if studio_match and not studio_match.group(3):
            kind, key, _action = studio_match.groups()
            document = self.service.store.get_studio_document(principal.tenant_id, kind, key)
            if document is None:
                raise ResourceNotFoundError("draft not found")
            if getattr(self, "_console_view", False):
                # Normalize a copy for older sparse drafts, never migrate on GET.
                try:
                    document["definition"] = draft_definition(kind, document["definition"])
                except ClientInputError:
                    raise ClientInputError("草稿结构暂不受编辑器支持，原始内容未修改。") from None
            self._send_json(200, document)
            return
        if path == "/v1/studio/binding":
            repository = canonical_repository(query.get("repository", [""])[0])
            self.service.review_use_cases.authorize_repository(principal.tenant_id, repository)
            self._send_json(
                200,
                {"binding": self.service.store.get_studio_binding(principal.tenant_id, repository)},
            )
            return
        artifact_match = WORKFLOW_ARTIFACT.fullmatch(path)
        if artifact_match:
            artifact = self.service.studio.artifact(principal.tenant_id, *artifact_match.groups())
            if artifact is None:
                raise ResourceNotFoundError("workflow artifact not found or expired")
            if self._console_view:
                artifact_workflow = (
                    self.service.store.workflow_status(artifact_match.group(1), principal.tenant_id)
                    or {}
                )
                artifact["port_types"] = next(
                    (
                        step
                        for step in artifact_workflow.get("steps", [])
                        if step["id"] == artifact_match.group(2)
                    ),
                    {},
                )
            self._send_json(200, artifact)
            return
        if path in {
            "/metrics",
            "/v1/evaluation/cases",
            "/v1/evolution/runs",
            "/v1/evolution/status",
        } and not principal.can("platform"):
            self._send_json(403, {"error": "permission denied"})
            return
        if path == "/metrics":
            self._send_text(200, metrics.prometheus(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/api/dashboard":
            self._send_json(
                200,
                {
                    "stats": self.service.store.dashboard_stats(principal.tenant_id),
                    "tasks": self.service.store.list_tasks(10, principal.tenant_id),
                    "queue": self.service.queue.backend,
                    "orchestrator": self.service.reviewer.name,
                    "capabilities": self.service.console_capabilities(principal),
                },
            )
            return
        if path == "/api/tasks":
            self._send_json(
                200,
                {
                    "tasks": self.service.store.list_tasks(
                        self._query_limit(query, 50), principal.tenant_id
                    )
                },
            )
            return
        if path == "/api/skills":
            skills, revision = self.service.skill_inventory()
            self._send_json(200, {"skills": skills, "reviewer_revision": revision})
            return
        if path == "/api/failures":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(
                200,
                {"cases": self.service.store.list_failure_cases(False, 100, principal.tenant_id)},
            )
            return
        if path == "/api/audit":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(
                200,
                {
                    "events": self.service.store.list_audit(
                        principal.tenant_id, self._query_limit(query, 100)
                    )
                },
            )
            return
        if path == "/api/alerts":
            self._send_json(200, {"alerts": self.service.store.list_alerts(principal.tenant_id)})
            return
        if path == "/api/deployments/llm-review":
            self._send_json(
                200,
                {
                    "deployment": self.service.store.get_deployment(
                        principal.tenant_id, "llm-review"
                    )
                },
            )
            return
        if path == "/api/queue/dead-letters":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(
                200,
                {
                    "messages": self.service.tenant_dead_letters(
                        principal.tenant_id, self._query_limit(query, 100)
                    )
                },
            )
            return
        if path == "/api/outbox":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            outbox_status = query.get("status", ["dead"])[0]
            self._send_json(
                200,
                {
                    "messages": self.service.store.list_outbox(
                        outbox_status, self._query_limit(query, 100), principal.tenant_id
                    )
                },
            )
            return
        if path == "/api/tenant-review-capacity":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(
                200,
                self.service.tenant_review_capacity_report(principal.tenant_id),
            )
            return
        if path == "/v1/repository-policies":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            repository = canonical_repository(query.get("repository", [""])[0])
            if not repository:
                self._send_json(400, {"error": "repository query parameter is required"})
                return
            self._send_json(
                200,
                self.service.get_repository_policy(principal.tenant_id, repository),
            )
            return
        if path == "/v1/evaluation/cases":
            split = query.get("split", ["validation"])[0]
            if split == "holdout":
                self._send_json(403, {"error": "holdout cases are not exposed through the API"})
                return
            self._send_json(
                200, {"cases": self.service.store.list_evaluation_cases(split, True, 100)}
            )
            return
        if path == "/v1/evolution/runs":
            self._send_json(
                200,
                {"runs": self.service.store.list_evolution_runs(self._query_limit(query, 50))},
            )
            return
        if path == "/v1/evolution/status":
            status = self.service.evolution.status()
            status["provider"] = self.service.llm_config.get("provider", "local")
            status["model"] = self.service.llm_config.get("model", "")
            self._send_json(200, status)
            return
        if path == "/v1/sessions":
            repository = canonical_repository(query.get("repository", [""])[0])
            raw_pr = query.get("pull_request", [""])[0]
            try:
                pull_request = int(raw_pr)
            except ValueError:
                self._send_json(400, {"error": "repository and integer pull_request are required"})
                return
            timeline = self.service.get_session_for_pull_request(
                repository, pull_request, principal.tenant_id
            )
            if not timeline:
                self._send_json(404, {"error": "session not found"})
                return
            self._send_json(200, timeline)
            return
        session_match = SESSION.match(path)
        if session_match:
            timeline = self.service.get_session_timeline(
                session_match.group(1), principal.tenant_id
            )
            if not timeline:
                self._send_json(404, {"error": "session not found"})
                return
            self._send_json(200, timeline)
            return
        workflow_match = WORKFLOW.match(path)
        if workflow_match:
            snapshot = self.service.store.workflow_status(
                workflow_match.group(1), principal.tenant_id
            )
            if snapshot is None:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, snapshot)
            return
        report_match = REPORT.match(path)
        task_match = TASK.match(path)
        if report_match:
            task = self.service.store.get(report_match.group(1), principal.tenant_id)
            if not task or not task.get("report"):
                self._send_json(404, {"error": "task or report not found"})
                return
            self._send_text(200, to_markdown(task["report"]), "text/markdown; charset=utf-8")
            return
        if task_match:
            task = self.service.store.get(task_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            if getattr(self, "_console_view", False):
                task = {**task, "fix_blocker": self.service.console_fix_blocker(task, principal)}
            self._send_json(200, task)
            return
        self._send_json(404, {"error": "not found"})

    def _do_POST(self) -> None:
        parsed_url = _request_target(self.path)
        path = parsed_url.path
        query = _query_parameters(parsed_url.query)
        try:
            body = self._read_body()
            if path.startswith("/v1/studio/"):
                principal = self._principal("manage")
                payload = self._read_json(body)
                if path in {"/v1/studio/agents", "/v1/studio/workflows"}:
                    self._send_json(
                        201,
                        self.service.studio.save(
                            principal.tenant_id, path.rsplit("/", 1)[1], payload, principal.username
                        ),
                    )
                    return
                studio_match = STUDIO_DOCUMENT.fullmatch(path)
                if studio_match and studio_match.group(3):
                    if payload.keys() != {"revision"}:
                        raise ClientInputError("publish requires only the saved draft revision")
                    self._send_json(
                        201,
                        self.service.studio.publish(
                            principal.tenant_id,
                            studio_match.group(1),
                            studio_match.group(2),
                            payload["revision"],
                            principal.username,
                        ),
                    )
                    return
                if path == "/v1/studio/validate":
                    if payload.keys() != {"definition"}:
                        raise ClientInputError("validation requires only definition")
                    bundle = self.service.studio.resolve(principal.tenant_id, payload["definition"])
                    workflow = compile_workflow(
                        bundle,
                        self.service.studio.builtins(),
                        self.service.model_gateway,
                        self.service.studio.context,
                    )
                    self._send_json(200, {"valid": True, "workflow": workflow.describe()})
                    return
                if path == "/v1/studio/binding":
                    if payload.keys() != {"repository", "workflow_id", "version", "revision"}:
                        raise ClientInputError(
                            "binding requires repository, workflow_id, version and revision; "
                            "workflow_id and version must both be null to unbind"
                        )
                    binding_repository = canonical_repository(payload["repository"])
                    policy = self.service.review_use_cases.authorize_repository(
                        principal.tenant_id, binding_repository
                    )
                    key = (
                        document_id(payload["workflow_id"])
                        if payload["workflow_id"] is not None
                        else None
                    )
                    revision = revision_number(payload["revision"], zero=True)
                    version = revision_number(payload["version"]) if key is not None else None
                    if key is None and payload["version"] is not None:
                        raise ClientInputError("version must be null when unbinding")
                    if key is not None:
                        self.service.review_use_cases.authorize_runtime(
                            principal.tenant_id, binding_repository, policy
                        )
                        self.service.studio.select(
                            principal.tenant_id, binding_repository, {"id": key, "version": version}
                        )
                    binding = self.service.store.bind_studio_workflow(
                        principal.tenant_id,
                        binding_repository,
                        key,
                        principal.username,
                        version=version,
                        expected_revision=revision,
                    )
                    self._send_json(200, {"binding": binding})
                    return
                self._send_json(404, {"error": "not found"})
                return
            if path == "/v1/auth/login":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                payload = self._read_json(body)
                try:
                    result = self.service.auth.login(
                        self._string_field(payload, "username"),
                        self._string_field(payload, "password"),
                        self._string_field(payload, "tenant_id"),
                    )
                except AccessDeniedError as exc:
                    self._send_json(401, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return
            if path == "/v1/auth/password":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                principal = self._principal("read")
                payload = self._read_json(body)
                if set(payload) != {"current_password", "new_password"}:
                    raise ClientInputError(
                        "password change requires only current_password and new_password"
                    )
                self.service.auth.change_password(
                    principal,
                    self._string_field(payload, "current_password"),
                    self._string_field(payload, "new_password"),
                )
                self._send_json(200, {"changed": True, "reauthenticate": True})
                return
            if path == "/v1/users":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                principal = self._principal("manage")
                payload = self._read_json(body)
                if set(payload) != {"username", "password", "role"}:
                    raise ClientInputError(
                        "user creation requires only username, password and role"
                    )
                self._send_json(
                    201,
                    self.service.auth.provision_user(
                        principal,
                        self._string_field(payload, "username"),
                        self._string_field(payload, "password"),
                        self._string_field(payload, "role"),
                    ),
                )
                return
            if path == "/v1/users/status":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                principal = self._principal("platform")
                payload = self._read_json(body)
                if set(payload) != {"username", "active"}:
                    raise ClientInputError("user status requires only username and active")
                active = payload["active"]
                if not isinstance(active, bool):
                    raise ClientInputError("active must be a boolean")
                self._send_json(
                    200,
                    self.service.auth.set_user_active(
                        principal,
                        self._string_field(payload, "username"),
                        active,
                    ),
                )
                return
            if path == "/v1/github/installations":
                principal = self._principal("manage")
                payload = self._read_json(body)
                if payload:
                    raise ClientInputError("GitHub installation request does not accept parameters")
                self._send_json(200, {"url": self.service.begin_github_installation(principal)})
                return
            if path == "/v1/reviews":
                principal = self._principal("review")
                payload = self._read_json(body)
                if set(payload).difference({"repository", "diff", "pull_request", "workflow"}):
                    raise ClientInputError(
                        "review request accepts only repository, diff, pull_request and workflow"
                    )
                repository = payload.get("repository")
                diff = payload.get("diff")
                pr = payload.get("pull_request")
                if not isinstance(repository, str) or not isinstance(diff, str):
                    raise ClientInputError("repository and diff must be strings")
                repository = canonical_repository(repository)
                selection = payload.get("workflow")
                if "workflow" in payload:
                    if not isinstance(selection, dict):
                        raise ClientInputError("workflow must be a version selection object")
                    if "draft_revision" in selection and not principal.can("manage"):
                        raise AccessDeniedError("draft trial runs require manage permission")
                if pr is not None and (
                    not isinstance(pr, int) or isinstance(pr, bool) or not 1 <= pr <= 2**31 - 1
                ):
                    raise ClientInputError("pull_request must be a positive integer")
                idempotency_values = self.headers.get_all("Idempotency-Key", []) or []
                if len(idempotency_values) > 1 or (
                    idempotency_values and not REQUEST_ID.fullmatch(idempotency_values[0])
                ):
                    raise ClientInputError("Idempotency-Key must be one 1-64 character token")
                idempotency_key = idempotency_values[0] if idempotency_values else ""
                async_requested = _async_review_requested(query)
                if idempotency_key and not async_requested:
                    raise ClientInputError(
                        "Idempotency-Key is supported only for asynchronous reviews"
                    )
                args = (repository, diff, pr)
                if async_requested:
                    result = self.service.enqueue_review(
                        *args,
                        tenant_id=principal.tenant_id,
                        idempotency_key=idempotency_key,
                        actor=principal.username,
                        **({"workflow_selection": selection} if selection is not None else {}),
                    )
                else:
                    result = self.service.create_review(
                        *args,
                        tenant_id=principal.tenant_id,
                        actor=principal.username,
                        **({"workflow_selection": selection} if selection is not None else {}),
                    )
                status = 200 if async_requested and result.get("replayed") else 202
                self._send_json(status if async_requested else 201, result)
                return
            if path == "/v1/repository-policies":
                principal = self._principal("manage")
                payload = self._read_json(body)
                repository = self._string_field(payload, "repository")
                repository = canonical_repository(repository)
                raw_policy = payload.get("policy")
                if not isinstance(raw_policy, dict):
                    raise ClientInputError("policy must be an object")
                result = self.service.set_repository_policy(
                    principal.tenant_id,
                    repository,
                    raw_policy,
                    principal.username,
                )
                self._send_json(201, result)
                return
            if path == "/webhooks/github":
                if not self.settings.github_webhook_secret:
                    self._send_json(503, {"error": "GitHub webhook secret is not configured"})
                    return
                signature = self._single_header("X-Hub-Signature-256")
                verified = verify_signature(
                    self.settings.github_webhook_secret,
                    body,
                    signature,
                )
                if not verified and self.settings.github_webhook_previous_secret:
                    verified = verify_signature(
                        self.settings.github_webhook_previous_secret,
                        body,
                        signature,
                    )
                    if verified:
                        metrics.inc("github_webhook_previous_secret_verifications_total")
                if not verified:
                    self._send_json(401, {"error": "invalid webhook signature"})
                    return
                if self._single_header("X-GitHub-Event") != "pull_request":
                    self._send_json(202, {"ignored": True, "reason": "unsupported GitHub event"})
                    return
                payload = self._read_json(body)
                event_time = github_pull_request_updated_at(payload)
                age = abs((datetime.now(UTC) - event_time).total_seconds())
                delivery_id = self._single_header("X-GitHub-Delivery")
                if not REQUEST_ID.fullmatch(delivery_id):
                    raise ClientInputError("invalid X-GitHub-Delivery")
                digest = hashlib.sha256(body).hexdigest()
                if age > self.settings.webhook_max_age_seconds:
                    existing = self.service.store.get_webhook(delivery_id)
                    if not existing or existing.get("payload_sha256") != digest:
                        self._send_json(409, {"error": "webhook is outside the replay window"})
                        return
                self._send_json(
                    202, self.service.handle_github_pull_request(payload, delivery_id, digest)
                )
                return
            match = FIX.match(path)
            if match:
                principal = self._principal("fix")
                payload = self._read_json(body)
                if payload:
                    raise ClientInputError("fix request does not accept parameters")
                result = self.service.create_fix(
                    match.group(1), principal.tenant_id, principal.username
                )
                self._send_json(200 if result["replayed"] else 201, result)
                return
            match = FEEDBACK.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                if set(payload).difference({"category", "finding", "note"}):
                    raise ClientInputError("feedback accepts only category, finding and note")
                finding = payload.get("finding")
                if finding is not None and not isinstance(finding, dict):
                    raise ClientInputError("finding must be an object or null")
                self._send_json(
                    201,
                    self.service.record_feedback(
                        match.group(1),
                        self._string_field(payload, "category"),
                        finding,
                        self._string_field(payload, "note"),
                        principal.tenant_id,
                        principal.username,
                    ),
                )
                return
            match = CANCEL.match(path)
            if match:
                principal = self._principal("review")
                if self._read_json(body):
                    raise ClientInputError("cancel request does not accept parameters")
                result = self.service.cancel_task(
                    match.group(1), principal.tenant_id, principal.username
                )
                status = 202 if result["cancel_requested"] else 409 if result["accepted"] else 404
                self._send_json(status, result)
                return
            match = RESUME.match(path)
            if match:
                principal = self._principal("review")
                if self._read_json(body):
                    raise ClientInputError("resume request does not accept parameters")
                result = self.service.resume_task(
                    match.group(1), principal.tenant_id, principal.username
                )
                resumed = result.get("resumed") or result.get("delivery_resumed")
                self._send_json(202 if resumed else 200, result)
                return
            match = SESSION_INPUT.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                if set(payload) != {"message"}:
                    raise ClientInputError("session input requires only message")
                result = self.service.provide_session_input(
                    match.group(1),
                    self._string_field(payload, "message"),
                    principal.tenant_id,
                    principal.username,
                )
                self._send_json(200, result)
                return
            if path == "/v1/codegraph/impact":
                principal = self._principal("review")
                payload = self._read_json(body)
                result = self.service.analyze_impact(
                    payload.get("files", {}), payload.get("changed", [])
                )
                self._send_json(201, result)
                return
            if path == "/v1/proofs":
                principal = self._principal("fix")
                payload = self._read_json(body)
                result = self.service.run_proof(
                    payload.get("original", {}),
                    payload.get("patched", {}),
                    self._string_field(payload, "reproduction_command"),
                    self._string_field(payload, "regression_command"),
                )
                steps = result.get("steps")
                attestations = (
                    [
                        step["attestation"]
                        for step in steps
                        if isinstance(step, dict) and isinstance(step.get("attestation"), dict)
                    ]
                    if isinstance(steps, list)
                    else []
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "proof.run",
                    str(result.get("evidence_label", "")),
                    {
                        "level": result.get("evidence_level"),
                        "attestations": attestations,
                    },
                )
                self._send_json(201, result)
                return
            if path == "/v1/deployments/llm-review":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.releases.configure(
                    principal.tenant_id,
                    "llm-review",
                    payload,
                    principal.username,
                )
                self._send_json(201, result)
                return
            if path == "/v1/outbox/replay":
                principal = self._principal("manage")
                payload = self._read_json(body)
                message_id = self._string_field(payload, "message_id")
                ok = self.service.replay_outbox(
                    message_id,
                    principal.tenant_id,
                    principal.username,
                )
                self._send_json(202 if ok else 404, {"replayed": ok})
                return
            if path == "/v1/evaluation/cases":
                self._principal("platform")
                payload = self._read_json(body)
                result = self.service.evolution.add_evaluation_case(
                    self._string_field(payload, "name"),
                    self._string_field(payload, "diff"),
                    payload.get("expected_findings", []),
                    self._string_field(payload, "split", "validation"),
                    "api",
                )
                self._send_json(201, result)
                return
            if path == "/v1/evolution/auto":
                self._principal("platform")
                payload = self._read_json(body)
                result = self.service.evolution.auto_propose(
                    self._string_field(payload, "skill_name", "llm-review")
                )
                self._send_json(201, result)
                return
            if path == "/v1/evolution/propose":
                self._principal("platform")
                payload = self._read_json(body)
                regression_score = payload.get("regression_score")
                if regression_score is not None and (
                    isinstance(regression_score, bool)
                    or not isinstance(regression_score, (int, float))
                    or not math.isfinite(regression_score)
                ):
                    raise ClientInputError("regression_score must be a finite number or null")
                result = self.service.evolution.propose(
                    self._string_field(payload, "skill_name"),
                    self._string_field(payload, "prompt"),
                    float(regression_score) if regression_score is not None else None,
                )
                self._send_json(201, result)
                return
            self._send_json(404, {"error": "not found"})
        except ResourceNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except StateConflictError as exc:
            self._send_json(409, {"error": str(exc)})
        except ClientInputError as exc:
            self._send_json(400, {"error": str(exc)})
        except AccessDeniedError as exc:
            self._send_json(403, {"error": str(exc)})


class EvoAgentHTTPServer(ThreadingHTTPServer):
    """Threaded server with bounded connections, I/O and graceful drain."""

    allow_reuse_address = True
    daemon_threads = False
    request_io_timeout_seconds = 30.0

    def __init__(self, *args: Any, max_connections: int = 128, **kwargs: Any):
        self.connection_gate = ConcurrencyLimiter(max_connections)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.request_io_timeout_seconds)
        return connection, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.connection_gate.try_acquire():
            metrics.inc("http_connections_rejected_total")
            self.shutdown_request(request)
            return
        metrics.add_gauge("http_connections_in_flight", 1)
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_gate.release()
            metrics.add_gauge("http_connections_in_flight", -1)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_gate.release()
            metrics.add_gauge("http_connections_in_flight", -1)


def _make_server(settings: Settings, service: ReviewService) -> EvoAgentHTTPServer:
    handler = type(
        "ConfiguredApiHandler", (ApiHandler,), {"service": service, "settings": settings}
    )
    return EvoAgentHTTPServer(
        (settings.host, settings.port),
        handler,
        max_connections=settings.max_http_connections,
    )


def _install_drain_on_sigterm(server: ThreadingHTTPServer, grace_seconds: float) -> None:
    """On SIGTERM: flip readiness to draining (so the LB stops routing), wait a
    short grace period, then stop accepting. Shutdown runs on its own thread
    because ``shutdown()`` must not be called from the ``serve_forever`` thread."""

    def _handler(_signum: int, _frame: Any) -> None:
        DRAINING.set()

        def _drain() -> None:
            if grace_seconds:
                time.sleep(grace_seconds)
            server.shutdown()

        threading.Thread(target=_drain, name="evoagent-drain", daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:  # pragma: no cover - not on main thread (e.g. under tests)
        pass


def _print_banner(settings: Settings, service: ReviewService) -> None:
    print("EvoAgent dashboard: http://%s:%d" % (settings.host, settings.port))
    print(
        "Persistence: %s | Queue: %s | Orchestrator: %s"
        % (
            "postgresql",
            service.queue.backend,
            service.reviewer.name,
        )
    )
    exposed = settings.host not in {"127.0.0.1", "localhost", "::1"}
    if exposed and not service.queue.durable:
        print(
            "WARNING: this deployment looks production-facing but the task queue is "
            "'%s' (non-durable). Set EVOAGENT_REDIS_URL for durable, crash-safe "
            "delivery; otherwise pending/in-flight/dead-letter tasks are lost on restart."
            % service.queue.backend
        )


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(
        prog="evoagent",
        description="Serve the EvoAgent API using EVOAGENT_* environment variables.",
        allow_abbrev=False,
    ).parse_args(argv)
    run()


def run(
    *,
    workflow_factory: WorkflowFactory | None = None,
    reviewer_contributions: Sequence[ReviewerContribution] | None = None,
) -> None:
    """Serve a trusted deployment using the normal auth, queue and shutdown lifecycle."""
    settings = Settings.from_env()
    service = ReviewService(
        settings,
        workflow_factory=workflow_factory,
        reviewer_contributions=reviewer_contributions,
    )
    server: EvoAgentHTTPServer | None = None
    try:
        server = _make_server(settings, service)
        _print_banner(settings, service)
        _install_drain_on_sigterm(server, settings.shutdown_grace_seconds)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        if server is not None:
            server.server_close()
        service.close()
