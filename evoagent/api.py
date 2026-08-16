import hashlib
import json
import math
import mimetypes
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .auth import Principal
from .config import Settings
from .github import verify_signature
from .metrics import metrics
from .report import to_markdown
from .service import ReviewService

TASK = re.compile(r"^/v1/tasks/([0-9a-f-]+)$")
REPORT = re.compile(r"^/v1/tasks/([0-9a-f-]+)/report$")
FIX = re.compile(r"^/v1/tasks/([0-9a-f-]+)/fix$")
FEEDBACK = re.compile(r"^/v1/tasks/([0-9a-f-]+)/feedback$")
CANCEL = re.compile(r"^/v1/tasks/([0-9a-f-]+)/cancel$")
RESUME = re.compile(r"^/v1/tasks/([0-9a-f-]+)/resume$")
SESSION = re.compile(r"^/v1/sessions/([0-9a-f-]+)$")
SESSION_INPUT = re.compile(r"^/v1/sessions/([0-9a-f-]+)/input$")
ROLLBACK = re.compile(r"^/v1/skills/([A-Za-z0-9_-]+)/versions/(\d+)/activate$")
# CPU/sandbox-heavy POST endpoints guarded by the bounded-concurrency gate.
HEAVY_PATHS = frozenset({"/v1/reviews", "/v1/proofs", "/v1/codegraph/impact"})
# Liveness/readiness/metrics probes are never rate-limited: monitoring and
# orchestration must keep working precisely when the service is under overload.
PROBE_PATHS = frozenset({"/health", "/ready", "/metrics"})
SOURCE_WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
INSTALLED_WEB_ROOT = os.path.join(sys.prefix, "share", "evoagent", "web")
WEB_ROOT = SOURCE_WEB_ROOT if os.path.isdir(SOURCE_WEB_ROOT) else INSTALLED_WEB_ROOT
# Set on SIGTERM so /ready starts failing (load balancers drain us) while
# in-flight requests finish before the process exits.
DRAINING = threading.Event()


class ApiHandler(BaseHTTPRequestHandler):
    service: ReviewService
    settings: Settings
    server_version = "EvoAgent/0.3"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()

    def _principal(self, permission: str = "read") -> Principal:
        if not self.settings.auth_required:
            return Principal("local", "local-development", self.settings.default_tenant_id, "admin")
        principal = self.service.auth.authenticate(self.headers.get("Authorization", ""))
        self.service.auth.require(principal, (permission,))
        return principal

    def _authenticate_or_send(self, permission: str = "read"):
        try:
            return self._principal(permission)
        except PermissionError as exc:
            self._send_json(401, {"error": str(exc)})
            return None

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _send_text(
        self, status: int, text: str, content_type: str = "text/plain; charset=utf-8"
    ) -> None:
        body = text.encode("utf-8")
        self._headers(status, content_type, len(body))
        self.wfile.write(body)

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

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        limit = self.settings.max_diff_bytes + 256 * 1024
        if length <= 0 or length > limit:
            raise ValueError("request body is empty or too large")
        return self.rfile.read(length)

    def _drain_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.close_connection = True
            return
        cap = self.settings.max_diff_bytes + 256 * 1024
        if length <= 0:
            return
        if length > cap:
            # Too large to safely drain; abandon keep-alive on this connection.
            self.close_connection = True
            length = cap
        while length > 0:
            chunk = self.rfile.read(min(65536, length))
            if not chunk:
                break
            length -= len(chunk)

    @staticmethod
    def _read_json(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid UTF-8 JSON") from None
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value

    def do_GET(self) -> None:
        self._dispatch("GET", self._do_GET)

    def do_POST(self) -> None:
        self._dispatch("POST", self._do_POST)

    def _dispatch(self, method: str, handler) -> None:
        """Single choke point for admission control and request-level
        observability: per-client rate limiting, a bounded concurrency gate for
        heavy endpoints, a latency histogram, and an in-flight gauge. Overload is
        shed here (429/503 + Retry-After) rather than allowed to pile up."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path not in PROBE_PATHS:
            client = self.client_address[0] if self.client_address else "unknown"
            allowed, retry_after = self.service.rate_limiter.check(client)
            if not allowed:
                self._reject(429, retry_after, "rate limit exceeded")
                return
        # The concurrency gate protects synchronous CPU/sandbox work. Async
        # review intake (?async=true) only enqueues a task and is cheap + already
        # protected by the queue, so it is not gated.
        gated = method == "POST" and path in HEAVY_PATHS
        if gated and path == "/v1/reviews":
            async_values = urllib.parse.parse_qs(parsed.query).get("async", ["false"])
            if async_values and async_values[0].lower() in {"true", "1", "yes"}:
                gated = False
        gate = self.service.heavy_gate if gated else None
        if gate is not None and not gate.try_acquire():
            self._reject(503, 1.0, "server is at capacity, retry shortly")
            return
        metrics.add_gauge("http_in_flight", 1)
        try:
            with metrics.latency("http_request_%s" % method):
                handler()
        finally:
            metrics.add_gauge("http_in_flight", -1)
            if gate is not None:
                gate.release()

    def _reject(self, status: int, retry_after: float, message: str) -> None:
        metrics.inc("http_rejected_total")
        # Drain any request body so the JSON error + Retry-After are delivered
        # cleanly instead of the peer seeing a connection reset (which would make
        # the Retry-After signal unreliable for exactly the shed heavy requests).
        self._drain_body()
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
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
        if path == "/health":
            plugin_status = self.service.plugin_status()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "reviewer": self.service.reviewer.name,
                    "runtime": self.service.harness.name,
                    "queue": self.service.queue.backend,
                    "queue_durable": self.service.queue.durable,
                    "llm_provider": self.service.llm_config.get("provider", "local"),
                    "llm_model": self.service.llm_config.get("model", ""),
                    "plugin_runtime": plugin_status["state"],
                    "plugin_profile": plugin_status["profile"],
                    "plugins": len(plugin_status["plugins"]),
                },
            )
            return
        if path == "/ready":
            if DRAINING.is_set():
                self._send_json(503, {"status": "draining"})
                return
            ready, detail = self.service.readiness()
            self._send_json(200 if ready else 503, detail)
            return
        principal = self._authenticate_or_send("read")
        if principal is None:
            return
        if path == "/metrics":
            self._send_text(200, metrics.prometheus(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/v1/plugins":
            self._send_json(200, self.service.plugin_status())
            return
        if path == "/api/dashboard":
            self._send_json(
                200,
                {
                    "stats": self.service.store.dashboard_stats(principal.tenant_id),
                    "tasks": self.service.store.list_tasks(10, principal.tenant_id),
                    "queue": self.service.queue.backend,
                    "orchestrator": self.service.reviewer.name,
                },
            )
            return
        if path == "/api/tasks":
            self._send_json(
                200,
                {
                    "tasks": self.service.store.list_tasks(
                        int(query.get("limit", [50])[0]), principal.tenant_id
                    )
                },
            )
            return
        if path == "/api/skills":
            self._send_json(200, {"skills": self.service.registry.list()})
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
                        principal.tenant_id, int(query.get("limit", [100])[0])
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
                {"messages": self.service.queue.dead_letters(int(query.get("limit", [100])[0]))},
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
                        outbox_status, int(query.get("limit", [100])[0])
                    )
                },
            )
            return
        if path == "/v1/repository-policies":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            repository = query.get("repository", [""])[0]
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
                {"runs": self.service.store.list_evolution_runs(int(query.get("limit", [50])[0]))},
            )
            return
        if path == "/v1/evolution/status":
            status = self.service.evolution.status()
            status["provider"] = self.service.llm_config.get("provider", "local")
            status["model"] = self.service.llm_config.get("model", "")
            self._send_json(200, status)
            return
        if path == "/github/install":
            if not self.settings.github_app_slug:
                self._send_json(503, {"error": "EVOAGENT_GITHUB_APP_SLUG is not configured"})
                return
            self.send_response(302)
            self.send_header(
                "Location",
                "https://github.com/apps/%s/installations/new" % self.settings.github_app_slug,
            )
            self.end_headers()
            return
        if path == "/github/setup":
            try:
                installation_id = int(query.get("installation_id", [""])[0])
            except ValueError:
                self._send_json(400, {"error": "missing installation_id"})
                return
            self.service.store.save_installation(
                installation_id, query.get("account", ["github-app"])[0]
            )
            self.send_response(302)
            self.send_header("Location", "/#github")
            self.end_headers()
            return
        if path == "/v1/sessions":
            repository = query.get("repository", [""])[0]
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
            self._send_json(200, task)
            return
        self._send_json(404, {"error": "not found"})

    def _do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            body = self._read_body()
            if path == "/v1/auth/login":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                payload = self._read_json(body)
                try:
                    result = self.service.auth.login(
                        str(payload.get("username", "")),
                        str(payload.get("password", "")),
                        str(payload.get("tenant_id", "")),
                    )
                except PermissionError as exc:
                    self._send_json(401, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return
            if path == "/v1/reviews":
                principal = self._principal("review")
                payload = self._read_json(body)
                pr = payload.get("pull_request")
                if pr is not None and not isinstance(pr, int):
                    raise ValueError("pull_request must be an integer")
                args = (str(payload.get("repository", "")), str(payload.get("diff", "")), pr)
                if query.get("async", ["false"])[0].lower() == "true":
                    result = self.service.enqueue_review(*args, tenant_id=principal.tenant_id)
                    self._send_json(202, result)
                else:
                    self._send_json(
                        201, self.service.create_review(*args, tenant_id=principal.tenant_id)
                    )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "review.create",
                    str(payload.get("repository", "")),
                    {"async": query.get("async", ["false"])[0]},
                )
                return
            if path == "/v1/repository-policies":
                principal = self._principal("manage")
                payload = self._read_json(body)
                repository = str(payload.get("repository", ""))
                raw_policy = payload.get("policy")
                if not isinstance(raw_policy, dict):
                    raise ValueError("policy must be an object")
                result = self.service.set_repository_policy(
                    principal.tenant_id,
                    repository,
                    raw_policy,
                    principal.username,
                )
                self._send_json(201, result)
                return
            if path == "/webhooks/github":
                if self.headers.get("X-GitHub-Event", "") != "pull_request":
                    self._send_json(202, {"ignored": True, "reason": "unsupported GitHub event"})
                    return
                if not self.settings.github_webhook_secret:
                    self._send_json(503, {"error": "GitHub webhook secret is not configured"})
                    return
                if not verify_signature(
                    self.settings.github_webhook_secret,
                    body,
                    self.headers.get("X-Hub-Signature-256", ""),
                ):
                    self._send_json(401, {"error": "invalid webhook signature"})
                    return
                payload = self._read_json(body)
                updated_at = (payload.get("pull_request") or {}).get("updated_at")
                if updated_at:
                    try:
                        event_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    except ValueError:
                        raise ValueError("invalid pull_request.updated_at") from None
                    age = abs((datetime.now(UTC) - event_time).total_seconds())
                    if age > self.settings.webhook_max_age_seconds:
                        self._send_json(409, {"error": "webhook is outside the replay window"})
                        return
                delivery_id = self.headers.get("X-GitHub-Delivery", "")
                digest = hashlib.sha256(body).hexdigest()
                self._send_json(
                    202, self.service.handle_github_pull_request(payload, delivery_id, digest)
                )
                return
            match = FIX.match(path)
            if match:
                principal = self._principal("fix")
                payload = self._read_json(body)
                installation_id = payload.get("installation_id")
                if installation_id is not None and not isinstance(installation_id, int):
                    raise ValueError("installation_id must be an integer")
                result = self.service.create_fix(
                    match.group(1), installation_id, principal.tenant_id
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "repair.create",
                    match.group(1),
                    {"branch": result.get("branch")},
                )
                self._send_json(201, result)
                return
            match = FEEDBACK.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                self._send_json(
                    201,
                    self.service.record_feedback(
                        match.group(1),
                        str(payload.get("category", "")),
                        payload.get("finding"),
                        str(payload.get("note", "")),
                        principal.tenant_id,
                    ),
                )
                return
            match = CANCEL.match(path)
            if match:
                principal = self._principal("review")
                ok = self.service.cancel_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.cancel", match.group(1)
                )
                self._send_json(202 if ok else 404, {"cancel_requested": ok})
                return
            match = RESUME.match(path)
            if match:
                principal = self._principal("review")
                result = self.service.resume_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.resume", match.group(1)
                )
                self._send_json(202, result)
                return
            match = SESSION_INPUT.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                result = self.service.provide_session_input(
                    match.group(1),
                    str(payload.get("message", "")),
                    principal.tenant_id,
                )
                self._send_json(201, result)
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
                    str(payload.get("reproduction_command", "")),
                    str(payload.get("regression_command", "")),
                )
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "proof.run",
                    str(result.get("evidence_label", "")),
                    {"level": result.get("evidence_level")},
                )
                self._send_json(201, result)
                return
            if path == "/v1/skills/reload":
                principal = self._principal("manage")
                self._send_json(
                    200,
                    {
                        "skills": self.service.reload_skills(),
                        "note": "New tasks now use the reloaded skill set.",
                    },
                )
                return
            if path == "/v1/deployments/llm-review":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.releases.configure(principal.tenant_id, "llm-review", payload)
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "deployment.configure",
                    "llm-review",
                    payload,
                )
                self._send_json(201, result)
                return
            if path == "/v1/queue/dead-letters/replay":
                principal = self._principal("manage")
                payload = self._read_json(body)
                ok = self.service.queue.replay_dead_letter(str(payload.get("message_id", "")))
                self._send_json(202 if ok else 404, {"replayed": ok})
                return
            if path == "/v1/outbox/replay":
                principal = self._principal("manage")
                payload = self._read_json(body)
                message_id = str(payload.get("message_id", ""))
                ok = self.service.replay_outbox(message_id)
                self.service.store.audit(
                    principal.tenant_id,
                    principal.username,
                    "outbox.replay",
                    message_id,
                    {"replayed": ok},
                )
                self._send_json(202 if ok else 404, {"replayed": ok})
                return
            if path == "/v1/evaluation/cases":
                self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.add_evaluation_case(
                    str(payload.get("name", "")),
                    str(payload.get("diff", "")),
                    payload.get("expected_findings", []),
                    str(payload.get("split", "validation")),
                    "api",
                )
                self._send_json(201, result)
                return
            if path == "/v1/evolution/auto":
                self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.auto_propose(
                    str(payload.get("skill_name", "llm-review"))
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self._send_json(201, result)
                return
            if path == "/v1/evolution/propose":
                self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.propose(
                    str(payload.get("skill_name", "")),
                    str(payload.get("prompt", "")),
                    float(payload["regression_score"]) if "regression_score" in payload else None,
                )
                if result["decision"] == "activated":
                    self.service.reload_skills()
                self._send_json(201, result)
                return
            match = ROLLBACK.match(path)
            if match:
                self._principal("manage")
                ok = self.service.evolution.rollback(match.group(1), int(match.group(2)))
                if ok:
                    self.service.reload_skills()
                self._send_json(200 if ok else 404, {"activated": ok})
                return
            self._send_json(404, {"error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except Exception as exc:
            metrics.inc("http_errors_total")
            self._send_json(500, {"error": "operation failed", "detail": str(exc)})


class ReuseportThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that enables ``SO_REUSEPORT`` so several worker
    processes can bind the same port and the kernel load-balances connections
    across them. ``block_on_close`` (default) makes ``server_close()`` join
    in-flight handler threads, giving a graceful drain."""

    allow_reuse_address = True
    daemon_threads = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError as exc:
                # Without SO_REUSEPORT the 2nd+ worker will fail bind with
                # EADDRINUSE; surface it instead of failing opaquely later.
                print("WARNING: could not set SO_REUSEPORT: %s" % exc)
        super().server_bind()


def _make_server(settings: Settings, service: ReviewService) -> ReuseportThreadingHTTPServer:
    handler = type(
        "ConfiguredApiHandler", (ApiHandler,), {"service": service, "settings": settings}
    )
    return ReuseportThreadingHTTPServer((settings.host, settings.port), handler)


def _grace_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("EVOAGENT_SHUTDOWN_GRACE_SECONDS", "0")))
    except ValueError:
        return 0.0


def _install_drain_on_sigterm(server: ThreadingHTTPServer) -> None:
    """On SIGTERM: flip readiness to draining (so the LB stops routing), wait a
    short grace period, then stop accepting. Shutdown runs on its own thread
    because ``shutdown()`` must not be called from the ``serve_forever`` thread."""

    def _handler(_signum: int, _frame: Any) -> None:
        DRAINING.set()

        def _drain() -> None:
            grace = _grace_seconds()
            if grace:
                time.sleep(grace)
            server.shutdown()

        threading.Thread(target=_drain, name="evoagent-drain", daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:  # pragma: no cover - not on main thread (e.g. under tests)
        pass


def _can_multiprocess(settings: Settings) -> bool:
    return (
        settings.web_workers > 1
        and os.name == "posix"
        and hasattr(os, "fork")
        and hasattr(socket, "SO_REUSEPORT")
    )


def _run_worker(settings: Settings) -> None:
    """Child worker: owns its own service (post-fork) and serves until drained.

    Signal handlers were reset to SIG_DFL by the spawner before this runs, so a
    SIGTERM during the (potentially slow) service construction simply terminates
    the not-yet-serving child; once serving, the drain handler takes over."""
    DRAINING.clear()
    service = ReviewService(settings)
    server = _make_server(settings, service)
    _install_drain_on_sigterm(server)
    # Same graceful-drain semantics for Ctrl-C in the worker.
    try:
        signal.signal(signal.SIGINT, signal.getsignal(signal.SIGTERM))
    except (ValueError, OSError, TypeError):  # pragma: no cover
        pass
    try:
        server.serve_forever()
    finally:
        service.close()
        server.server_close()


_TERM_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# Restart storm guard: if more than this many workers die within the window,
# the master gives up instead of fork-looping against a broken dependency.
_MAX_RESTARTS = 10
_RESTART_WINDOW = 60.0


def _run_master(settings: Settings) -> None:
    """Fork ``web_workers`` children (each binds the shared port via SO_REUSEPORT),
    restart crashed workers with backoff, and on SIGTERM forward the signal, wait
    a bounded grace period, then SIGKILL any stragglers."""
    workers = settings.web_workers
    print(
        "EvoAgent: master pid %d supervising %d workers on http://%s:%d"
        % (os.getpid(), workers, settings.host, settings.port)
    )
    children: set[int] = set()
    stopping = threading.Event()
    restart_times: list[float] = []

    def _spawn() -> None:
        # Block term signals across fork+bookkeeping so a signal can neither run
        # the master handler mid-fork nor race the child into `children`.
        blocked = _block_term_signals()
        try:
            pid = os.fork()
        except OSError:
            _restore_signals(blocked)
            raise
        if pid == 0:
            # Child: default disposition during startup, restore mask, then run.
            for sig in _TERM_SIGNALS:
                try:
                    signal.signal(sig, signal.SIG_DFL)
                except (ValueError, OSError):
                    pass
            _restore_signals(blocked)
            try:
                _run_worker(settings)
            finally:
                os._exit(0)
        children.add(pid)
        _restore_signals(blocked)

    def _handle_term(_signum: int, _frame: Any) -> None:
        stopping.set()
        for pid in list(children):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    for sig in _TERM_SIGNALS:
        signal.signal(sig, _handle_term)

    for _ in range(workers):
        _spawn()

    while children and not stopping.is_set():
        try:
            pid, _status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        except InterruptedError:  # pragma: no cover - EINTR retried by CPython
            continue
        children.discard(pid)
        if stopping.is_set():
            break
        now = time.monotonic()
        restart_times[:] = [t for t in restart_times if now - t < _RESTART_WINDOW]
        if len(restart_times) >= _MAX_RESTARTS:
            print("EvoAgent: too many worker restarts; shutting down")
            stopping.set()
            for other in list(children):
                try:
                    os.kill(other, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            break
        restart_times.append(now)
        time.sleep(min(0.1 * 2 ** min(len(restart_times), 6), 5.0))
        print("EvoAgent: worker %d exited; restarting" % pid)
        _spawn()

    _reap_children(children, deadline_seconds=_grace_seconds() + 10.0)


def _block_term_signals():
    if hasattr(signal, "pthread_sigmask"):
        return signal.pthread_sigmask(signal.SIG_BLOCK, set(_TERM_SIGNALS))
    return None


def _restore_signals(previous) -> None:
    if previous is not None and hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _reap_children(children: set[int], deadline_seconds: float) -> None:
    """Wait up to the deadline for children to exit after SIGTERM, then SIGKILL
    and reap any that ignored it, so the master never hangs forever."""
    deadline = time.monotonic() + max(1.0, deadline_seconds)
    while children and time.monotonic() < deadline:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            children.clear()
            return
        if pid == 0:
            time.sleep(0.05)
            continue
        children.discard(pid)
    for pid in list(children):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in list(children):
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        children.discard(pid)


def _print_banner(settings: Settings, service: ReviewService) -> None:
    print("EvoAgent dashboard: http://%s:%d" % (settings.host, settings.port))
    print(
        "Persistence: %s | Queue: %s | Orchestrator: %s"
        % (
            "postgresql" if settings.database_url else "sqlite",
            service.queue.backend,
            service.reviewer.name,
        )
    )
    exposed = settings.host not in {"127.0.0.1", "localhost", "::1"}
    if (settings.database_url or exposed) and not service.queue.durable:
        print(
            "WARNING: this deployment looks production-facing but the task queue is "
            "'%s' (non-durable). Set EVOAGENT_REDIS_URL for durable, crash-safe "
            "delivery; otherwise pending/in-flight/dead-letter tasks are lost on restart."
            % service.queue.backend
        )


def run() -> None:
    settings = Settings.from_env()
    if _can_multiprocess(settings):
        # Banner from a throwaway probe would create a stray service; children
        # print their own readiness. The master just reports supervision.
        _run_master(settings)
        return
    service = ReviewService(settings)
    _print_banner(settings, service)
    server = _make_server(settings, service)
    _install_drain_on_sigterm(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
