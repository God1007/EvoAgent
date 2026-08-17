import json
import os
import tempfile
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

from evoagent.circuit_breaker import CircuitBreaker
from evoagent.diff_parser import parse_unified_diff
from evoagent.model_gateway import (
    EnterpriseModelGateway,
    ModelGatewayOptions,
    ModelGovernanceContext,
    ModelMessage,
    ModelOutputError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelRoute,
    OpenAICompatibleModelProvider,
    load_model_routes,
    redact_model_messages,
)
from evoagent.reviewer import GatewayReviewer
from evoagent.store import TaskStore


class FakeProvider:
    def __init__(self, content='{"findings":[]}'):
        self.content = content
        self.calls = []

    def complete(self, route, messages, max_output_tokens, require_json_object):
        self.calls.append((route, messages, max_output_tokens, require_json_object))
        return ModelResponse(self.content, route.provider, route.model, 12, 5, "upstream-1")


class FailingProvider:
    def complete(self, route, messages, max_output_tokens, require_json_object):
        raise RuntimeError("upstream echoed credential %s" % route.api_key)


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        self.store = TaskStore(self.path)
        self.route = ModelRoute(
            "test-provider",
            "model-a",
            "https://models.example/v1",
            "secret-key",
            input_cost_micros_per_million=1_000_000,
            output_cost_micros_per_million=2_000_000,
        )

    def gateway(self, provider=None, **options):
        return EnterpriseModelGateway(
            self.store,
            self.route,
            provider or FakeProvider(),
            ModelGatewayOptions(
                allowed_hosts=("models.example",),
                max_input_tokens=options.get("max_input_tokens", 1000),
                max_output_tokens=options.get("max_output_tokens", 100),
                daily_token_budget=options.get("daily_token_budget", 0),
                daily_cost_micros=options.get("daily_cost_micros", 0),
            ),
        )

    def request(self, content="review this"):
        return ModelRequest(
            "tenant-a",
            "org/repo",
            "task-1",
            "review",
            (ModelMessage("user", content),),
        )

    def test_redacts_credentials_and_records_only_governance_metadata(self):
        provider = FakeProvider()
        gateway = self.gateway(provider)
        response = gateway.complete(
            self.request('password = "super-secret"\nAuthorization: Bearer abcdefghijk')
        )

        sent = provider.calls[0][1][0].content
        self.assertNotIn("super-secret", sent)
        self.assertNotIn("abcdefghijk", sent)
        self.assertIn("<redacted>", sent)
        self.assertEqual("upstream-1", response.request_id)
        usage = self.store.list_model_usage("tenant-a", "org/repo")
        self.assertEqual("success", usage[0]["status"])
        self.assertEqual(2, usage[0]["redactions"])
        self.assertEqual(22, usage[0]["cost_micros"])
        self.assertNotIn("super-secret", json.dumps(usage))

    def test_budget_rejection_happens_before_provider_call(self):
        provider = FakeProvider()
        gateway = self.gateway(provider, daily_token_budget=1)

        with self.assertRaisesRegex(PermissionError, "budget is exhausted"):
            gateway.complete(self.request())

        self.assertEqual([], provider.calls)
        self.assertEqual([], self.store.list_model_usage("tenant-a", "org/repo"))

    def test_gateway_startup_quarantines_stale_reservations_and_throttles_scans(self):
        self.store.reserve_model_usage(
            {
                "request_id": "stale-request",
                "tenant_id": "tenant-a",
                "repository": "org/repo",
                "purpose": "review",
                "provider": "test-provider",
                "model": "model-a",
                "reserved_tokens": 100,
                "request_sha256": "a" * 64,
                "created_at": "2000-01-01T00:00:00+00:00",
            },
            "2000-01-01T00:00:00+00:00",
        )
        with mock.patch.object(
            self.store,
            "expire_model_usage_reservations",
            wraps=self.store.expire_model_usage_reservations,
        ) as expire:
            gateway = self.gateway()
            gateway.complete(self.request())
            gateway.complete(self.request())

        self.assertEqual(1, expire.call_count)
        usage = self.store.list_model_usage("tenant-a", "org/repo")
        self.assertIn("uncertain", {item["status"] for item in usage})

    def test_invalid_structured_output_is_a_failed_usage_record(self):
        gateway = self.gateway(FakeProvider("not-json"))

        with self.assertRaises(ModelOutputError):
            gateway.complete(self.request())

        usage = self.store.list_model_usage("tenant-a", "org/repo")
        self.assertEqual("failed", usage[0]["status"])
        self.assertIn("not valid JSON", usage[0]["error"])
        self.assertEqual(12, usage[0]["input_tokens"])
        self.assertEqual(5, usage[0]["output_tokens"])
        self.assertEqual(22, usage[0]["cost_micros"])

    def test_provider_errors_cannot_persist_or_raise_route_credentials(self):
        gateway = self.gateway(FailingProvider())

        with self.assertRaisesRegex(RuntimeError, "<redacted>") as raised:
            gateway.complete(self.request())

        self.assertNotIn("secret-key", str(raised.exception))
        usage = self.store.list_model_usage("tenant-a", "org/repo")
        self.assertNotIn("secret-key", usage[0]["error"])
        self.assertIn("<redacted>", usage[0]["error"])
        self.assertEqual(0, usage[0]["cost_micros"])

    def test_provider_reported_output_over_cap_is_rejected(self):
        provider = FakeProvider()
        provider.complete = lambda route, messages, cap, require_json: ModelResponse(
            '{"findings":[]}', route.provider, route.model, 12, cap + 1, "oversized"
        )
        gateway = self.gateway(provider, max_output_tokens=10)

        with self.assertRaisesRegex(ModelOutputError, "output-token limit"):
            gateway.complete(self.request())

        self.assertEqual("failed", self.store.list_model_usage("tenant-a", "org/repo")[0]["status"])

    def test_route_host_is_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "allowed"):
            EnterpriseModelGateway(
                self.store,
                self.route,
                FakeProvider(),
                ModelGatewayOptions(allowed_hosts=("approved.example",)),
            )

    def test_private_key_redaction_preserves_line_count(self):
        content = (
            "before\n-----BEGIN PRIVATE KEY-----\nline-a\nline-b\n-----END PRIVATE KEY-----\nafter"
        )
        redacted, count = redact_model_messages((ModelMessage("user", content),))
        self.assertEqual(1, count)
        self.assertEqual(content.count("\n"), redacted[0].content.count("\n"))
        self.assertNotIn("line-a", redacted[0].content)

    def test_gateway_reviewer_resolves_task_scope(self):
        finding = {
            "rule_id": "LLM-1",
            "severity": "high",
            "title": "danger",
            "explanation": "specific explanation",
            "path": "a.py",
            "line": 1,
            "evidence": "eval(value)",
            "fix": "remove eval",
            "test": "assert input is data",
        }
        gateway = self.gateway(FakeProvider(json.dumps({"findings": [finding]})))
        reviewer = GatewayReviewer(
            gateway,
            lambda _task_id: ModelGovernanceContext("tenant-a", "org/repo"),
        )
        diff = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,1 @@\n+eval(value)\n"

        findings = reviewer.review_with_context("task-1", diff, parse_unified_diff(diff))

        self.assertEqual(["LLM-1"], [item.rule_id for item in findings])
        usage = self.store.list_model_usage("tenant-a", "org/repo")
        self.assertEqual("task-1", usage[0]["task_id"])

    def test_transient_primary_failure_uses_one_bounded_fallback(self):
        primary = ModelRoute(
            "provider-a",
            "model-a",
            "https://a.example/v1",
            "secret-a",
            route_id="primary",
            priority=10,
            region="us",
        )
        secondary = ModelRoute(
            "provider-b",
            "model-b",
            "https://b.example/v1",
            "secret-b",
            route_id="secondary",
            priority=20,
            region="us",
        )

        class TransientProvider:
            def complete(self, route, messages, max_output_tokens, require_json_object):
                raise ModelProviderError("temporarily unavailable", transient=True)

        fallback = FakeProvider()
        gateway = EnterpriseModelGateway(
            self.store,
            (secondary, primary),
            {"primary": TransientProvider(), "secondary": fallback},
            ModelGatewayOptions(
                allowed_hosts=("a.example", "b.example"),
                max_input_tokens=1000,
                max_output_tokens=100,
                fallback_attempts=1,
            ),
        )

        response = gateway.complete(self.request())

        self.assertEqual("provider-b", response.provider)
        usage = sorted(
            self.store.list_model_usage("tenant-a", "org/repo"),
            key=lambda item: item["attempt"],
        )
        self.assertEqual(["primary", "secondary"], [item["route_id"] for item in usage])
        self.assertEqual(["failed", "success"], [item["status"] for item in usage])
        self.assertEqual(1, len({item["root_request_id"] for item in usage}))

    def test_zero_fallback_budget_never_calls_secondary(self):
        primary = ModelRoute(
            "provider-a",
            "model-a",
            "https://a.example/v1",
            "secret-a",
            route_id="primary",
            priority=10,
        )
        secondary = ModelRoute(
            "provider-b",
            "model-b",
            "https://b.example/v1",
            "secret-b",
            route_id="secondary",
            priority=20,
        )

        class TransientProvider:
            def complete(self, route, messages, max_output_tokens, require_json_object):
                raise ModelProviderError("temporarily unavailable", transient=True)

        fallback = FakeProvider()
        gateway = EnterpriseModelGateway(
            self.store,
            (primary, secondary),
            {"primary": TransientProvider(), "secondary": fallback},
            ModelGatewayOptions(
                allowed_hosts=("a.example", "b.example"),
                fallback_attempts=0,
            ),
        )

        with self.assertRaisesRegex(ModelProviderError, "temporarily unavailable"):
            gateway.complete(self.request())
        self.assertEqual([], fallback.calls)

    def test_route_policy_filters_tenant_repository_provider_and_region(self):
        restricted = ModelRoute(
            "provider-eu",
            "model-eu",
            "https://eu.example/v1",
            "secret-eu",
            route_id="eu-payments",
            region="eu",
            tenant_ids=("tenant-a",),
            repository_patterns=("org/payments-*",),
        )
        gateway = EnterpriseModelGateway(
            self.store,
            (restricted,),
            FakeProvider(),
            ModelGatewayOptions(allowed_hosts=("eu.example",)),
        )
        request = ModelRequest(
            "tenant-a",
            "org/payments-api",
            "task-1",
            "review",
            (ModelMessage("user", "review"),),
            allowed_providers=("provider-eu",),
            allowed_models=("model-eu",),
            required_region="eu",
        )
        self.assertEqual(
            ("eu-payments",),
            tuple(
                item["route_id"] for item in gateway.route_catalog("tenant-a", "org/payments-api")
            ),
        )
        self.assertEqual((), gateway.route_catalog("tenant-b", "org/payments-api"))
        self.assertEqual("provider-eu", gateway.complete(request).provider)

        denied = self.request()
        with self.assertRaisesRegex(PermissionError, "governance policy"):
            gateway.complete(denied)

    def test_route_file_resolves_key_reference_without_storing_it_in_toml(self):
        handle, path = tempfile.mkstemp(suffix=".toml")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as output:
            output.write(
                'version = 1\n[[routes]]\nid = "eu-primary"\n'
                'provider = "provider-a"\nmodel = "model-a"\n'
                'base_url = "https://eu.example/v1"\n'
                'api_key_env = "TEST_MODEL_ROUTE_KEY"\npriority = 5\nregion = "eu"\n'
                'tenant_ids = ["tenant-a"]\nrepository_patterns = ["org/*"]\n'
            )
        with mock.patch.dict(os.environ, {"TEST_MODEL_ROUTE_KEY": "resolved-secret"}):
            routes = load_model_routes(path)

        self.assertEqual("eu-primary", routes[0].route_id)
        self.assertEqual("resolved-secret", routes[0].api_key)
        self.assertNotIn("resolved-secret", open(path, encoding="utf-8").read())


class OpenAICompatibleModelProviderTests(unittest.TestCase):
    def setUp(self):
        self.route = ModelRoute(
            "provider-a",
            "model-a",
            "https://models.example/v1",
            "route-secret",
        )
        self.messages = (ModelMessage("user", "review"),)

    def test_sends_bounded_structured_request_and_parses_usage(self):
        body = json.dumps(
            {
                "id": "response-1",
                "choices": [{"message": {"content": '{"findings":[]}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        ).encode()

        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        captured = []

        def open_request(request, timeout):
            captured.append((request, timeout))
            return Response(body)

        provider = OpenAICompatibleModelProvider(("models.example",), timeout_seconds=9)
        with mock.patch("evoagent.model_gateway.urllib.request.urlopen", open_request):
            response = provider.complete(self.route, self.messages, 41, True)

        request, timeout = captured[0]
        payload = json.loads(request.data)
        self.assertEqual("https://models.example/v1/chat/completions", request.full_url)
        self.assertEqual("Bearer route-secret", request.get_header("Authorization"))
        self.assertEqual(9, timeout)
        self.assertEqual(41, payload["max_tokens"])
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual(
            (7, 3, "response-1"),
            (
                response.input_tokens,
                response.output_tokens,
                response.request_id,
            ),
        )

    def test_transient_http_error_opens_breaker_and_redacts_echoed_key(self):
        breaker = CircuitBreaker("route", failure_threshold=1, reset_seconds=60)
        provider = OpenAICompatibleModelProvider(("models.example",), breaker=breaker)
        error = urllib.error.HTTPError(
            "https://models.example/v1/chat/completions",
            500,
            "failed",
            {},
            BytesIO(b"upstream echoed route-secret"),
        )
        with (
            mock.patch("evoagent.model_gateway.urllib.request.urlopen", side_effect=error),
            self.assertRaisesRegex(RuntimeError, "<redacted>") as raised,
        ):
            provider.complete(self.route, self.messages, 10, True)

        self.assertNotIn("route-secret", str(raised.exception))
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)

    def test_non_transient_http_error_does_not_open_transport_breaker(self):
        breaker = CircuitBreaker("route", failure_threshold=1, reset_seconds=60)
        provider = OpenAICompatibleModelProvider(("models.example",), breaker=breaker)
        error = urllib.error.HTTPError(
            "https://models.example/v1/chat/completions",
            401,
            "unauthorized",
            {},
            BytesIO(b"invalid credentials"),
        )
        with (
            mock.patch("evoagent.model_gateway.urllib.request.urlopen", side_effect=error),
            self.assertRaises(ModelProviderError) as raised,
        ):
            provider.complete(self.route, self.messages, 10, True)

        self.assertFalse(raised.exception.transient)
        self.assertEqual(CircuitBreaker.CLOSED, breaker.state)

    def test_response_body_cap_is_enforced(self):
        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        provider = OpenAICompatibleModelProvider(("models.example",), max_response_bytes=8)
        with (
            mock.patch(
                "evoagent.model_gateway.urllib.request.urlopen",
                return_value=Response(b"x" * 9),
            ),
            self.assertRaisesRegex(RuntimeError, "size limit"),
        ):
            provider.complete(self.route, self.messages, 10, True)


if __name__ == "__main__":
    unittest.main()
