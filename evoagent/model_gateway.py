"""Governed model access with redaction, budgets, and durable usage accounting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any, Protocol

from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .metrics import metrics
from .ports import ModelUsageStorePort


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelGovernanceContext:
    tenant_id: str
    repository: str
    allowed_providers: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    required_region: str = ""


@dataclass(frozen=True)
class ModelRequest:
    tenant_id: str
    repository: str
    task_id: str
    purpose: str
    messages: tuple[ModelMessage, ...]
    require_json_object: bool = True
    max_output_tokens: int | None = None
    allowed_providers: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    required_region: str = ""


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
    route_id: str = ""
    priority: int = 100
    region: str = ""
    tenant_ids: tuple[str, ...] = ()
    repository_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelGatewayOptions:
    allowed_hosts: tuple[str, ...] = ()
    max_input_tokens: int = 120000
    max_output_tokens: int = 4096
    daily_token_budget: int = 0
    daily_cost_micros: int = 0
    max_response_bytes: int = 2 * 1024 * 1024
    fallback_attempts: int = 1


class ModelProviderPort(Protocol):
    def complete(
        self,
        route: ModelRoute,
        messages: tuple[ModelMessage, ...],
        max_output_tokens: int,
        require_json_object: bool,
    ) -> ModelResponse: ...


class ModelProviderError(RuntimeError):
    """Provider failure classified for safe fallback decisions."""

    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


class ModelOutputError(RuntimeError):
    """A provider responded, but the response violated the gateway contract."""


_ROUTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_ROUTE_FIELDS = frozenset(
    {
        "id",
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "priority",
        "region",
        "tenant_ids",
        "repository_patterns",
        "input_cost_micros_per_million",
        "output_cost_micros_per_million",
    }
)


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


def _route_key(route: ModelRoute) -> str:
    return route.route_id or "%s-%s" % (route.provider, route.model)


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("model route %s must be a list of strings" % field_name)
    normalized = tuple(item.strip() for item in value)
    if any(not item or len(item) > 250 for item in normalized):
        raise ValueError("model route %s contains an invalid entry" % field_name)
    if len(normalized) != len(set(normalized)):
        raise ValueError("model route %s contains duplicates" % field_name)
    return normalized


def _non_negative_route_int(value: Any, field_name: str, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("model route %s must be a non-negative integer" % field_name)
    return value


def load_model_routes(path: str) -> tuple[ModelRoute, ...]:
    """Load a trusted v1 route topology while resolving secrets only from env."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ValueError("cannot read EVOAGENT_LLM_ROUTES_FILE: %s" % exc) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("EVOAGENT_LLM_ROUTES_FILE is not valid TOML: %s" % exc) from exc
    if set(document).difference({"version", "routes"}):
        raise ValueError("model routes file contains unsupported top-level fields")
    if document.get("version") != 1:
        raise ValueError("model routes file version must be 1")
    raw_routes = document.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes or len(raw_routes) > 20:
        raise ValueError("model routes file must define between 1 and 20 routes")
    routes = []
    seen = set()
    for raw in raw_routes:
        if not isinstance(raw, dict):
            raise ValueError("each model route must be a TOML table")
        unknown = set(raw).difference(_ROUTE_FIELDS)
        if unknown:
            raise ValueError("model route contains unsupported fields: %s" % ", ".join(unknown))
        route_id = raw.get("id")
        if not isinstance(route_id, str) or not _ROUTE_ID.fullmatch(route_id):
            raise ValueError("model route id is required and must be a stable identifier")
        if route_id in seen:
            raise ValueError("duplicate model route id: %s" % route_id)
        seen.add(route_id)
        api_key_env = raw.get("api_key_env")
        if not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env):
            raise ValueError("model route api_key_env must name an uppercase environment variable")
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise ValueError(
                "model route %s requires environment variable %s" % (route_id, api_key_env)
            )
        required = {}
        for name in ("provider", "model", "base_url"):
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("model route %s requires %s" % (route_id, name))
            required[name] = value.strip()
        region = raw.get("region", "")
        if not isinstance(region, str) or len(region) > 100:
            raise ValueError("model route region must be a string of at most 100 characters")
        routes.append(
            ModelRoute(
                provider=required["provider"],
                model=required["model"],
                base_url=required["base_url"],
                api_key=api_key,
                input_cost_micros_per_million=_non_negative_route_int(
                    raw.get("input_cost_micros_per_million"),
                    "input_cost_micros_per_million",
                ),
                output_cost_micros_per_million=_non_negative_route_int(
                    raw.get("output_cost_micros_per_million"),
                    "output_cost_micros_per_million",
                ),
                route_id=route_id,
                priority=_non_negative_route_int(raw.get("priority"), "priority", 100),
                region=region.strip(),
                tenant_ids=_string_list(raw.get("tenant_ids"), "tenant_ids"),
                repository_patterns=_string_list(
                    raw.get("repository_patterns"), "repository_patterns"
                ),
            )
        )
    return tuple(sorted(routes, key=lambda item: (item.priority, item.route_id)))


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
            raise ModelProviderError(
                "%s API returned HTTP %d: %s" % (route.provider, exc.code, detail),
                transient=exc.code in {408, 425, 429} or exc.code >= 500,
            ) from exc
        except (
            TimeoutError,
            urllib.error.URLError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise ModelProviderError(
                "%s model request failed: %s" % (route.provider, exc),
                transient=True,
            ) from exc

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
    """Policy-filtered multi-route gateway with a bounded fallback budget."""

    def __init__(
        self,
        store: ModelUsageStorePort,
        route: ModelRoute | tuple[ModelRoute, ...] | None,
        provider: ModelProviderPort | Mapping[str, ModelProviderPort],
        options: ModelGatewayOptions,
    ):
        self.store = store
        self.routes = (
            tuple(sorted(route, key=lambda item: (item.priority, _route_key(item))))
            if isinstance(route, tuple)
            else ((route,) if route is not None else ())
        )
        self.route = self.routes[0] if self.routes else None
        self.options = options
        keys = [_route_key(item) for item in self.routes]
        if len(keys) != len(set(keys)):
            raise ValueError("model route ids must be unique")
        for item in self.routes:
            _validated_endpoint(item.base_url, options.allowed_hosts)
        if isinstance(provider, Mapping):
            missing = set(keys).difference(provider)
            if missing:
                raise ValueError(
                    "model providers are missing routes: %s" % ", ".join(sorted(missing))
                )
            self.providers = dict(provider)
        else:
            self.providers = {key: provider for key in keys}
        self.provider = provider

    @property
    def configured(self) -> bool:
        return bool(self.routes)

    def route_info(self) -> dict[str, Any]:
        if self.route is None:
            return {}
        return {
            "provider": self.route.provider,
            "model": self.route.model,
            "route_id": _route_key(self.route),
            "region": self.route.region,
            "route_count": len(self.routes),
            "routes": [
                {
                    "route_id": _route_key(item),
                    "provider": item.provider,
                    "model": item.model,
                    "region": item.region,
                }
                for item in self.routes
            ],
        }

    def route_catalog(self, tenant_id: str, repository: str) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "route_id": _route_key(route),
                "provider": route.provider,
                "model": route.model,
                "region": route.region,
            }
            for route in self.routes
            if (not route.tenant_ids or tenant_id in route.tenant_ids)
            and (
                not route.repository_patterns
                or any(fnmatchcase(repository, pattern) for pattern in route.repository_patterns)
            )
        )

    def breaker_state_code(self) -> int:
        states = []
        for provider in self.providers.values():
            breaker = getattr(provider, "breaker", None)
            if breaker is not None:
                states.append(int(breaker.state_code()))
        return max(states, default=0)

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.routes:
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
        candidates = self._candidate_routes(request)
        if not candidates:
            metrics.inc("model_route_rejections_total")
            raise PermissionError("no model route satisfies the request governance policy")
        attempt_limit = min(len(candidates), max(1, self.options.fallback_attempts + 1))
        root_request_id = uuid.uuid4().hex
        last_error: Exception | None = None
        for attempt, selected in enumerate(candidates[:attempt_limit], start=1):
            try:
                return self._complete_route(
                    request,
                    selected,
                    messages,
                    redactions,
                    canonical,
                    input_tokens,
                    max_output_tokens,
                    root_request_id,
                    attempt,
                )
            except Exception as exc:
                last_error = exc
                if attempt >= attempt_limit or not self._fallback_allowed(exc):
                    raise
                metrics.inc("model_fallback_attempts_total")
        if last_error is not None:  # pragma: no cover - defensive loop invariant
            raise last_error
        raise RuntimeError("model gateway has no executable route")  # pragma: no cover

    def _candidate_routes(self, request: ModelRequest) -> list[ModelRoute]:
        candidates = []
        for route in self.routes:
            if route.tenant_ids and request.tenant_id not in route.tenant_ids:
                continue
            if route.repository_patterns and not any(
                fnmatchcase(request.repository, pattern) for pattern in route.repository_patterns
            ):
                continue
            if request.required_region and route.region != request.required_region:
                continue
            if request.allowed_providers and route.provider not in request.allowed_providers:
                continue
            if request.allowed_models and route.model not in request.allowed_models:
                continue
            candidates.append(route)
        return candidates

    @staticmethod
    def _fallback_allowed(exc: Exception) -> bool:
        if isinstance(exc, ModelProviderError):
            return exc.transient
        return isinstance(exc, (CircuitOpenError, ModelOutputError, PermissionError))

    def _complete_route(
        self,
        request: ModelRequest,
        route: ModelRoute,
        messages: tuple[ModelMessage, ...],
        redactions: int,
        canonical: str,
        input_tokens: int,
        max_output_tokens: int,
        root_request_id: str,
        attempt: int,
    ) -> ModelResponse:
        reserved_tokens = input_tokens + max_output_tokens
        reserved_cost = _cost_micros(input_tokens, max_output_tokens, route)
        request_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        if not self.store.reserve_model_usage(
            {
                "request_id": request_id,
                "root_request_id": root_request_id,
                "attempt": attempt,
                "route_id": _route_key(route),
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
            response = self.providers[_route_key(route)].complete(
                route,
                messages,
                max_output_tokens,
                request.require_json_object,
            )
            actual_input = response.input_tokens or input_tokens
            actual_output = response.output_tokens or _estimated_tokens(response.content)
            cost = _cost_micros(actual_input, actual_output, route)
            if actual_output > max_output_tokens:
                raise ModelOutputError("model response exceeds the configured output-token limit")
            if request.require_json_object:
                try:
                    parsed = json.loads(response.content)
                except json.JSONDecodeError as exc:
                    raise ModelOutputError("model response is not valid JSON") from exc
                if not isinstance(parsed, dict):
                    raise ModelOutputError("model response must be a JSON object")
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
                raise ModelProviderError(
                    safe_error,
                    transient=bool(isinstance(exc, ModelProviderError) and exc.transient),
                ) from None
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
