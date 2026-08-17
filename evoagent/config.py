import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from ipaddress import ip_network
from typing import Any

SOURCE_SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
INSTALLED_SKILLS_DIR = os.path.join(sys.prefix, "share", "evoagent", "skills")
DEFAULT_SKILLS_DIR = SOURCE_SKILLS_DIR if os.path.isdir(SOURCE_SKILLS_DIR) else INSTALLED_SKILLS_DIR
_PROOF_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_QUEUE_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError("%s must be non-negative" % name)
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: str
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
    public_base_url: str = "http://127.0.0.1:8080"
    llm_provider: str = "local"
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_site_url: str = ""
    openrouter_app_name: str = "EvoAgent"
    llm_allowed_hosts: tuple[str, ...] = ()
    llm_max_input_tokens: int = 120000
    llm_max_output_tokens: int = 4096
    llm_daily_token_budget: int = 0
    llm_daily_cost_micros: int = 0
    llm_input_cost_micros_per_million: int = 0
    llm_output_cost_micros_per_million: int = 0
    llm_routes_file: str = ""
    llm_fallback_attempts: int = 1
    llm_reservation_ttl_seconds: int = 600
    llm_shadow_workers: int = 2
    llm_shadow_max_inflight: int = 8
    llm_shadow_daily_token_budget: int = 0
    llm_shadow_daily_cost_micros: int = 0
    llm_capacity_lease_seconds: int = 180
    llm_capacity_window_retention_hours: int = 48
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
    queue_fair_scheduling: bool = False
    queue_tenant_weights_file: str = ""
    queue_redis_cluster: bool = False
    queue_namespace: str = ""
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
    repair_require_container: bool = False
    repair_max_output_bytes: int = 16000
    proof_runner_url: str = ""
    proof_runner_signing_key: str = ""
    proof_runner_signing_key_id: str = "default"
    proof_runner_allowed_hosts: tuple[str, ...] = ()
    proof_runner_timeout_seconds: int = 150
    proof_runner_max_request_bytes: int = 12 * 1024 * 1024
    proof_runner_max_response_bytes: int = 128 * 1024
    proof_runner_replay_window_seconds: int = 300
    proof_require_remote: bool = False
    otel_endpoint: str = ""
    otel_service_name: str = "evoagent"
    alert_failure_rate: float = 0.20
    alert_min_samples: int = 10
    alert_window_seconds: int = 900
    alert_webhook_url: str = ""
    alert_smtp_host: str = ""
    alert_email_to: str = ""
    continuous_eval_seconds: int = 0
    web_workers: int = 1
    rate_limit_rps: int = 0
    rate_limit_burst: int = 0
    trusted_proxy_cidrs: tuple[str, ...] = ()
    max_inflight_heavy: int = 0
    history_retention_days: int = 0
    history_maintenance_seconds: int = 3600
    history_prune_batch_size: int = 1000
    tenant_max_active_reviews: int = 0
    tenant_capacity_retry_seconds: int = 5
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: int = 30
    outbound_retries: int = 2
    pg_pool_min: int = 1
    pg_pool_max: int = 10
    pg_pool_timeout: int = 10
    plugin_profile_path: str = ""
    plugin_profile_layers: tuple[str, ...] = ()
    plugin_discovery: bool = False
    plugin_allowlist: tuple[str, ...] = ()
    outbox_poll_seconds: float = 0.25
    outbox_batch_size: int = 50
    outbox_lease_seconds: int = 30
    outbox_max_attempts: int = 20
    effect_lease_seconds: int = 300

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
        if bool(self.bootstrap_admin_username) != bool(self.bootstrap_admin_password):
            raise ValueError("bootstrap admin username and password must be configured together")
        if not 0.0 <= self.alert_failure_rate <= 1.0:
            raise ValueError("EVOAGENT_ALERT_FAILURE_RATE must be between 0 and 1")
        if self.plugin_discovery and not self.plugin_allowlist:
            raise ValueError(
                "EVOAGENT_PLUGIN_ALLOWLIST is required when trusted plugin discovery is enabled"
            )
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
        if self.queue_fair_scheduling and not self.redis_url:
            raise ValueError("EVOAGENT_QUEUE_FAIR_SCHEDULING requires EVOAGENT_REDIS_URL")
        if self.queue_namespace and not self.redis_url:
            raise ValueError("EVOAGENT_QUEUE_NAMESPACE requires EVOAGENT_REDIS_URL")
        if self.queue_namespace and not _QUEUE_NAMESPACE.fullmatch(self.queue_namespace):
            raise ValueError("EVOAGENT_QUEUE_NAMESPACE is not a canonical queue namespace")
        if self.queue_redis_cluster and not self.queue_namespace:
            raise ValueError("EVOAGENT_REDIS_CLUSTER requires EVOAGENT_QUEUE_NAMESPACE")
        if self.queue_redis_cluster:
            parsed_redis = urllib.parse.urlsplit(self.redis_url)
            query_database = urllib.parse.parse_qs(parsed_redis.query, keep_blank_values=True).get(
                "db", ["0"]
            )
            if parsed_redis.path.removeprefix("/") not in {"", "0"} or query_database != ["0"]:
                raise ValueError("EVOAGENT_REDIS_CLUSTER cannot select a logical database")
        if (
            (self.llm_daily_cost_micros > 0 or self.llm_shadow_daily_cost_micros > 0)
            and self.llm_input_cost_micros_per_million == 0
            and self.llm_output_cost_micros_per_million == 0
            and not self.llm_routes_file
        ):
            raise ValueError(
                "model input or output pricing is required when the daily cost budget is enabled"
            )
        if self.llm_reservation_ttl_seconds <= self.timeout_seconds:
            raise ValueError(
                "EVOAGENT_LLM_RESERVATION_TTL_SECONDS must exceed EVOAGENT_TIMEOUT_SECONDS"
            )
        if self.llm_capacity_lease_seconds <= self.timeout_seconds:
            raise ValueError(
                "EVOAGENT_LLM_CAPACITY_LEASE_SECONDS must exceed EVOAGENT_TIMEOUT_SECONDS"
            )
        if not 1 <= self.llm_capacity_window_retention_hours <= 24 * 30:
            raise ValueError(
                "EVOAGENT_LLM_CAPACITY_WINDOW_RETENTION_HOURS must be between 1 and 720"
            )
        if self.llm_shadow_workers > 32:
            raise ValueError("EVOAGENT_LLM_SHADOW_WORKERS must be at most 32")
        if self.llm_shadow_max_inflight <= 0:
            raise ValueError("EVOAGENT_LLM_SHADOW_MAX_INFLIGHT must be positive")
        if self.llm_shadow_workers > self.llm_shadow_max_inflight:
            raise ValueError(
                "EVOAGENT_LLM_SHADOW_WORKERS cannot exceed EVOAGENT_LLM_SHADOW_MAX_INFLIGHT"
            )
        if self.proof_require_remote and not self.proof_runner_url:
            raise ValueError(
                "EVOAGENT_PROOF_RUNNER_URL is required when remote proof execution is mandatory"
            )
        if self.proof_runner_url:
            if len(self.proof_runner_signing_key.encode("utf-8")) < 32:
                raise ValueError("EVOAGENT_PROOF_RUNNER_SIGNING_KEY must contain at least 32 bytes")
            if not _PROOF_KEY_ID.fullmatch(self.proof_runner_signing_key_id):
                raise ValueError("EVOAGENT_PROOF_RUNNER_SIGNING_KEY_ID is invalid")
            if not self.proof_runner_allowed_hosts:
                raise ValueError(
                    "EVOAGENT_PROOF_RUNNER_ALLOWED_HOSTS is required for remote proof execution"
                )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("EVOAGENT_HOST", "127.0.0.1"),
            port=_int("EVOAGENT_PORT", 8080),
            db_path=os.getenv("EVOAGENT_DB_PATH", "evoagent.db"),
            max_diff_bytes=_int("EVOAGENT_MAX_DIFF_BYTES", 1024 * 1024),
            max_steps=_int("EVOAGENT_MAX_STEPS", 8),
            timeout_seconds=_int("EVOAGENT_TIMEOUT_SECONDS", 120),
            llm_base_url=os.getenv("EVOAGENT_LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=os.getenv("EVOAGENT_LLM_API_KEY", ""),
            llm_model=os.getenv("EVOAGENT_LLM_MODEL", ""),
            github_webhook_secret=os.getenv("EVOAGENT_GITHUB_WEBHOOK_SECRET", ""),
            github_token=os.getenv("EVOAGENT_GITHUB_TOKEN", ""),
            auto_post_review=_bool("EVOAGENT_AUTO_POST_REVIEW"),
            database_url=os.getenv("EVOAGENT_DATABASE_URL", ""),
            redis_url=os.getenv("EVOAGENT_REDIS_URL", ""),
            async_workers=_int("EVOAGENT_ASYNC_WORKERS", 2),
            skills_dir=os.getenv("EVOAGENT_SKILLS_DIR", DEFAULT_SKILLS_DIR),
            github_app_id=os.getenv("EVOAGENT_GITHUB_APP_ID", ""),
            github_app_slug=os.getenv("EVOAGENT_GITHUB_APP_SLUG", ""),
            github_private_key_path=os.getenv("EVOAGENT_GITHUB_PRIVATE_KEY_PATH", ""),
            public_base_url=os.getenv("EVOAGENT_PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip(
                "/"
            ),
            llm_provider=os.getenv("EVOAGENT_LLM_PROVIDER", "local"),
            deepseek_api_key=os.getenv("EVOAGENT_DEEPSEEK_API_KEY", ""),
            openrouter_api_key=os.getenv("EVOAGENT_OPENROUTER_API_KEY", ""),
            openrouter_site_url=os.getenv("EVOAGENT_OPENROUTER_SITE_URL", ""),
            openrouter_app_name=os.getenv("EVOAGENT_OPENROUTER_APP_NAME", "EvoAgent"),
            llm_allowed_hosts=_csv("EVOAGENT_LLM_ALLOWED_HOSTS"),
            llm_max_input_tokens=_int("EVOAGENT_LLM_MAX_INPUT_TOKENS", 120000),
            llm_max_output_tokens=_int("EVOAGENT_LLM_MAX_OUTPUT_TOKENS", 4096),
            llm_daily_token_budget=_non_negative_int("EVOAGENT_LLM_DAILY_TOKEN_BUDGET", 0),
            llm_daily_cost_micros=_non_negative_int("EVOAGENT_LLM_DAILY_COST_MICROS", 0),
            llm_input_cost_micros_per_million=_non_negative_int(
                "EVOAGENT_LLM_INPUT_COST_MICROS_PER_MILLION", 0
            ),
            llm_output_cost_micros_per_million=_non_negative_int(
                "EVOAGENT_LLM_OUTPUT_COST_MICROS_PER_MILLION", 0
            ),
            llm_routes_file=os.getenv("EVOAGENT_LLM_ROUTES_FILE", ""),
            llm_fallback_attempts=_non_negative_int("EVOAGENT_LLM_FALLBACK_ATTEMPTS", 1),
            llm_reservation_ttl_seconds=_int("EVOAGENT_LLM_RESERVATION_TTL_SECONDS", 600),
            llm_shadow_workers=_non_negative_int("EVOAGENT_LLM_SHADOW_WORKERS", 2),
            llm_shadow_max_inflight=_int("EVOAGENT_LLM_SHADOW_MAX_INFLIGHT", 8),
            llm_shadow_daily_token_budget=_non_negative_int(
                "EVOAGENT_LLM_SHADOW_DAILY_TOKEN_BUDGET", 0
            ),
            llm_shadow_daily_cost_micros=_non_negative_int(
                "EVOAGENT_LLM_SHADOW_DAILY_COST_MICROS", 0
            ),
            llm_capacity_lease_seconds=_int("EVOAGENT_LLM_CAPACITY_LEASE_SECONDS", 180),
            llm_capacity_window_retention_hours=_int(
                "EVOAGENT_LLM_CAPACITY_WINDOW_RETENTION_HOURS", 48
            ),
            eval_max_cases=_int("EVOAGENT_EVAL_MAX_CASES", 5),
            eval_min_cases=_int("EVOAGENT_EVAL_MIN_CASES", 3),
            eval_min_improvement=float(os.getenv("EVOAGENT_EVAL_MIN_IMPROVEMENT", "0.01")),
            eval_min_holdout_cases=_non_negative_int("EVOAGENT_EVAL_MIN_HOLDOUT_CASES", 2),
            eval_max_metric_regression=float(os.getenv("EVOAGENT_EVAL_MAX_METRIC_REGRESSION", "0")),
            auth_required=_bool("EVOAGENT_AUTH_REQUIRED", False),
            auth_secret=os.getenv("EVOAGENT_AUTH_SECRET", ""),
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
            queue_fair_scheduling=_bool("EVOAGENT_QUEUE_FAIR_SCHEDULING", False),
            queue_tenant_weights_file=os.getenv("EVOAGENT_QUEUE_TENANT_WEIGHTS_FILE", ""),
            queue_redis_cluster=_bool("EVOAGENT_REDIS_CLUSTER", False),
            queue_namespace=os.getenv("EVOAGENT_QUEUE_NAMESPACE", ""),
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
            repair_cpus=float(os.getenv("EVOAGENT_REPAIR_CPUS", "1.0")),
            repair_require_container=_bool("EVOAGENT_REPAIR_REQUIRE_CONTAINER", False),
            repair_max_output_bytes=_int("EVOAGENT_REPAIR_MAX_OUTPUT_BYTES", 16000),
            proof_runner_url=os.getenv("EVOAGENT_PROOF_RUNNER_URL", "").rstrip("/"),
            proof_runner_signing_key=os.getenv("EVOAGENT_PROOF_RUNNER_SIGNING_KEY", ""),
            proof_runner_signing_key_id=os.getenv(
                "EVOAGENT_PROOF_RUNNER_SIGNING_KEY_ID", "default"
            ),
            proof_runner_allowed_hosts=_csv("EVOAGENT_PROOF_RUNNER_ALLOWED_HOSTS"),
            proof_runner_timeout_seconds=_int("EVOAGENT_PROOF_RUNNER_TIMEOUT_SECONDS", 150),
            proof_runner_max_request_bytes=_int(
                "EVOAGENT_PROOF_RUNNER_MAX_REQUEST_BYTES", 12 * 1024 * 1024
            ),
            proof_runner_max_response_bytes=_int(
                "EVOAGENT_PROOF_RUNNER_MAX_RESPONSE_BYTES", 128 * 1024
            ),
            proof_runner_replay_window_seconds=_int(
                "EVOAGENT_PROOF_RUNNER_REPLAY_WINDOW_SECONDS", 300
            ),
            proof_require_remote=_bool("EVOAGENT_PROOF_REQUIRE_REMOTE", False),
            otel_endpoint=os.getenv("EVOAGENT_OTEL_ENDPOINT", ""),
            otel_service_name=os.getenv("EVOAGENT_OTEL_SERVICE_NAME", "evoagent"),
            alert_failure_rate=float(os.getenv("EVOAGENT_ALERT_FAILURE_RATE", "0.20")),
            alert_min_samples=_int("EVOAGENT_ALERT_MIN_SAMPLES", 10),
            alert_window_seconds=_int("EVOAGENT_ALERT_WINDOW_SECONDS", 900),
            alert_webhook_url=os.getenv("EVOAGENT_ALERT_WEBHOOK_URL", ""),
            alert_smtp_host=os.getenv("EVOAGENT_ALERT_SMTP_HOST", ""),
            alert_email_to=os.getenv("EVOAGENT_ALERT_EMAIL_TO", ""),
            continuous_eval_seconds=_non_negative_int("EVOAGENT_CONTINUOUS_EVAL_SECONDS", 0),
            web_workers=_int("EVOAGENT_WEB_WORKERS", 1),
            rate_limit_rps=_non_negative_int("EVOAGENT_RATE_LIMIT_RPS", 0),
            rate_limit_burst=_non_negative_int("EVOAGENT_RATE_LIMIT_BURST", 0),
            trusted_proxy_cidrs=_csv("EVOAGENT_TRUSTED_PROXY_CIDRS"),
            max_inflight_heavy=_non_negative_int("EVOAGENT_MAX_INFLIGHT_HEAVY", 0),
            history_retention_days=_non_negative_int("EVOAGENT_HISTORY_RETENTION_DAYS", 0),
            history_maintenance_seconds=_int("EVOAGENT_HISTORY_MAINTENANCE_SECONDS", 3600),
            history_prune_batch_size=_int("EVOAGENT_HISTORY_PRUNE_BATCH_SIZE", 1000),
            tenant_max_active_reviews=_non_negative_int("EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS", 0),
            tenant_capacity_retry_seconds=_int("EVOAGENT_TENANT_CAPACITY_RETRY_SECONDS", 5),
            breaker_failure_threshold=_int("EVOAGENT_BREAKER_FAILURE_THRESHOLD", 5),
            breaker_reset_seconds=_int("EVOAGENT_BREAKER_RESET_SECONDS", 30),
            outbound_retries=_non_negative_int("EVOAGENT_OUTBOUND_RETRIES", 2),
            pg_pool_min=_non_negative_int("EVOAGENT_PG_POOL_MIN", 1),
            pg_pool_max=_int("EVOAGENT_PG_POOL_MAX", 10),
            pg_pool_timeout=_int("EVOAGENT_PG_POOL_TIMEOUT", 10),
            plugin_profile_path=os.getenv("EVOAGENT_PLUGIN_PROFILE", ""),
            plugin_profile_layers=_csv("EVOAGENT_PLUGIN_PROFILE_LAYERS"),
            plugin_discovery=_bool("EVOAGENT_PLUGIN_DISCOVERY", False),
            plugin_allowlist=_csv("EVOAGENT_PLUGIN_ALLOWLIST"),
            outbox_poll_seconds=_positive_float("EVOAGENT_OUTBOX_POLL_SECONDS", 0.25),
            outbox_batch_size=_int("EVOAGENT_OUTBOX_BATCH_SIZE", 50),
            outbox_lease_seconds=_int("EVOAGENT_OUTBOX_LEASE_SECONDS", 30),
            outbox_max_attempts=_int("EVOAGENT_OUTBOX_MAX_ATTEMPTS", 20),
            effect_lease_seconds=_int("EVOAGENT_EFFECT_LEASE_SECONDS", 300),
        )
