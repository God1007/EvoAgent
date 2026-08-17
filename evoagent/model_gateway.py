"""Governed model access with redaction, budgets, and durable usage accounting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from functools import partial
from typing import Any, Protocol

from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .errors import AccessDeniedError, ClientInputError, safe_exception_fields
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
    state: str = "active"
    weight: int = 0
    shadow_percent: int = 0
    baseline_route_id: str = ""
    min_shadow_samples: int = 100
    max_shadow_error_rate: float = 0.05
    max_shadow_disagreement_rate: float = 0.20
    evaluation_dataset_sha256: str = ""
    evaluation_report_sha256: str = ""
    topology_sha256: str = ""
    config_version: int = 1
    capacity_max_inflight: int = 0
    capacity_requests_per_minute: int = 0


@dataclass(frozen=True)
class ModelGatewayOptions:
    allowed_hosts: tuple[str, ...] = ()
    max_input_tokens: int = 120000
    max_output_tokens: int = 4096
    daily_token_budget: int = 0
    daily_cost_micros: int = 0
    max_response_bytes: int = 2 * 1024 * 1024
    fallback_attempts: int = 1
    reservation_ttl_seconds: int = 600
    reservation_sweep_limit: int = 1000
    shadow_workers: int = 2
    shadow_max_inflight: int = 8
    shadow_shutdown_timeout_seconds: float = 5.0
    shadow_daily_token_budget: int = 0
    shadow_daily_cost_micros: int = 0
    capacity_lease_seconds: int = 180
    capacity_window_retention_hours: int = 48


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


class ModelRouteCapacityError(RuntimeError):
    """A shared route bulkhead rejected work before provider/budget use."""

    def __init__(self, route_id: str, reason: str, retry_at: str | None = None):
        super().__init__("model route capacity is exhausted")
        self.route_id = route_id
        self.reason = reason
        self.retry_at = retry_at


_ROUTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_V2_ROUTE_FIELDS = frozenset(
    {
        "state",
        "weight",
        "shadow_percent",
        "baseline_route_id",
        "min_shadow_samples",
        "max_shadow_error_rate",
        "max_shadow_disagreement_rate",
        "evaluation_dataset_sha256",
        "evaluation_report_sha256",
        "capacity_max_inflight",
        "capacity_requests_per_minute",
    }
)
_CANDIDATE_ROUTE_FIELDS = frozenset(
    {
        "shadow_percent",
        "baseline_route_id",
        "min_shadow_samples",
        "max_shadow_error_rate",
        "max_shadow_disagreement_rate",
        "evaluation_dataset_sha256",
        "evaluation_report_sha256",
    }
)
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
        *_V2_ROUTE_FIELDS,
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


def _route_rate(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("model route %s must be a number between 0 and 1" % field_name)
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise ValueError("model route %s must be between 0 and 1" % field_name)
    return normalized


def _optional_sha256(value: Any, field_name: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("model route %s must be a lowercase SHA-256 digest" % field_name)
    return value


def load_model_routes(path: str) -> tuple[ModelRoute, ...]:
    """Load a trusted v1/v2 topology while resolving secrets only from env."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ValueError("cannot read EVOAGENT_LLM_ROUTES_FILE: %s" % exc) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("EVOAGENT_LLM_ROUTES_FILE is not valid TOML: %s" % exc) from exc
    if set(document).difference({"version", "routes"}):
        raise ValueError("model routes file contains unsupported top-level fields")
    version = document.get("version")
    if version not in {1, 2}:
        raise ValueError("model routes file version must be 1 or 2")
    topology_sha256 = hashlib.sha256(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
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
        if version == 1 and set(raw).intersection(_V2_ROUTE_FIELDS):
            raise ValueError("model route v2 governance fields require file version 2")
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
        state = raw.get("state", "active")
        if not isinstance(state, str) or state not in {"active", "candidate", "disabled"}:
            raise ValueError("model route state must be active, candidate, or disabled")
        weight = _non_negative_route_int(
            raw.get("weight"),
            "weight",
            0 if version == 2 and state == "candidate" else (1 if version == 2 else 0),
        )
        shadow_percent = _non_negative_route_int(raw.get("shadow_percent"), "shadow_percent", 0)
        baseline_route_id = raw.get("baseline_route_id", "")
        if not isinstance(baseline_route_id, str) or (
            baseline_route_id and not _ROUTE_ID.fullmatch(baseline_route_id)
        ):
            raise ValueError("model route baseline_route_id must be a stable route identifier")
        min_shadow_samples = _non_negative_route_int(
            raw.get("min_shadow_samples"), "min_shadow_samples", 100
        )
        evaluation_dataset_sha256 = _optional_sha256(
            raw.get("evaluation_dataset_sha256"), "evaluation_dataset_sha256"
        )
        evaluation_report_sha256 = _optional_sha256(
            raw.get("evaluation_report_sha256"), "evaluation_report_sha256"
        )
        if bool(evaluation_dataset_sha256) != bool(evaluation_report_sha256):
            raise ValueError("model route promotion evidence requires both SHA-256 digests")
        capacity_max_inflight = _non_negative_route_int(
            raw.get("capacity_max_inflight"), "capacity_max_inflight"
        )
        capacity_requests_per_minute = _non_negative_route_int(
            raw.get("capacity_requests_per_minute"), "capacity_requests_per_minute"
        )
        if capacity_max_inflight > 100_000:
            raise ValueError("model route capacity_max_inflight must be at most 100000")
        if capacity_requests_per_minute > 10_000_000:
            raise ValueError("model route capacity_requests_per_minute must be at most 10000000")
        if version == 2 and state == "active" and not 1 <= weight <= 10_000:
            raise ValueError("active model route weight must be between 1 and 10000")
        if state == "candidate":
            if weight != 0:
                raise ValueError("candidate model route weight must be 0")
            if not 1 <= shadow_percent <= 100:
                raise ValueError("candidate model route shadow_percent must be between 1 and 100")
            if not baseline_route_id:
                raise ValueError("candidate model route requires baseline_route_id")
            if min_shadow_samples <= 0:
                raise ValueError("candidate model route min_shadow_samples must be positive")
        elif set(raw).intersection(_CANDIDATE_ROUTE_FIELDS):
            raise ValueError("candidate governance fields require state=candidate")
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
                state=state,
                weight=weight,
                shadow_percent=shadow_percent,
                baseline_route_id=baseline_route_id,
                min_shadow_samples=min_shadow_samples,
                max_shadow_error_rate=_route_rate(
                    raw.get("max_shadow_error_rate"), "max_shadow_error_rate", 0.05
                ),
                max_shadow_disagreement_rate=_route_rate(
                    raw.get("max_shadow_disagreement_rate"),
                    "max_shadow_disagreement_rate",
                    0.20,
                ),
                evaluation_dataset_sha256=evaluation_dataset_sha256,
                evaluation_report_sha256=evaluation_report_sha256,
                topology_sha256=topology_sha256,
                config_version=version,
                capacity_max_inflight=capacity_max_inflight,
                capacity_requests_per_minute=capacity_requests_per_minute,
            )
        )
    active_ids = {_route_key(route) for route in routes if route.state == "active"}
    if not active_ids:
        raise ValueError("model routes file must define at least one active route")
    for route in routes:
        if route.state == "candidate" and route.baseline_route_id not in active_ids:
            raise ValueError(
                "candidate model route %s baseline must reference an active route"
                % _route_key(route)
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
    """Policy-filtered routing with bounded fallback and isolated shadow traffic."""

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
        self.active_routes = tuple(item for item in self.routes if item.state == "active")
        self.candidate_routes = tuple(item for item in self.routes if item.state == "candidate")
        if self.routes and not self.active_routes:
            raise ValueError("model gateway requires at least one active route")
        self.route = self.active_routes[0] if self.active_routes else None
        self.options = options
        if options.reservation_ttl_seconds <= 0:
            raise ValueError("model reservation TTL must be positive")
        if not 1 <= options.reservation_sweep_limit <= 10_000:
            raise ValueError("model reservation sweep limit must be between 1 and 10000")
        if not 0 <= options.shadow_workers <= 32:
            raise ValueError("model shadow workers must be between 0 and 32")
        if not 1 <= options.shadow_max_inflight <= 10_000:
            raise ValueError("model shadow max inflight must be between 1 and 10000")
        if options.shadow_shutdown_timeout_seconds < 0:
            raise ValueError("model shadow shutdown timeout must be non-negative")
        if options.shadow_daily_token_budget < 0 or options.shadow_daily_cost_micros < 0:
            raise ValueError("model shadow budgets must be non-negative")
        if options.capacity_lease_seconds <= 0:
            raise ValueError("model route capacity lease must be positive")
        if not 1 <= options.capacity_window_retention_hours <= 24 * 30:
            raise ValueError(
                "model route capacity window retention must be between 1 and 720 hours"
            )
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
        topology_hashes = {item.topology_sha256 for item in self.routes if item.topology_sha256}
        if len(topology_hashes) > 1:
            raise ValueError("model routes must belong to one topology")
        if topology_hashes and any(not item.topology_sha256 for item in self.routes):
            raise ValueError("model routes cannot mix versioned and runtime topology entries")
        self.topology_sha256 = next(iter(topology_hashes), self._runtime_topology_sha256())
        self._maintenance_lock = threading.Lock()
        self._next_maintenance_at = 0.0
        self._shadow_slots = threading.BoundedSemaphore(options.shadow_max_inflight)
        self._shadow_condition = threading.Condition()
        self._shadow_futures: set[Future[None]] = set()
        self._shadow_closed = False
        self._shadow_executor: ThreadPoolExecutor | None = None
        self.expire_stale_reservations(force=True)
        self._shadow_executor = (
            ThreadPoolExecutor(
                max_workers=options.shadow_workers,
                thread_name_prefix="evoagent-model-shadow",
            )
            if self.candidate_routes and options.shadow_workers
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self.active_routes)

    def route_info(self) -> dict[str, Any]:
        if self.route is None:
            return {}
        return {
            "provider": self.route.provider,
            "model": self.route.model,
            "route_id": _route_key(self.route),
            "region": self.route.region,
            "route_count": len(self.routes),
            "active_route_count": len(self.active_routes),
            "candidate_route_count": len(self.candidate_routes),
            "topology_sha256": self.topology_sha256,
            "routes": [
                {
                    "route_id": _route_key(item),
                    "provider": item.provider,
                    "model": item.model,
                    "region": item.region,
                    "state": item.state,
                    "weight": item.weight,
                    "shadow_percent": item.shadow_percent,
                    "baseline_route_id": item.baseline_route_id,
                    "capacity_max_inflight": item.capacity_max_inflight,
                    "capacity_requests_per_minute": item.capacity_requests_per_minute,
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
            if route.state == "active"
            if (not route.tenant_ids or tenant_id in route.tenant_ids)
            and (
                not route.repository_patterns
                or any(fnmatchcase(repository, pattern) for pattern in route.repository_patterns)
            )
        )

    def breaker_state_code(self) -> int:
        states = []
        for route in self.active_routes:
            provider = self.providers[_route_key(route)]
            breaker = getattr(provider, "breaker", None)
            if breaker is not None:
                states.append(int(breaker.state_code()))
        return max(states, default=0)

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.expire_stale_reservations()
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
            raise ClientInputError(
                "model input exceeds the configured limit of %d tokens"
                % self.options.max_input_tokens
            )
        max_output_tokens = min(
            request.max_output_tokens or self.options.max_output_tokens,
            self.options.max_output_tokens,
        )
        if max_output_tokens <= 0:
            raise ValueError("model max_output_tokens must be positive")
        request_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        candidates = self._candidate_routes(request, request_sha256)
        if not candidates:
            metrics.inc("model_route_rejections_total")
            raise AccessDeniedError("no model route satisfies the request governance policy")
        attempt_limit = min(len(candidates), max(1, self.options.fallback_attempts + 1))
        root_request_id = uuid.uuid4().hex
        last_error: Exception | None = None
        for attempt, selected in enumerate(candidates[:attempt_limit], start=1):
            try:
                response = self._complete_route(
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
                try:
                    self._dispatch_shadows(
                        request,
                        selected,
                        response,
                        messages,
                        redactions,
                        canonical,
                        request_sha256,
                        input_tokens,
                        max_output_tokens,
                        root_request_id,
                    )
                except Exception:
                    # Shadow instrumentation is never allowed to turn a
                    # completed active model call into a client-visible failure.
                    metrics.inc("model_shadow_dispatch_failures_total")
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= attempt_limit or not self._fallback_allowed(exc):
                    raise
                metrics.inc("model_fallback_attempts_total")
        if last_error is not None:  # pragma: no cover - defensive loop invariant
            raise last_error
        raise RuntimeError("model gateway has no executable route")  # pragma: no cover

    def expire_stale_reservations(self, *, force: bool = False) -> int:
        """Quarantine stale calls at startup and periodically without releasing budget."""
        now_monotonic = time.monotonic()
        if not force and now_monotonic < self._next_maintenance_at:
            return 0
        with self._maintenance_lock:
            now_monotonic = time.monotonic()
            if not force and now_monotonic < self._next_maintenance_at:
                return 0
            cutoff = datetime.now(UTC) - timedelta(seconds=self.options.reservation_ttl_seconds)
            expired = self.store.expire_model_usage_reservations(
                cutoff.isoformat(), self.options.reservation_sweep_limit
            )
            expired_shadows = self.store.expire_model_route_shadows(
                cutoff.isoformat(), self.options.reservation_sweep_limit
            )
            interval = max(1.0, min(60.0, self.options.reservation_ttl_seconds / 4.0))
            self._next_maintenance_at = now_monotonic + interval
        if expired:
            metrics.inc("model_reservations_expired_total", expired)
        if expired_shadows:
            metrics.inc("model_shadow_observations_expired_total", expired_shadows)
        return expired

    def _candidate_routes(
        self, request: ModelRequest, request_sha256: str = ""
    ) -> list[ModelRoute]:
        candidates = []
        for route in self.active_routes:
            if self._route_matches(route, request):
                candidates.append(route)
        routing_key = "\0".join(
            (
                request.tenant_id,
                request.repository,
                request.task_id,
                request.purpose,
                request_sha256,
            )
        )
        return self._weighted_order(candidates, routing_key)

    @staticmethod
    def _route_matches(route: ModelRoute, request: ModelRequest) -> bool:
        if route.tenant_ids and request.tenant_id not in route.tenant_ids:
            return False
        if route.repository_patterns and not any(
            fnmatchcase(request.repository, pattern) for pattern in route.repository_patterns
        ):
            return False
        if request.required_region and route.region != request.required_region:
            return False
        if request.allowed_providers and route.provider not in request.allowed_providers:
            return False
        if request.allowed_models and route.model not in request.allowed_models:
            return False
        return True

    @staticmethod
    def _weighted_order(routes: list[ModelRoute], routing_key: str) -> list[ModelRoute]:
        if not routes or all(route.weight <= 0 for route in routes):
            return routes
        ordered: list[ModelRoute] = []
        priorities = sorted({route.priority for route in routes})
        for priority in priorities:
            tier = [route for route in routes if route.priority == priority]

            def score(route: ModelRoute) -> tuple[float, str]:
                digest = hashlib.sha256(
                    (routing_key + "\0" + _route_key(route)).encode("utf-8")
                ).digest()
                uniform = (int.from_bytes(digest, "big") + 1) / ((1 << 256) + 1)
                return (-math.log(uniform) / max(1, route.weight), _route_key(route))

            ordered.extend(sorted(tier, key=score))
        return ordered

    def promotion_report(
        self, tenant_id: str, candidate_route_id: str, repository: str | None = None
    ) -> dict[str, Any]:
        if not tenant_id:
            raise ClientInputError("model route promotion report requires tenant_id")
        route = next(
            (
                item
                for item in self.candidate_routes
                if _route_key(item) == candidate_route_id
                and (not item.tenant_ids or tenant_id in item.tenant_ids)
            ),
            None,
        )
        if route is None:
            raise ClientInputError("candidate model route was not found for this tenant")
        if (
            repository is not None
            and route.repository_patterns
            and not any(fnmatchcase(repository, pattern) for pattern in route.repository_patterns)
        ):
            raise ClientInputError("candidate model route does not match this repository")
        stats = self.store.model_route_shadow_stats(
            tenant_id, candidate_route_id, self.topology_sha256, repository
        )
        completed = stats["samples"] + stats["errors"]
        error_rate = stats["errors"] / completed if completed else 0.0
        disagreement_rate = stats["disagreements"] / stats["samples"] if stats["samples"] else 0.0
        evidence_ready = bool(route.evaluation_dataset_sha256 and route.evaluation_report_sha256)
        checks = {
            "minimum_samples": stats["samples"] >= route.min_shadow_samples,
            "maximum_error_rate": error_rate <= route.max_shadow_error_rate,
            "maximum_disagreement_rate": (disagreement_rate <= route.max_shadow_disagreement_rate),
            "no_pending_observations": stats["pending"] == 0,
            "offline_quality_evidence": evidence_ready,
        }
        return {
            "candidate_route_id": candidate_route_id,
            "baseline_route_id": route.baseline_route_id,
            "topology_sha256": self.topology_sha256,
            "scope": {"tenant_id": tenant_id, "repository": repository},
            "eligible": all(checks.values()),
            "checks": checks,
            "thresholds": {
                "min_shadow_samples": route.min_shadow_samples,
                "max_shadow_error_rate": route.max_shadow_error_rate,
                "max_shadow_disagreement_rate": route.max_shadow_disagreement_rate,
            },
            "observations": {
                **stats,
                "error_rate": error_rate,
                "disagreement_rate": disagreement_rate,
            },
            "evidence": {
                "evaluation_dataset_sha256": route.evaluation_dataset_sha256 or None,
                "evaluation_report_sha256": route.evaluation_report_sha256 or None,
            },
            "activation": (
                "validate every intended tenant/repository scope, then change route state "
                "in reviewed topology and redeploy"
            ),
        }

    def capacity_report(self, tenant_id: str, repository: str | None = None) -> dict[str, Any]:
        if not tenant_id:
            raise ClientInputError("model route capacity report requires tenant_id")
        routes = [
            route
            for route in self.routes
            if route.state != "disabled"
            and (not route.tenant_ids or tenant_id in route.tenant_ids)
            and (
                repository is None
                or not route.repository_patterns
                or any(fnmatchcase(repository, pattern) for pattern in route.repository_patterns)
            )
        ]
        recommendations = self._capacity_weight_recommendations(routes)
        now = datetime.now(UTC)
        window_start = now.replace(second=0, microsecond=0)
        values = []
        for route in routes:
            route_id = _route_key(route)
            stats = self.store.model_route_capacity_stats(
                route_id,
                now.isoformat(),
                window_start.isoformat(),
            )
            provider = self.providers[route_id]
            breaker = getattr(provider, "breaker", None)
            breaker_state = getattr(breaker, "state", "closed")
            concurrency_available = (
                not route.capacity_max_inflight
                or stats["active_inflight"] < route.capacity_max_inflight
            )
            rate_available = (
                not route.capacity_requests_per_minute
                or stats["admitted_this_minute"] < route.capacity_requests_per_minute
            )
            tenant_exclusive = route.tenant_ids == (tenant_id,)
            visible_stats = (
                stats
                if tenant_exclusive
                else {
                    "active_inflight": None,
                    "earliest_expiry": None,
                    "admitted_this_minute": None,
                    "concurrency_rejections_this_minute": None,
                    "rate_rejections_this_minute": None,
                }
            )
            recommendation = recommendations.get(route_id)
            values.append(
                {
                    "route_id": route_id,
                    "state": route.state,
                    "priority": route.priority,
                    "configured_weight": route.weight,
                    "recommended_weight": recommendation[0] if recommendation else None,
                    "recommendation_basis": recommendation[1] if recommendation else None,
                    "capacity_max_inflight": route.capacity_max_inflight,
                    "capacity_requests_per_minute": route.capacity_requests_per_minute,
                    "breaker_state": breaker_state,
                    "observation_scope": (
                        "tenant-exclusive" if tenant_exclusive else "shared-redacted"
                    ),
                    "available": (
                        route.state == "active"
                        and concurrency_available
                        and rate_available
                        and breaker_state != CircuitBreaker.OPEN
                    ),
                    **visible_stats,
                }
            )
        return {
            "topology_sha256": self.topology_sha256,
            "scope": {"tenant_id": tenant_id, "repository": repository},
            "window_start": window_start.isoformat(),
            "routes": values,
            "activation": "review weight recommendations, change topology, and redeploy",
        }

    @staticmethod
    def _capacity_weight_recommendations(
        routes: list[ModelRoute],
    ) -> dict[str, tuple[int, str]]:
        recommendations: dict[str, tuple[int, str]] = {}
        active = [route for route in routes if route.state == "active"]
        for priority in sorted({route.priority for route in active}):
            tier = [route for route in active if route.priority == priority]
            if all(route.capacity_requests_per_minute > 0 for route in tier):
                basis = "requests_per_minute"
                capacities = [route.capacity_requests_per_minute for route in tier]
            elif all(route.capacity_max_inflight > 0 for route in tier):
                basis = "max_inflight"
                capacities = [route.capacity_max_inflight for route in tier]
            else:
                continue
            divisor = capacities[0]
            for capacity in capacities[1:]:
                divisor = math.gcd(divisor, capacity)
            normalized = [capacity // max(1, divisor) for capacity in capacities]
            if max(normalized) > 10_000:
                scale = max(normalized) / 10_000
                normalized = [max(1, round(value / scale)) for value in normalized]
            for route, weight in zip(tier, normalized, strict=True):
                recommendations[_route_key(route)] = (weight, basis)
        return recommendations

    @staticmethod
    def _fallback_allowed(exc: Exception) -> bool:
        if isinstance(exc, ModelProviderError):
            return exc.transient
        return isinstance(
            exc,
            (CircuitOpenError, ModelOutputError, ModelRouteCapacityError, PermissionError),
        )

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
        lane: str = "active",
    ) -> ModelResponse:
        lease_id = ""
        if route.capacity_max_inflight or route.capacity_requests_per_minute:
            now = datetime.now(UTC)
            window_start = now.replace(second=0, microsecond=0)
            capacity = self.store.acquire_model_route_capacity(
                {
                    "lease_id": uuid.uuid4().hex,
                    "topology_sha256": self.topology_sha256,
                    "route_id": _route_key(route),
                    "root_request_id": root_request_id,
                    "now": now.isoformat(),
                    "expires_at": (
                        now + timedelta(seconds=self.options.capacity_lease_seconds)
                    ).isoformat(),
                    "window_start": window_start.isoformat(),
                    "window_end": (window_start + timedelta(minutes=1)).isoformat(),
                    "retention_cutoff": (
                        now - timedelta(hours=self.options.capacity_window_retention_hours)
                    ).isoformat(),
                },
                route.capacity_max_inflight,
                route.capacity_requests_per_minute,
            )
            if not capacity["admitted"]:
                metrics.inc("model_route_capacity_rejections_total")
                raise ModelRouteCapacityError(
                    _route_key(route),
                    str(capacity["reason"]),
                    capacity.get("retry_at"),
                )
            lease_id = str(capacity.get("lease_id") or "")
        try:
            return self._complete_admitted_route(
                request,
                route,
                messages,
                redactions,
                canonical,
                input_tokens,
                max_output_tokens,
                root_request_id,
                attempt,
                lane,
            )
        finally:
            if lease_id:
                try:
                    self.store.release_model_route_capacity(lease_id)
                except Exception:
                    # The lease is time-bounded; a release outage must not turn
                    # a completed provider call into a client-visible failure.
                    metrics.inc("model_route_capacity_release_failures_total")

    def _complete_admitted_route(
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
        lane: str = "active",
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
                "lane": lane,
                "topology_sha256": self.topology_sha256,
                "created_at": now.isoformat(),
            },
            now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            self.options.daily_token_budget,
            self.options.daily_cost_micros,
            self.options.shadow_daily_token_budget if lane == "shadow" else 0,
            self.options.shadow_daily_cost_micros if lane == "shadow" else 0,
        ):
            metrics.inc("model_budget_rejections_total")
            raise AccessDeniedError("model usage budget is exhausted for this repository")
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

    def _runtime_topology_sha256(self) -> str:
        topology = [
            {
                "route_id": _route_key(route),
                "provider": route.provider,
                "model": route.model,
                "base_url": route.base_url,
                "priority": route.priority,
                "region": route.region,
                "tenant_ids": route.tenant_ids,
                "repository_patterns": route.repository_patterns,
                "state": route.state,
                "weight": route.weight,
                "shadow_percent": route.shadow_percent,
                "baseline_route_id": route.baseline_route_id,
                "capacity_max_inflight": route.capacity_max_inflight,
                "capacity_requests_per_minute": route.capacity_requests_per_minute,
            }
            for route in self.routes
        ]
        return hashlib.sha256(
            json.dumps(topology, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _output_sha256(content: str) -> str:
        try:
            value = json.loads(content)
            canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError):
            canonical = content
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _dispatch_shadows(
        self,
        request: ModelRequest,
        active_route: ModelRoute,
        active_response: ModelResponse,
        messages: tuple[ModelMessage, ...],
        redactions: int,
        canonical: str,
        request_sha256: str,
        input_tokens: int,
        max_output_tokens: int,
        root_request_id: str,
    ) -> None:
        active_route_id = _route_key(active_route)
        active_output_sha256 = self._output_sha256(active_response.content)
        for candidate in self.candidate_routes:
            try:
                if candidate.baseline_route_id != active_route_id:
                    continue
                if not self._route_matches(candidate, request):
                    continue
                sample_digest = hashlib.sha256(
                    "\0".join(
                        (
                            self.topology_sha256,
                            _route_key(candidate),
                            request.tenant_id,
                            request.repository,
                            request.task_id,
                            request.purpose,
                            request_sha256,
                        )
                    ).encode("utf-8")
                ).digest()
                if int.from_bytes(sample_digest[:8], "big") % 100 >= candidate.shadow_percent:
                    continue
                observation_id = uuid.uuid4().hex
                if not self.store.start_model_route_shadow(
                    {
                        "observation_id": observation_id,
                        "topology_sha256": self.topology_sha256,
                        "root_request_id": root_request_id,
                        "tenant_id": request.tenant_id,
                        "repository": request.repository,
                        "task_id": request.task_id or None,
                        "purpose": request.purpose,
                        "active_route_id": active_route_id,
                        "candidate_route_id": _route_key(candidate),
                        "active_output_sha256": active_output_sha256,
                        "input_sha256": request_sha256,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ):
                    metrics.inc("model_shadow_observation_conflicts_total")
                    continue
                if self._shadow_executor is None:
                    self._finish_shadow(observation_id, "shed")
                    continue
                if not self._shadow_slots.acquire(blocking=False):
                    self._finish_shadow(observation_id, "shed")
                    metrics.inc("model_shadow_shed_total")
                    continue
                with self._shadow_condition:
                    if self._shadow_closed:
                        self._shadow_slots.release()
                        self._finish_shadow(observation_id, "cancelled")
                        continue
                    try:
                        future = self._shadow_executor.submit(
                            self._execute_shadow,
                            observation_id,
                            request,
                            candidate,
                            active_output_sha256,
                            messages,
                            redactions,
                            canonical,
                            input_tokens,
                            max_output_tokens,
                            root_request_id,
                        )
                    except RuntimeError:
                        self._shadow_slots.release()
                        self._finish_shadow(observation_id, "cancelled")
                        continue
                    self._shadow_futures.add(future)
                    future.add_done_callback(
                        partial(self._shadow_done, observation_id=observation_id)
                    )
                metrics.inc("model_shadow_scheduled_total")
            except Exception:
                # Candidate instrumentation must never change an already-completed
                # production response. A scheduled row, when present, remains a
                # visible promotion blocker for operator investigation.
                metrics.inc("model_shadow_dispatch_failures_total")

    def _execute_shadow(
        self,
        observation_id: str,
        request: ModelRequest,
        candidate: ModelRoute,
        active_output_sha256: str,
        messages: tuple[ModelMessage, ...],
        redactions: int,
        canonical: str,
        input_tokens: int,
        max_output_tokens: int,
        root_request_id: str,
    ) -> None:
        started = time.monotonic()
        try:
            response = self._complete_route(
                request,
                candidate,
                messages,
                redactions,
                canonical,
                input_tokens,
                max_output_tokens,
                root_request_id,
                1,
                "shadow",
            )
            output_sha256 = self._output_sha256(response.content)
            self._finish_shadow(
                observation_id,
                "success",
                agreement=output_sha256 == active_output_sha256,
                candidate_output_sha256=output_sha256,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_micros=_cost_micros(response.input_tokens, response.output_tokens, candidate),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            metrics.inc("model_shadow_completed_total")
        except Exception as exc:
            fields = safe_exception_fields(exc)
            if isinstance(exc, AccessDeniedError):
                status = "budget-rejected"
            elif isinstance(exc, ModelRouteCapacityError):
                status = "capacity-rejected"
            else:
                status = "failed"
            self._finish_shadow(
                observation_id,
                status,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_type=fields["error_type"],
                error_ref=fields["error_ref"],
            )
            metrics.inc("model_shadow_failed_total")

    def _finish_shadow(
        self,
        observation_id: str,
        status: str,
        *,
        agreement: bool | None = None,
        candidate_output_sha256: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_micros: int = 0,
        duration_ms: int = 0,
        error_type: str = "",
        error_ref: str = "",
    ) -> None:
        self.store.complete_model_route_shadow(
            observation_id,
            status,
            agreement,
            candidate_output_sha256,
            input_tokens,
            output_tokens,
            cost_micros,
            duration_ms,
            error_type,
            error_ref,
        )

    def _shadow_done(self, future: Future[None], observation_id: str) -> None:
        if future.cancelled():
            try:
                self._finish_shadow(observation_id, "cancelled")
            except Exception:
                metrics.inc("model_shadow_dispatch_failures_total")
        with self._shadow_condition:
            self._shadow_futures.discard(future)
            self._shadow_slots.release()
            self._shadow_condition.notify_all()

    def close(self) -> bool:
        """Stop accepting shadow work and wait a bounded time for in-flight samples."""
        executor = self._shadow_executor
        with self._shadow_condition:
            if self._shadow_closed:
                return not self._shadow_futures
            self._shadow_closed = True
        if executor is None:
            return True
        executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + self.options.shadow_shutdown_timeout_seconds
        with self._shadow_condition:
            while self._shadow_futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    metrics.inc("model_shadow_shutdown_timeouts_total")
                    return False
                self._shadow_condition.wait(remaining)
        return True
