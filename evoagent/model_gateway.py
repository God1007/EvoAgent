"""Single-route model access with bounded, redacted egress."""

from __future__ import annotations

import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .errors import AccessDeniedError, ClientInputError
from .metrics import metrics


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
    route_id: str = "default"
    region: str = ""


@dataclass(frozen=True)
class ModelGatewayOptions:
    allowed_hosts: tuple[str, ...] = ()
    max_input_tokens: int = 120_000
    max_output_tokens: int = 4_096
    max_response_bytes: int = 2 * 1024 * 1024


class ModelProviderPort(Protocol):
    def complete(
        self,
        route: ModelRoute,
        messages: tuple[ModelMessage, ...],
        max_output_tokens: int,
        require_json_object: bool,
    ) -> ModelResponse: ...


class ModelProviderError(RuntimeError):
    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


class ModelOutputError(RuntimeError):
    """The provider response violated the gateway contract."""


_ROUTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b(\s*[:=]\s*)(['\"])([^'\"\n]+)(['\"])"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_ROUTE_FIELDS = frozenset({"id", "provider", "model", "base_url", "api_key_env", "region"})
_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})


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
    messages: tuple[ModelMessage, ...], explicit_secrets: tuple[str, ...] = ()
) -> tuple[tuple[ModelMessage, ...], int]:
    redactions = 0
    output = []
    for message in messages:
        content, count = _redact_text(message.content, explicit_secrets)
        redactions += count
        output.append(ModelMessage(message.role, content))
    return tuple(output), redactions


def _estimated_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def load_model_route(path: str) -> ModelRoute:
    """Load exactly one trusted route; secrets remain environment-backed."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ValueError("cannot read EVOAGENT_LLM_ROUTES_FILE: %s" % exc) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("EVOAGENT_LLM_ROUTES_FILE is not valid TOML: %s" % exc) from exc
    if set(document).difference({"version", "routes"}) or document.get("version") != 1:
        raise ValueError("model routes file must use version 1")
    routes = document.get("routes")
    if not isinstance(routes, list) or len(routes) != 1 or not isinstance(routes[0], dict):
        raise ValueError("model routes file must define exactly one route")
    raw = routes[0]
    unknown = set(raw).difference(_ROUTE_FIELDS)
    if unknown:
        raise ValueError("model route contains unsupported fields: %s" % ", ".join(unknown))
    route_id = raw.get("id", "default")
    if not isinstance(route_id, str) or not _ROUTE_ID.fullmatch(route_id):
        raise ValueError("model route id must be a stable identifier")
    required = {}
    for name in ("provider", "model", "base_url"):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("model route requires %s" % name)
        required[name] = value.strip()
    api_key_env = raw.get("api_key_env")
    if not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env):
        raise ValueError("model route api_key_env must name an uppercase environment variable")
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        raise ValueError("model route requires environment variable %s" % api_key_env)
    region = raw.get("region", "")
    if not isinstance(region, str) or len(region) > 100:
        raise ValueError("model route region must be a string of at most 100 characters")
    return ModelRoute(
        provider=required["provider"],
        model=required["model"],
        base_url=required["base_url"],
        api_key=api_key,
        route_id=route_id,
        region=region.strip(),
    )


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
            **route.headers,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._open(request) as response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ValueError("model response exceeds the configured size limit")
            body = json.loads(raw.decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("model content is not text")
            usage = body.get("usage") or {}
            return ModelResponse(
                content,
                route.provider,
                route.model,
                max(0, int(usage.get("prompt_tokens", 0) or 0)),
                max(0, int(usage.get("completion_tokens", 0) or 0)),
                str(body.get("id", "")),
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            detail, _ = _redact_text(detail, (route.api_key, *route.headers.values()))
            raise ModelProviderError(
                "%s API returned HTTP %d: %s" % (route.provider, exc.code, detail),
                transient=exc.code in {408, 425, 429} or exc.code >= 500,
            ) from exc
        except CircuitOpenError:
            raise
        except (
            TimeoutError,
            urllib.error.URLError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            detail, _ = _redact_text(str(exc), (route.api_key, *route.headers.values()))
            raise ModelProviderError(
                "%s model request failed: %s" % (route.provider, detail), transient=True
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
                self.breaker.record_success()
            raise
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return response


class ModelGateway:
    def __init__(
        self,
        route: ModelRoute | None,
        provider: ModelProviderPort | None,
        options: ModelGatewayOptions | None = None,
    ):
        self.route = route
        self.provider = provider
        self.options = options or ModelGatewayOptions()

    @property
    def configured(self) -> bool:
        return self.route is not None and self.provider is not None

    def route_info(self) -> dict[str, Any]:
        if not self.route:
            return {"provider": "local", "model": "", "route_id": ""}
        return {
            "provider": self.route.provider,
            "model": self.route.model,
            "route_id": self.route.route_id,
            "region": self.route.region,
        }

    @property
    def breaker_state_code(self) -> int:
        breaker = getattr(self.provider, "breaker", None)
        return breaker.state_code if breaker is not None else 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.route or not self.provider:
            raise RuntimeError("model gateway is not configured")
        route = self.route
        if request.allowed_providers and route.provider not in request.allowed_providers:
            raise AccessDeniedError("model provider is not allowed by repository policy")
        if request.allowed_models and route.model not in request.allowed_models:
            raise AccessDeniedError("model is not allowed by repository policy")
        if request.required_region and route.region != request.required_region:
            raise AccessDeniedError("model route does not satisfy the required region")
        if not request.messages:
            raise ClientInputError("model request messages are required")
        if any(
            message.role not in _MESSAGE_ROLES or not isinstance(message.content, str)
            for message in request.messages
        ):
            raise ClientInputError("model request contains an invalid message")
        input_tokens = sum(_estimated_tokens(message.content) for message in request.messages)
        if input_tokens > self.options.max_input_tokens:
            raise ClientInputError("model input exceeds the configured token limit")
        max_output_tokens = request.max_output_tokens or self.options.max_output_tokens
        if not 1 <= max_output_tokens <= self.options.max_output_tokens:
            raise ClientInputError("model output limit exceeds the configured maximum")
        secrets = (route.api_key, *route.headers.values())
        messages, redactions = redact_model_messages(request.messages, secrets)
        if redactions:
            metrics.inc("model_redactions_total", redactions)
        try:
            response = self.provider.complete(
                route, messages, max_output_tokens, request.require_json_object
            )
        except (AccessDeniedError, ClientInputError, CircuitOpenError, ModelProviderError):
            metrics.inc("model_requests_failed_total")
            raise
        except Exception as exc:
            metrics.inc("model_requests_failed_total")
            detail, _ = _redact_text(str(exc), secrets)
            raise ModelProviderError(
                "%s model request failed: %s" % (route.provider, detail), transient=True
            ) from exc
        if len(response.content.encode("utf-8")) > self.options.max_response_bytes:
            metrics.inc("model_requests_failed_total")
            raise ModelOutputError("model response exceeds the configured size limit")
        if request.require_json_object:
            try:
                output = json.loads(response.content)
            except json.JSONDecodeError as exc:
                metrics.inc("model_requests_failed_total")
                raise ModelOutputError("model response is not valid JSON") from exc
            if not isinstance(output, dict):
                metrics.inc("model_requests_failed_total")
                raise ModelOutputError("model response must be a JSON object")
        metrics.inc("model_requests_total")
        return response
