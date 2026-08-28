"""Single-route model access with bounded, redacted egress."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .errors import AccessDeniedError, ClientInputError
from .json_boundary import strict_json_loads
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
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    route_id: str = "default"
    region: str = ""

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 200
            or not value.isprintable()
            for value in (self.provider, self.model)
        ):
            raise ValueError("model route provider and model must be bounded printable strings")
        if not isinstance(self.base_url, str) or not self.base_url:
            raise ValueError("model route base_url must be a non-empty string")
        if (
            not isinstance(self.api_key, str)
            or not 1 <= len(self.api_key) <= 4096
            or not self.api_key.isascii()
            or any(character.isspace() or ord(character) < 33 for character in self.api_key)
        ):
            raise ValueError("model route API key must be a bounded ASCII bearer token")
        if not isinstance(self.route_id, str) or not _ROUTE_ID.fullmatch(self.route_id):
            raise ValueError("model route id must be a stable identifier")
        if (
            not isinstance(self.region, str)
            or len(self.region) > 100
            or self.region != self.region.strip()
            or (self.region and not self.region.isprintable())
        ):
            raise ValueError("model route region must be a bounded printable string")
        if not isinstance(self.headers, Mapping) or len(self.headers) > 32:
            raise ValueError("model route headers must be a bounded mapping")
        headers = dict(self.headers)
        if any(
            not isinstance(name, str)
            or not _HEADER_NAME.fullmatch(name)
            or name.casefold() in {"authorization", "content-type", "accept"}
            or not isinstance(value, str)
            or len(value) > 8192
            or not value.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for name, value in headers.items()
        ):
            raise ValueError("model route contains an invalid or reserved header")
        object.__setattr__(self, "headers", MappingProxyType(headers))


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
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SECRET_NAME = r"password|passwd|api[_-]?key|secret|token"
_SECRET_FIELD = re.compile(r"(?i)^(?:%s)$" % _SECRET_NAME)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:%s)\b['\"]?\s*[:=]\s*)(['\"])([^'\"\n]+)(['\"])" % _SECRET_NAME
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_ROUTE_FIELDS = frozenset({"id", "provider", "model", "base_url", "api_key_env", "region"})
_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
MAX_MODEL_ROUTE_BYTES = 64 * 1024
MAX_MODEL_JSON_DEPTH = 64


def _redact_text(value: str, explicit_secrets: tuple[str, ...] = ()) -> tuple[str, int]:
    redactions = 0
    content, count = _SECRET_ASSIGNMENT.subn(
        lambda match: "%s%s<redacted>%s" % (match.group(1), match.group(2), match.group(4)),
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

    def redact_content(content: str, depth: int) -> str:
        nonlocal redactions
        if content.lstrip()[:1] in {"{", "[", '"'}:
            try:
                value = strict_json_loads(content)
            except json.JSONDecodeError:
                pass  # Ordinary prose/source fragments still use the text rules.
            except (ValueError, UnicodeError, RecursionError):
                raise ClientInputError("model JSON input is invalid or too deeply nested") from None
            else:
                before = redactions
                value = redact_value(value, depth + 1)
                return json.dumps(value, ensure_ascii=False) if redactions != before else content
        value, count = _redact_text(content, explicit_secrets)
        redactions += count
        return value

    def redact_value(value: Any, depth: int) -> Any:
        nonlocal redactions
        if depth > MAX_MODEL_JSON_DEPTH:
            raise ClientInputError("model JSON input exceeds the redaction depth limit")
        if isinstance(value, str):
            return redact_content(value, depth)
        if isinstance(value, list):
            return [redact_value(item, depth + 1) for item in value]
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                safe_key, count = _redact_text(key, explicit_secrets)
                redactions += count
                if safe_key in output:
                    raise ClientInputError("model JSON field names collide after redaction")
                if (
                    _SECRET_FIELD.fullmatch(key)
                    and isinstance(item, str)
                    and item not in {"", "<redacted>"}
                ):
                    output[safe_key] = "<redacted>"
                    redactions += 1
                else:
                    output[safe_key] = redact_value(item, depth + 1)
            return output
        return value

    output = []
    for message in messages:
        output.append(ModelMessage(message.role, redact_content(message.content, 0)))
    return tuple(output), redactions


def _estimated_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def load_model_route(path: str) -> ModelRoute:
    """Load exactly one trusted route; secrets remain environment-backed."""
    try:
        with open(path, "rb") as handle:
            content = handle.read(MAX_MODEL_ROUTE_BYTES + 1)
    except OSError as exc:
        raise ValueError("cannot read EVOAGENT_LLM_ROUTES_FILE: %s" % exc) from exc
    if len(content) > MAX_MODEL_ROUTE_BYTES:
        raise ValueError("EVOAGENT_LLM_ROUTES_FILE exceeds the 64 KiB limit")
    try:
        document = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("EVOAGENT_LLM_ROUTES_FILE is not valid TOML: %s" % exc) from exc
    version = document.get("version")
    if (
        set(document).difference({"version", "routes"})
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != 1
    ):
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
    if base_url != base_url.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in base_url
    ):
        raise ValueError("model route URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlparse(base_url.rstrip("/"))
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("model route must use a valid network port") from None
    if port == 0:
        raise ValueError("model route must use a valid network port")
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("model route must be an absolute URL without credentials or query data")
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("model route must use HTTPS (HTTP is allowed only for loopback)")
    if not loopback and port not in (None, 443):
        raise ValueError("model route must use port 443 outside loopback")
    allowed = {item.strip().lower() for item in allowed_hosts if item.strip()} or {host}
    if host not in allowed:
        raise ValueError("model route host is not an allowed EVOAGENT_LLM_ALLOWED_HOSTS entry")
    return base_url.rstrip("/") + "/chat/completions"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("model provider redirects are not allowed")


class OpenAICompatibleModelProvider:
    def __init__(
        self,
        allowed_hosts: tuple[str, ...] = (),
        timeout_seconds: int = 60,
        max_response_bytes: int = 2 * 1024 * 1024,
        breaker: CircuitBreaker | None = None,
    ):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("model provider timeout must be positive and finite")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("model provider response limit must be a positive integer")
        self.allowed_hosts = allowed_hosts
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.breaker = breaker
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

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
        if self.breaker is not None:
            self.breaker.allow()
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ValueError("model response exceeds the configured size limit")
            body = strict_json_loads(raw)
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("model content is not text")
            usage = body.get("usage")
            usage = {} if usage is None else usage
            if not isinstance(usage, dict):
                raise TypeError("model usage is not an object")
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (input_tokens, output_tokens)
            ):
                raise TypeError("model token usage is not a non-negative integer")
            request_id = body.get("id", "")
            if not isinstance(request_id, str):
                raise TypeError("model request id is not text")
            result = ModelResponse(
                content,
                route.provider,
                route.model,
                input_tokens,
                output_tokens,
                request_id,
            )
        except urllib.error.HTTPError as exc:
            try:
                transient = exc.code in {408, 425, 429} or exc.code >= 500
                if self.breaker is not None:
                    if transient:
                        self.breaker.record_failure()
                    else:
                        self.breaker.record_success()
                detail = exc.read(1000).decode("utf-8", errors="replace")
                detail, _ = _redact_text(detail, (route.api_key, *route.headers.values()))
            finally:
                exc.close()
            raise ModelProviderError(
                "%s API returned HTTP %d: %s" % (route.provider, exc.code, detail),
                transient=transient,
            ) from exc
        except (
            TimeoutError,
            urllib.error.URLError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            RecursionError,
        ) as exc:
            if self.breaker is not None:
                self.breaker.record_failure()
            detail, _ = _redact_text(str(exc), (route.api_key, *route.headers.values()))
            raise ModelProviderError(
                "%s model request failed: %s" % (route.provider, detail), transient=True
            ) from exc
        except Exception:
            if self.breaker is not None:
                self.breaker.record_failure()
            raise
        if self.breaker is not None:
            self.breaker.record_success()
        return result


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
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (
                self.options.max_input_tokens,
                self.options.max_output_tokens,
                self.options.max_response_bytes,
            )
        ):
            raise ValueError("model gateway token and response limits must be positive integers")
        if (route is None) != (provider is None):
            raise ValueError("model route and provider must be configured together")
        if route is not None:
            if not isinstance(route.base_url, str) or not route.base_url:
                raise ValueError("model route base_url must be a non-empty string")
            _validated_endpoint(route.base_url, self.options.allowed_hosts)

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

    def execution_revision(self) -> str:
        """Return a secret-free digest of settings that can change model execution."""
        route = self.route
        payload = (
            {
                "route": {
                    "provider": route.provider,
                    "model": route.model,
                    "base_url": route.base_url,
                    "route_id": route.route_id,
                    "region": route.region,
                    "header_names": sorted(route.headers),
                },
                "limits": {
                    "allowed_hosts": sorted(self.options.allowed_hosts),
                    "max_input_tokens": self.options.max_input_tokens,
                    "max_output_tokens": self.options.max_output_tokens,
                    "max_response_bytes": self.options.max_response_bytes,
                },
            }
            if route
            else {"route": None}
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def breaker_state_code(self) -> int:
        breaker = getattr(self.provider, "breaker", None)
        return breaker.state_code if breaker is not None else 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.route or not self.provider:
            raise RuntimeError("model gateway is not configured")
        if not isinstance(request, ModelRequest):
            raise ClientInputError("model request must use the governed request schema")
        if (
            any(
                not isinstance(value, str)
                for value in (
                    request.tenant_id,
                    request.repository,
                    request.task_id,
                    request.purpose,
                    request.required_region,
                )
            )
            or not request.tenant_id
            or not request.repository
            or not request.purpose
            or not isinstance(request.allowed_providers, tuple)
            or any(not isinstance(value, str) for value in request.allowed_providers)
            or not isinstance(request.allowed_models, tuple)
            or any(not isinstance(value, str) for value in request.allowed_models)
        ):
            raise ClientInputError("model request contains invalid governance metadata")
        if not isinstance(request.require_json_object, bool):
            raise ClientInputError("model request JSON mode must be boolean")
        if not isinstance(request.messages, tuple):
            raise ClientInputError("model request messages must use the message schema")
        route = self.route
        if request.allowed_providers and route.provider not in request.allowed_providers:
            raise AccessDeniedError("model provider is not allowed by repository policy")
        if request.allowed_models and route.model not in request.allowed_models:
            raise AccessDeniedError("model is not allowed by repository policy")
        if request.required_region and route.region != request.required_region:
            raise AccessDeniedError("model route does not satisfy the required region")
        if not request.messages:
            raise ClientInputError("model request messages are required")
        input_tokens = 0
        try:
            for message in request.messages:
                if not isinstance(message, ModelMessage):
                    raise ClientInputError("model request messages must use the message schema")
                if (
                    not isinstance(message.role, str)
                    or message.role not in _MESSAGE_ROLES
                    or not isinstance(message.content, str)
                ):
                    raise ClientInputError("model request contains an invalid message")
                input_tokens += _estimated_tokens(message.content)
                if input_tokens > self.options.max_input_tokens:
                    raise ClientInputError("model input exceeds the configured token limit")
        except UnicodeEncodeError:
            raise ClientInputError("model request messages must contain valid UTF-8") from None
        max_output_tokens = request.max_output_tokens
        if max_output_tokens is None:
            max_output_tokens = self.options.max_output_tokens
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= self.options.max_output_tokens
        ):
            raise ClientInputError("model output limit must be a positive bounded integer")
        secrets = (route.api_key, *route.headers.values())
        messages, redactions = redact_model_messages(request.messages, secrets)
        if (
            sum(_estimated_tokens(message.content) for message in messages)
            > self.options.max_input_tokens
        ):
            raise ClientInputError("redacted model input exceeds the configured token limit")
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
        if (
            not isinstance(response, ModelResponse)
            or not isinstance(response.content, str)
            or response.provider != route.provider
            or response.model != route.model
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (response.input_tokens, response.output_tokens)
            )
            or response.output_tokens > max_output_tokens
            or not isinstance(response.request_id, str)
        ):
            metrics.inc("model_requests_failed_total")
            raise ModelOutputError("model provider response violates the gateway contract")
        try:
            response_bytes = response.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            metrics.inc("model_requests_failed_total")
            raise ModelOutputError("model provider response violates the gateway contract") from exc
        if len(response_bytes) > self.options.max_response_bytes:
            metrics.inc("model_requests_failed_total")
            raise ModelOutputError("model response exceeds the configured size limit")
        if request.require_json_object:
            try:
                output = strict_json_loads(response.content)
            except (UnicodeError, ValueError, RecursionError) as exc:
                metrics.inc("model_requests_failed_total")
                raise ModelOutputError("model response is not valid JSON") from exc
            if not isinstance(output, dict):
                metrics.inc("model_requests_failed_total")
                raise ModelOutputError("model response must be a JSON object")
        metrics.inc("model_requests_total")
        return response
