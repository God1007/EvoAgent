"""Governed model access with redaction, budgets, and durable usage accounting."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .circuit_breaker import CircuitBreaker
from .metrics import metrics
from .ports import ModelUsageStorePort


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelRequest:
    tenant_id: str
    repository: str
    task_id: str
    purpose: str
    messages: tuple[ModelMessage, ...]
    require_json_object: bool = True
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    request_id: str


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    input_cost_micros_per_million: int = 0
    output_cost_micros_per_million: int = 0


@dataclass(frozen=True)
class ModelGatewayOptions:
    allowed_hosts: tuple[str, ...] = ()
    max_input_tokens: int = 120000
    max_output_tokens: int = 4096
    daily_token_budget: int = 0
    daily_cost_micros: int = 0
    max_response_bytes: int = 2 * 1024 * 1024


class ModelProviderPort(Protocol):
    def complete(
        self,
        route: ModelRoute,
        messages: tuple[ModelMessage, ...],
        max_output_tokens: int,
        require_json_object: bool,
    ) -> ModelResponse: ...


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b(\s*[:=]\s*)(['\"])([^'\"\n]+)(['\"])"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def _redact_text(value: str, explicit_secrets: tuple[str, ...] = ()) -> tuple[str, int]:
    redactions = 0
    content, count = _SECRET_ASSIGNMENT.subn(
        lambda match: (
            "%s%s%s<redacted>%s" % (match.group(1), match.group(2), match.group(3), match.group(5))
        ),
        value,
    )
    redactions += count
    content, count = _BEARER.subn("Bearer <redacted>", content)
    redactions += count
    content, count = _PRIVATE_KEY.subn(
        lambda match: "<redacted-private-key>" + "\n" * match.group(0).count("\n"),
        content,
    )
    redactions += count
    for secret in sorted(
        {item for item in explicit_secrets if len(item) >= 4}, key=len, reverse=True
    ):
        count = content.count(secret)
        if count:
            content = content.replace(secret, "<redacted>")
            redactions += count
    return content, redactions


def redact_model_messages(
    messages: tuple[ModelMessage, ...],
) -> tuple[tuple[ModelMessage, ...], int]:
    """Redact likely credentials while preserving code shape and line numbers."""
    redactions = 0
    output = []
    for message in messages:
        content, count = _redact_text(message.content)
        redactions += count
        output.append(ModelMessage(message.role, content))
    return tuple(output), redactions


def _estimated_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _cost_micros(input_tokens: int, output_tokens: int, route: ModelRoute) -> int:
    numerator = (
        input_tokens * route.input_cost_micros_per_million
        + output_tokens * route.output_cost_micros_per_million
    )
    return (numerator + 999_999) // 1_000_000


def _validated_endpoint(base_url: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urllib.parse.urlparse(base_url.rstrip("/"))
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("model route must be an absolute URL without credentials or query data")
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("model route must use HTTPS (HTTP is allowed only for loopback)")
    allowed = {item.strip().lower() for item in allowed_hosts if item.strip()} or {host}
    if host not in allowed:
        raise ValueError("model route host is not an allowed EVOAGENT_LLM_ALLOWED_HOSTS entry")
    return base_url.rstrip("/") + "/chat/completions"


class OpenAICompatibleModelProvider:
    def __init__(
        self,
        allowed_hosts: tuple[str, ...] = (),
        timeout_seconds: int = 60,
        max_response_bytes: int = 2 * 1024 * 1024,
        breaker: CircuitBreaker | None = None,
    ):
        self.allowed_hosts = allowed_hosts
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.breaker = breaker

    def complete(
        self,
        route: ModelRoute,
        messages: tuple[ModelMessage, ...],
        max_output_tokens: int,
        require_json_object: bool,
    ) -> ModelResponse:
        endpoint = _validated_endpoint(route.base_url, self.allowed_hosts)
        payload: dict[str, Any] = {
            "model": route.model,
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }
        if require_json_object:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": "Bearer " + route.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(route.headers)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = self._open(request)
            with response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ValueError("model response exceeds the configured size limit")
            body = json.loads(raw.decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("model content is not text")
            usage = body.get("usage") or {}
            return ModelResponse(
                content=content,
                provider=route.provider,
                model=route.model,
                input_tokens=max(0, int(usage.get("prompt_tokens", 0) or 0)),
                output_tokens=max(0, int(usage.get("completion_tokens", 0) or 0)),
                request_id=str(body.get("id", "")),
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            detail, _redactions = _redact_text(
                detail,
                (route.api_key, *tuple(route.headers.values())),
            )
            raise RuntimeError(
                "%s API returned HTTP %d: %s" % (route.provider, exc.code, detail)
            ) from exc
        except (
            TimeoutError,
            urllib.error.URLError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise RuntimeError("%s model request failed: %s" % (route.provider, exc)) from exc

    def _open(self, request: urllib.request.Request):
        if self.breaker is None:
            return urllib.request.urlopen(request, timeout=self.timeout_seconds)
        self.breaker.allow()
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            if exc.code in {408, 425, 429} or exc.code >= 500:
                self.breaker.record_failure()
            else:
                # Authentication/validation failures prove the dependency is
                # reachable; opening a transport breaker cannot repair them.
                self.breaker.record_success()
            raise
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return response


class EnterpriseModelGateway:
    """Single-route baseline behind a multi-route-ready enterprise boundary."""

    def __init__(
        self,
        store: ModelUsageStorePort,
        route: ModelRoute | None,
        provider: ModelProviderPort,
        options: ModelGatewayOptions,
    ):
        self.store = store
        self.route = route
        self.provider = provider
        self.options = options
        if route is not None:
            _validated_endpoint(route.base_url, options.allowed_hosts)

    @property
    def configured(self) -> bool:
        return self.route is not None

    def route_info(self) -> dict[str, Any]:
        if self.route is None:
            return {}
        return {"provider": self.route.provider, "model": self.route.model}

    def complete(self, request: ModelRequest) -> ModelResponse:
        route = self.route
        if route is None:
            raise RuntimeError("no model route is configured")
        if not request.tenant_id or not request.repository:
            raise ValueError("model requests require tenant_id and repository")
        messages, redactions = redact_model_messages(request.messages)
        canonical = json.dumps(
            [[item.role, item.content] for item in messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        input_tokens = _estimated_tokens(canonical)
        if input_tokens > self.options.max_input_tokens:
            raise ValueError(
                "model input exceeds the configured limit of %d tokens"
                % self.options.max_input_tokens
            )
        max_output_tokens = min(
            request.max_output_tokens or self.options.max_output_tokens,
            self.options.max_output_tokens,
        )
        if max_output_tokens <= 0:
            raise ValueError("model max_output_tokens must be positive")
        reserved_tokens = input_tokens + max_output_tokens
        reserved_cost = _cost_micros(input_tokens, max_output_tokens, route)
        request_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        if not self.store.reserve_model_usage(
            {
                "request_id": request_id,
                "tenant_id": request.tenant_id,
                "repository": request.repository,
                "task_id": request.task_id or None,
                "purpose": request.purpose,
                "provider": route.provider,
                "model": route.model,
                "reserved_tokens": reserved_tokens,
                "reserved_cost_micros": reserved_cost,
                "redactions": redactions,
                "request_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "created_at": now.isoformat(),
            },
            now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            self.options.daily_token_budget,
            self.options.daily_cost_micros,
        ):
            metrics.inc("model_budget_rejections_total")
            raise PermissionError("model usage budget is exhausted for this repository")
        actual_input = 0
        actual_output = 0
        cost = 0
        try:
            response = self.provider.complete(
                route,
                messages,
                max_output_tokens,
                request.require_json_object,
            )
            actual_input = response.input_tokens or input_tokens
            actual_output = response.output_tokens or _estimated_tokens(response.content)
            cost = _cost_micros(actual_input, actual_output, route)
            if actual_output > max_output_tokens:
                raise ValueError("model response exceeds the configured output-token limit")
            if request.require_json_object:
                parsed = json.loads(response.content)
                if not isinstance(parsed, dict):
                    raise ValueError("model response must be a JSON object")
            if not self.store.complete_model_usage(
                request_id, "success", actual_input, actual_output, cost
            ):
                raise RuntimeError("model usage reservation was lost before completion")
        except Exception as exc:
            safe_error, _redactions = _redact_text(
                str(exc),
                (route.api_key, *tuple(route.headers.values())),
            )
            self.store.complete_model_usage(
                request_id,
                "failed",
                actual_input,
                actual_output,
                cost,
                safe_error,
            )
            metrics.inc("model_requests_failed_total")
            if safe_error != str(exc):
                raise RuntimeError(safe_error) from None
            raise
        metrics.inc("model_requests_total")
        if redactions:
            metrics.inc("model_redactions_total", redactions)
        return ModelResponse(
            response.content,
            route.provider,
            route.model,
            actual_input,
            actual_output,
            response.request_id or request_id,
        )
