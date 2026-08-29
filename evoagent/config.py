import math
import os
import sys
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Any
from urllib.parse import urlsplit

SOURCE_SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
INSTALLED_SKILLS_DIR = os.path.join(sys.prefix, "share", "evoagent", "skills")
DEFAULT_SKILLS_DIR = SOURCE_SKILLS_DIR if os.path.isdir(SOURCE_SKILLS_DIR) else INSTALLED_SKILLS_DIR


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("%s must be a boolean" % name)


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError("%s must be non-negative" % name)
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _non_negative_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s must be non-negative" % name)
    return value


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host.strip()).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    max_diff_bytes: int
    max_steps: int
    timeout_seconds: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    github_webhook_secret: str
    github_token: str
    auto_post_review: bool
    database_url: str = ""
    redis_url: str = ""
    async_workers: int = 2
    skills_dir: str = DEFAULT_SKILLS_DIR
    github_app_id: str = ""
    github_app_slug: str = ""
    github_private_key_path: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_callback_url: str = ""
    llm_provider: str = "local"
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_site_url: str = ""
    openrouter_app_name: str = "EvoAgent"
    llm_allowed_hosts: tuple[str, ...] = ()
    llm_max_input_tokens: int = 120000
    llm_max_output_tokens: int = 4096
    llm_routes_file: str = ""
    eval_max_cases: int = 5
    eval_min_cases: int = 3
    eval_min_improvement: float = 0.01
    eval_min_holdout_cases: int = 2
    eval_max_metric_regression: float = 0.0
    auth_required: bool = False
    auth_secret: str = ""
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    default_tenant_id: str = "default"
    session_ttl_seconds: int = 3600
    webhook_max_age_seconds: int = 600
    queue_max_attempts: int = 3
    queue_lease_seconds: int = 60
    queue_shutdown_timeout_seconds: int = 30
    shutdown_grace_seconds: float = 0.0
    skill_timeout_seconds: int = 30
    skill_memory_mb: int = 256
    skill_sandbox: bool = True
    skill_signing_key: str = ""
    skill_container_image: str = ""
    skill_require_container: bool = False
    repair_test_command: str = ""
    repair_verify_timeout_seconds: int = 120
    repair_container_image: str = ""
    repair_memory_mb: int = 1024
    repair_pids_limit: int = 256
    repair_cpus: float = 1.0
    repair_max_output_bytes: int = 16000
    proof_executor_socket: str = ""
    otel_endpoint: str = ""
    otel_service_name: str = "evoagent"
    alert_failure_rate: float = 0.20
    alert_min_samples: int = 10
    rate_limit_rps: int = 0
    rate_limit_burst: int = 0
    trusted_proxy_cidrs: tuple[str, ...] = ()
    max_inflight_heavy: int = 0
    max_http_connections: int = 128
    history_retention_days: int = 0
    history_maintenance_seconds: int = 3600
    history_prune_batch_size: int = 1000
    tenant_max_active_reviews: int = 0
    tenant_capacity_retry_seconds: int = 5
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: int = 30
    pg_pool_min: int = 1
    pg_pool_max: int = 10
    pg_pool_timeout: int = 10
    pg_statement_timeout_seconds: int = 120
    outbox_poll_seconds: float = 0.25
    outbox_batch_size: int = 50
    outbox_lease_seconds: int = 30
    outbox_max_attempts: int = 20
    effect_lease_seconds: int = 300
    repository_evidence_max_bytes: int = 32 * 1024 * 1024
    github_webhook_previous_secret: str = ""
    auth_previous_secret: str = ""

    def resolved_llm(self) -> dict[str, Any]:
        """Resolve a named provider to the existing OpenAI-compatible transport."""
        provider = self.llm_provider.strip().lower()
        if provider in {"", "local", "none"}:
            if self.llm_base_url or self.llm_api_key or self.llm_model:
                provider = "custom"
            else:
                return {}

        if provider == "deepseek":
            api_key = self.deepseek_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("DeepSeek requires EVOAGENT_DEEPSEEK_API_KEY")
            return {
                "provider": "deepseek",
                "base_url": self.llm_base_url or "https://api.deepseek.com",
                "api_key": api_key,
                "model": self.llm_model or "deepseek-v4-flash",
                "headers": {},
            }

        if provider in {"openrouter-deepseek-free", "openrouter_deepseek_free"}:
            api_key = self.openrouter_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("OpenRouter requires EVOAGENT_OPENROUTER_API_KEY")
            headers = {}
            if self.openrouter_site_url:
                headers["HTTP-Referer"] = self.openrouter_site_url
            if self.openrouter_app_name:
                headers["X-Title"] = self.openrouter_app_name
            return {
                "provider": "openrouter-deepseek-free",
                "base_url": self.llm_base_url or "https://openrouter.ai/api/v1",
                "api_key": api_key,
                "model": self.llm_model or "deepseek/deepseek-chat-v3-0324:free",
                "headers": headers,
            }

        if provider == "openrouter-free":
            api_key = self.openrouter_api_key or self.llm_api_key
            if not api_key:
                raise ValueError("OpenRouter requires EVOAGENT_OPENROUTER_API_KEY")
            headers = {}
            if self.openrouter_site_url:
                headers["HTTP-Referer"] = self.openrouter_site_url
            if self.openrouter_app_name:
                headers["X-Title"] = self.openrouter_app_name
            return {
                "provider": "openrouter-free",
                "base_url": self.llm_base_url or "https://openrouter.ai/api/v1",
                "api_key": api_key,
                "model": self.llm_model or "openrouter/free",
                "headers": headers,
            }

        if provider == "custom":
            if not (self.llm_base_url and self.llm_api_key and self.llm_model):
                raise ValueError(
                    "Custom LLM requires EVOAGENT_LLM_BASE_URL, "
                    "EVOAGENT_LLM_API_KEY and EVOAGENT_LLM_MODEL"
                )
            return {
                "provider": "custom",
                "base_url": self.llm_base_url,
                "api_key": self.llm_api_key,
                "model": self.llm_model,
                "headers": {},
            }
        raise ValueError("unsupported EVOAGENT_LLM_PROVIDER: %s" % self.llm_provider)

    def validate_evolution(self) -> None:
        if (
            not isinstance(self.host, str)
            or not self.host
            or not self.host.isprintable()
            or any(character.isspace() for character in self.host)
        ):
            raise ValueError("EVOAGENT_HOST must be a non-empty hostname or address")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65_535
        ):
            raise ValueError("EVOAGENT_PORT must be between 1 and 65535")
        exposed = not _is_loopback(self.host)
        if exposed and not self.auth_required:
            raise ValueError("EVOAGENT_AUTH_REQUIRED must be true outside loopback")
        if exposed and not self.redis_url:
            raise ValueError("EVOAGENT_REDIS_URL is required outside loopback")
        if exposed and self.rate_limit_rps <= 0:
            raise ValueError("EVOAGENT_RATE_LIMIT_RPS must be positive outside loopback")
        if exposed and self.max_inflight_heavy <= 0:
            raise ValueError("EVOAGENT_MAX_INFLIGHT_HEAVY must be positive outside loopback")
        if self.max_http_connections <= 0:
            raise ValueError("EVOAGENT_MAX_HTTP_CONNECTIONS must be positive")
        if self.max_diff_bytes <= 0:
            raise ValueError("EVOAGENT_MAX_DIFF_BYTES must be positive")
        if self.max_steps <= 0:
            raise ValueError("EVOAGENT_MAX_STEPS must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("EVOAGENT_TIMEOUT_SECONDS must be positive")
        if self.llm_max_input_tokens <= 0:
            raise ValueError("EVOAGENT_LLM_MAX_INPUT_TOKENS must be positive")
        if self.llm_max_output_tokens <= 0:
            raise ValueError("EVOAGENT_LLM_MAX_OUTPUT_TOKENS must be positive")
        if not 1 <= self.async_workers <= 256:
            raise ValueError("EVOAGENT_ASYNC_WORKERS must be between 1 and 256")
        if self.queue_max_attempts <= 0:
            raise ValueError("EVOAGENT_QUEUE_MAX_ATTEMPTS must be positive")
        if self.queue_lease_seconds <= 0:
            raise ValueError("EVOAGENT_QUEUE_LEASE_SECONDS must be positive")
        if exposed and self.tenant_max_active_reviews <= 0:
            raise ValueError("EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS must be positive outside loopback")
        if exposed and not self.skill_require_container:
            raise ValueError("EVOAGENT_SKILL_REQUIRE_CONTAINER must be true outside loopback")
        if self.eval_min_cases > self.eval_max_cases:
            raise ValueError("EVOAGENT_EVAL_MIN_CASES cannot exceed EVOAGENT_EVAL_MAX_CASES")
        if not 0.0 <= self.eval_min_improvement <= 1.0:
            raise ValueError("EVOAGENT_EVAL_MIN_IMPROVEMENT must be between 0 and 1")
        if self.eval_min_holdout_cases > self.eval_max_cases:
            raise ValueError(
                "EVOAGENT_EVAL_MIN_HOLDOUT_CASES cannot exceed EVOAGENT_EVAL_MAX_CASES"
            )
        if not 0.0 <= self.eval_max_metric_regression <= 1.0:
            raise ValueError("EVOAGENT_EVAL_MAX_METRIC_REGRESSION must be between 0 and 1")
        if self.auth_required and len(self.auth_secret.encode("utf-8")) < 32:
            raise ValueError(
                "EVOAGENT_AUTH_SECRET must contain at least 32 bytes when authentication is enabled"
            )
        if self.auth_previous_secret and (
            not self.auth_secret or len(self.auth_previous_secret.encode("utf-8")) < 32
        ):
            raise ValueError(
                "EVOAGENT_AUTH_PREVIOUS_SECRET requires the current secret and at least 32 bytes"
            )
        if bool(self.bootstrap_admin_username) != bool(self.bootstrap_admin_password):
            raise ValueError("bootstrap admin username and password must be configured together")
        if (
            not self.default_tenant_id
            or self.default_tenant_id != self.default_tenant_id.strip()
            or len(self.default_tenant_id) > 200
        ):
            raise ValueError(
                "EVOAGENT_DEFAULT_TENANT_ID must be 1-200 characters without surrounding whitespace"
            )
        if self.github_webhook_previous_secret and not self.github_webhook_secret:
            raise ValueError(
                "EVOAGENT_GITHUB_WEBHOOK_PREVIOUS_SECRET requires EVOAGENT_GITHUB_WEBHOOK_SECRET"
            )
        github_app = (
            bool(self.github_app_id.strip()),
            bool(self.github_private_key_path.strip()),
        )
        if any(github_app) and not all(github_app):
            raise ValueError(
                "EVOAGENT_GITHUB_APP_ID and EVOAGENT_GITHUB_PRIVATE_KEY_PATH "
                "must be configured together"
            )
        if self.auto_post_review and not (self.github_token.strip() or all(github_app)):
            raise ValueError(
                "EVOAGENT_AUTO_POST_REVIEW requires EVOAGENT_GITHUB_TOKEN or GitHub App credentials"
            )
        github_oauth = (
            self.github_app_slug,
            self.github_client_id,
            self.github_client_secret,
            self.github_oauth_callback_url,
        )
        if any(github_oauth):
            if not all(github_oauth):
                raise ValueError(
                    "GitHub installation OAuth requires EVOAGENT_GITHUB_APP_SLUG, "
                    "EVOAGENT_GITHUB_CLIENT_ID, EVOAGENT_GITHUB_CLIENT_SECRET and "
                    "EVOAGENT_GITHUB_OAUTH_CALLBACK_URL"
                )
            if not self.auth_required:
                raise ValueError("GitHub installation OAuth requires EVOAGENT_AUTH_REQUIRED")
            if not all(github_app):
                raise ValueError(
                    "GitHub installation OAuth requires EVOAGENT_GITHUB_APP_ID and "
                    "EVOAGENT_GITHUB_PRIVATE_KEY_PATH"
                )
            callback = urlsplit(self.github_oauth_callback_url)
            secure_callback = callback.scheme == "https" or (
                callback.scheme == "http" and _is_loopback(callback.hostname or "")
            )
            if (
                not secure_callback
                or not callback.hostname
                or callback.username
                or callback.password
                or callback.query
                or callback.fragment
            ):
                raise ValueError("EVOAGENT_GITHUB_OAUTH_CALLBACK_URL must be a secure exact URL")
        if (
            exposed
            and (self.auto_post_review or self.github_webhook_secret)
            and not all(github_oauth)
        ):
            raise ValueError(
                "GitHub Webhook intake outside loopback requires complete tenant-bound "
                "installation OAuth configuration"
            )
        if (
            exposed
            and (self.auto_post_review or self.github_webhook_secret)
            and len(self.github_webhook_secret.encode("utf-8")) < 32
        ):
            raise ValueError(
                "EVOAGENT_GITHUB_WEBHOOK_SECRET must contain at least 32 bytes outside loopback"
            )
        if (
            exposed
            and self.github_webhook_previous_secret
            and len(self.github_webhook_previous_secret.encode("utf-8")) < 32
        ):
            raise ValueError(
                "EVOAGENT_GITHUB_WEBHOOK_PREVIOUS_SECRET must contain at least 32 bytes "
                "outside loopback"
            )
        if not 0.0 <= self.alert_failure_rate <= 1.0:
            raise ValueError("EVOAGENT_ALERT_FAILURE_RATE must be between 0 and 1")
        if self.alert_min_samples <= 0:
            raise ValueError("EVOAGENT_ALERT_MIN_SAMPLES must be positive")
        if self.session_ttl_seconds <= 0:
            raise ValueError("EVOAGENT_SESSION_TTL_SECONDS must be positive")
        if self.webhook_max_age_seconds <= 0:
            raise ValueError("EVOAGENT_WEBHOOK_MAX_AGE_SECONDS must be positive")
        if self.skill_timeout_seconds <= 0:
            raise ValueError("EVOAGENT_SKILL_TIMEOUT_SECONDS must be positive")
        if self.skill_memory_mb <= 0:
            raise ValueError("EVOAGENT_SKILL_MEMORY_MB must be positive")
        if self.repair_verify_timeout_seconds <= 0:
            raise ValueError("EVOAGENT_REPAIR_VERIFY_TIMEOUT_SECONDS must be positive")
        if self.repair_memory_mb <= 0:
            raise ValueError("EVOAGENT_REPAIR_MEMORY_MB must be positive")
        if self.repair_pids_limit <= 0:
            raise ValueError("EVOAGENT_REPAIR_PIDS_LIMIT must be positive")
        if self.repair_max_output_bytes <= 0:
            raise ValueError("EVOAGENT_REPAIR_MAX_OUTPUT_BYTES must be positive")
        if not isinstance(self.proof_executor_socket, str) or (
            self.proof_executor_socket
            and (
                not os.path.isabs(self.proof_executor_socket) or "\0" in self.proof_executor_socket
            )
        ):
            raise ValueError("EVOAGENT_PROOF_EXECUTOR_SOCKET must be an absolute path")
        if len(self.trusted_proxy_cidrs) > 64:
            raise ValueError("EVOAGENT_TRUSTED_PROXY_CIDRS accepts at most 64 networks")
        normalized_proxy_cidrs = []
        for value in self.trusted_proxy_cidrs:
            try:
                network = ip_network(value, strict=True)
            except ValueError as exc:
                raise ValueError("EVOAGENT_TRUSTED_PROXY_CIDRS contains an invalid CIDR") from exc
            if value != str(network):
                raise ValueError(
                    "EVOAGENT_TRUSTED_PROXY_CIDRS entries must use canonical CIDR notation"
                )
            if network.prefixlen == 0:
                raise ValueError("EVOAGENT_TRUSTED_PROXY_CIDRS cannot trust the entire internet")
            normalized_proxy_cidrs.append(str(network))
        if len(normalized_proxy_cidrs) != len(set(normalized_proxy_cidrs)):
            raise ValueError("EVOAGENT_TRUSTED_PROXY_CIDRS contains duplicate networks")
        if self.history_retention_days > 36_500:
            raise ValueError("EVOAGENT_HISTORY_RETENTION_DAYS must be at most 36500")
        if (
            self.history_retention_days
            and self.history_retention_days * 86_400 <= self.webhook_max_age_seconds
        ):
            raise ValueError(
                "EVOAGENT_HISTORY_RETENTION_DAYS must exceed the webhook replay window"
            )
        if self.history_maintenance_seconds <= 0:
            raise ValueError("EVOAGENT_HISTORY_MAINTENANCE_SECONDS must be positive")
        if self.history_retention_days and self.history_maintenance_seconds < 60:
            raise ValueError(
                "EVOAGENT_HISTORY_MAINTENANCE_SECONDS must be at least 60 when retention is enabled"
            )
        if self.history_prune_batch_size <= 0:
            raise ValueError("EVOAGENT_HISTORY_PRUNE_BATCH_SIZE must be positive")
        if self.history_prune_batch_size > 10_000:
            raise ValueError("EVOAGENT_HISTORY_PRUNE_BATCH_SIZE must be at most 10000")
        if not 0 <= self.tenant_max_active_reviews <= 1_000_000:
            raise ValueError("EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS must be between 0 and 1000000")
        if not 1 <= self.tenant_capacity_retry_seconds <= 3600:
            raise ValueError("EVOAGENT_TENANT_CAPACITY_RETRY_SECONDS must be between 1 and 3600")
        if not math.isfinite(self.outbox_poll_seconds) or self.outbox_poll_seconds <= 0:
            raise ValueError("EVOAGENT_OUTBOX_POLL_SECONDS must be positive")
        if self.outbox_batch_size <= 0:
            raise ValueError("EVOAGENT_OUTBOX_BATCH_SIZE must be positive")
        if self.outbox_lease_seconds <= 0:
            raise ValueError("EVOAGENT_OUTBOX_LEASE_SECONDS must be positive")
        if self.outbox_max_attempts <= 0:
            raise ValueError("EVOAGENT_OUTBOX_MAX_ATTEMPTS must be positive")
        if self.pg_statement_timeout_seconds <= 0:
            raise ValueError("EVOAGENT_PG_STATEMENT_TIMEOUT_SECONDS must be positive")
        # ponytail: fixed floor covers one bounded provider attempt; effect owners
        # renew immediately before each write/retry instead of running heartbeat threads.
        if self.effect_lease_seconds < 300:
            raise ValueError("EVOAGENT_EFFECT_LEASE_SECONDS must be at least 300")
        if not 1 <= self.repository_evidence_max_bytes <= 1024 * 1024 * 1024:
            raise ValueError(
                "EVOAGENT_REPOSITORY_EVIDENCE_MAX_BYTES must be between 1 and 1073741824"
            )
        if not math.isfinite(self.repair_cpus) or self.repair_cpus <= 0:
            raise ValueError("EVOAGENT_REPAIR_CPUS must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("EVOAGENT_HOST", "127.0.0.1"),
            port=_int("EVOAGENT_PORT", 8080),
            max_diff_bytes=_int("EVOAGENT_MAX_DIFF_BYTES", 1024 * 1024),
            repository_evidence_max_bytes=_int(
                "EVOAGENT_REPOSITORY_EVIDENCE_MAX_BYTES", 32 * 1024 * 1024
            ),
            max_steps=_int("EVOAGENT_MAX_STEPS", 8),
            timeout_seconds=_int("EVOAGENT_TIMEOUT_SECONDS", 120),
            llm_base_url=os.getenv("EVOAGENT_LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=os.getenv("EVOAGENT_LLM_API_KEY", ""),
            llm_model=os.getenv("EVOAGENT_LLM_MODEL", ""),
            github_webhook_secret=os.getenv("EVOAGENT_GITHUB_WEBHOOK_SECRET", ""),
            github_token=os.getenv("EVOAGENT_GITHUB_TOKEN", ""),
            auto_post_review=_bool("EVOAGENT_AUTO_POST_REVIEW"),
            github_webhook_previous_secret=os.getenv("EVOAGENT_GITHUB_WEBHOOK_PREVIOUS_SECRET", ""),
            database_url=os.getenv("EVOAGENT_DATABASE_URL", ""),
            redis_url=os.getenv("EVOAGENT_REDIS_URL", ""),
            async_workers=_int("EVOAGENT_ASYNC_WORKERS", 2),
            skills_dir=os.getenv("EVOAGENT_SKILLS_DIR", DEFAULT_SKILLS_DIR),
            github_app_id=os.getenv("EVOAGENT_GITHUB_APP_ID", ""),
            github_app_slug=os.getenv("EVOAGENT_GITHUB_APP_SLUG", ""),
            github_private_key_path=os.getenv("EVOAGENT_GITHUB_PRIVATE_KEY_PATH", ""),
            github_client_id=os.getenv("EVOAGENT_GITHUB_CLIENT_ID", ""),
            github_client_secret=os.getenv("EVOAGENT_GITHUB_CLIENT_SECRET", ""),
            github_oauth_callback_url=os.getenv("EVOAGENT_GITHUB_OAUTH_CALLBACK_URL", ""),
            llm_provider=os.getenv("EVOAGENT_LLM_PROVIDER", "local"),
            deepseek_api_key=os.getenv("EVOAGENT_DEEPSEEK_API_KEY", ""),
            openrouter_api_key=os.getenv("EVOAGENT_OPENROUTER_API_KEY", ""),
            openrouter_site_url=os.getenv("EVOAGENT_OPENROUTER_SITE_URL", ""),
            openrouter_app_name=os.getenv("EVOAGENT_OPENROUTER_APP_NAME", "EvoAgent"),
            llm_allowed_hosts=_csv("EVOAGENT_LLM_ALLOWED_HOSTS"),
            llm_max_input_tokens=_int("EVOAGENT_LLM_MAX_INPUT_TOKENS", 120000),
            llm_max_output_tokens=_int("EVOAGENT_LLM_MAX_OUTPUT_TOKENS", 4096),
            llm_routes_file=os.getenv("EVOAGENT_LLM_ROUTES_FILE", ""),
            eval_max_cases=_int("EVOAGENT_EVAL_MAX_CASES", 5),
            eval_min_cases=_int("EVOAGENT_EVAL_MIN_CASES", 3),
            eval_min_improvement=float(os.getenv("EVOAGENT_EVAL_MIN_IMPROVEMENT", "0.01")),
            eval_min_holdout_cases=_non_negative_int("EVOAGENT_EVAL_MIN_HOLDOUT_CASES", 2),
            eval_max_metric_regression=float(os.getenv("EVOAGENT_EVAL_MAX_METRIC_REGRESSION", "0")),
            auth_required=_bool("EVOAGENT_AUTH_REQUIRED", False),
            auth_secret=os.getenv("EVOAGENT_AUTH_SECRET", ""),
            auth_previous_secret=os.getenv("EVOAGENT_AUTH_PREVIOUS_SECRET", ""),
            bootstrap_admin_username=os.getenv("EVOAGENT_BOOTSTRAP_ADMIN_USERNAME", ""),
            bootstrap_admin_password=os.getenv("EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD", ""),
            default_tenant_id=os.getenv("EVOAGENT_DEFAULT_TENANT_ID", "default"),
            session_ttl_seconds=_int("EVOAGENT_SESSION_TTL_SECONDS", 3600),
            webhook_max_age_seconds=_int("EVOAGENT_WEBHOOK_MAX_AGE_SECONDS", 600),
            queue_max_attempts=_int("EVOAGENT_QUEUE_MAX_ATTEMPTS", 3),
            queue_lease_seconds=_int("EVOAGENT_QUEUE_LEASE_SECONDS", 60),
            queue_shutdown_timeout_seconds=_non_negative_int(
                "EVOAGENT_QUEUE_SHUTDOWN_TIMEOUT_SECONDS", 30
            ),
            shutdown_grace_seconds=_non_negative_float("EVOAGENT_SHUTDOWN_GRACE_SECONDS", 0.0),
            skill_timeout_seconds=_int("EVOAGENT_SKILL_TIMEOUT_SECONDS", 30),
            skill_memory_mb=_int("EVOAGENT_SKILL_MEMORY_MB", 256),
            skill_sandbox=_bool("EVOAGENT_SKILL_SANDBOX", True),
            skill_signing_key=os.getenv("EVOAGENT_SKILL_SIGNING_KEY", ""),
            skill_container_image=os.getenv("EVOAGENT_SKILL_CONTAINER_IMAGE", ""),
            skill_require_container=_bool("EVOAGENT_SKILL_REQUIRE_CONTAINER", False),
            repair_test_command=os.getenv("EVOAGENT_REPAIR_TEST_COMMAND", ""),
            repair_verify_timeout_seconds=_int("EVOAGENT_REPAIR_VERIFY_TIMEOUT_SECONDS", 120),
            repair_container_image=os.getenv("EVOAGENT_REPAIR_CONTAINER_IMAGE", ""),
            repair_memory_mb=_int("EVOAGENT_REPAIR_MEMORY_MB", 1024),
            repair_pids_limit=_int("EVOAGENT_REPAIR_PIDS_LIMIT", 256),
            repair_cpus=_positive_float("EVOAGENT_REPAIR_CPUS", 1.0),
            repair_max_output_bytes=_int("EVOAGENT_REPAIR_MAX_OUTPUT_BYTES", 16000),
            proof_executor_socket=os.getenv("EVOAGENT_PROOF_EXECUTOR_SOCKET", ""),
            otel_endpoint=os.getenv("EVOAGENT_OTEL_ENDPOINT", ""),
            otel_service_name=os.getenv("EVOAGENT_OTEL_SERVICE_NAME", "evoagent"),
            alert_failure_rate=float(os.getenv("EVOAGENT_ALERT_FAILURE_RATE", "0.20")),
            alert_min_samples=_int("EVOAGENT_ALERT_MIN_SAMPLES", 10),
            rate_limit_rps=_non_negative_int("EVOAGENT_RATE_LIMIT_RPS", 0),
            rate_limit_burst=_non_negative_int("EVOAGENT_RATE_LIMIT_BURST", 0),
            trusted_proxy_cidrs=_csv("EVOAGENT_TRUSTED_PROXY_CIDRS"),
            max_inflight_heavy=_non_negative_int("EVOAGENT_MAX_INFLIGHT_HEAVY", 0),
            max_http_connections=_int("EVOAGENT_MAX_HTTP_CONNECTIONS", 128),
            history_retention_days=_non_negative_int("EVOAGENT_HISTORY_RETENTION_DAYS", 0),
            history_maintenance_seconds=_int("EVOAGENT_HISTORY_MAINTENANCE_SECONDS", 3600),
            history_prune_batch_size=_int("EVOAGENT_HISTORY_PRUNE_BATCH_SIZE", 1000),
            tenant_max_active_reviews=_non_negative_int("EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS", 0),
            tenant_capacity_retry_seconds=_int("EVOAGENT_TENANT_CAPACITY_RETRY_SECONDS", 5),
            breaker_failure_threshold=_int("EVOAGENT_BREAKER_FAILURE_THRESHOLD", 5),
            breaker_reset_seconds=_int("EVOAGENT_BREAKER_RESET_SECONDS", 30),
            pg_pool_min=_non_negative_int("EVOAGENT_PG_POOL_MIN", 1),
            pg_pool_max=_int("EVOAGENT_PG_POOL_MAX", 10),
            pg_pool_timeout=_int("EVOAGENT_PG_POOL_TIMEOUT", 10),
            pg_statement_timeout_seconds=_int("EVOAGENT_PG_STATEMENT_TIMEOUT_SECONDS", 120),
            outbox_poll_seconds=_positive_float("EVOAGENT_OUTBOX_POLL_SECONDS", 0.25),
            outbox_batch_size=_int("EVOAGENT_OUTBOX_BATCH_SIZE", 50),
            outbox_lease_seconds=_int("EVOAGENT_OUTBOX_LEASE_SECONDS", 30),
            outbox_max_attempts=_int("EVOAGENT_OUTBOX_MAX_ATTEMPTS", 20),
            effect_lease_seconds=_int("EVOAGENT_EFFECT_LEASE_SECONDS", 300),
        )
