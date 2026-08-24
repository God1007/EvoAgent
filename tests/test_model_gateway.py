import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from unittest import mock

from evoagent.circuit_breaker import CircuitBreaker, CircuitOpenError
from evoagent.errors import AccessDeniedError, ClientInputError
from evoagent.model_gateway import (
    MAX_MODEL_ROUTE_BYTES,
    ModelGateway,
    ModelGatewayOptions,
    ModelMessage,
    ModelOutputError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelRoute,
    OpenAICompatibleModelProvider,
    _NoRedirectHandler,
    load_model_route,
    redact_model_messages,
)


class FakeProvider:
    def __init__(self, content='{"findings":[]}'):
        self.content = content
        self.calls = []

    def complete(self, route, messages, max_output_tokens, require_json_object):
        self.calls.append((route, messages, max_output_tokens, require_json_object))
        return ModelResponse(self.content, route.provider, route.model, 12, 5, "upstream-1")


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.route = ModelRoute(
            "test-provider",
            "model-a",
            "https://models.example/v1",
            "secret-key",
            region="eu",
        )

    def gateway(self, provider=None, **options):
        return ModelGateway(
            self.route,
            provider or FakeProvider(),
            ModelGatewayOptions(
                allowed_hosts=("models.example",),
                max_input_tokens=options.get("max_input_tokens", 1000),
                max_output_tokens=options.get("max_output_tokens", 100),
                max_response_bytes=options.get("max_response_bytes", 1024),
            ),
        )

    def test_gateway_budgets_must_be_positive_integers(self):
        for name, value in (
            ("max_input_tokens", 0),
            ("max_output_tokens", 0),
            ("max_response_bytes", 0),
            ("max_input_tokens", True),
            ("max_response_bytes", float("nan")),
        ):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "limits"):
                self.gateway(**{name: value})

    def test_route_metadata_and_headers_are_validated_and_immutable(self):
        defaults = {
            "provider": "test",
            "model": "model",
            "base_url": "https://models.example/v1",
            "api_key": "key",
        }
        for changes in (
            {"provider": ""},
            {"model": " model"},
            {"api_key": "bad\nkey"},
            {"api_key": True},
            {"route_id": "bad route"},
            {"region": "eu\n"},
            {"headers": {"Authorization": "other"}},
            {"headers": {"bad name": "value"}},
            {"headers": {"X-Test": "bad\nvalue"}},
            {"headers": []},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "model route"):
                ModelRoute(**{**defaults, **changes})

        headers = {"X-Title": "original"}
        route = ModelRoute(**defaults, headers=headers)
        headers["X-Title"] = "changed"
        self.assertEqual("original", route.headers["X-Title"])
        with self.assertRaises(TypeError):
            route.headers["X-Title"] = "changed"

    @staticmethod
    def request(content="review this", **changes):
        values = {
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "task_id": "task-1",
            "purpose": "review",
            "messages": (ModelMessage("user", content),),
        }
        values.update(changes)
        return ModelRequest(**values)

    def test_redacts_credentials_before_provider_call(self):
        provider = FakeProvider()
        response = self.gateway(provider).complete(
            self.request('password = "super-secret"\nBearer abcdefghijk\nsecret-key')
        )

        sent = provider.calls[0][1][0].content
        self.assertNotIn("super-secret", sent)
        self.assertNotIn("abcdefghijk", sent)
        self.assertNotIn("secret-key", sent)
        self.assertEqual("upstream-1", response.request_id)

    def test_rejects_invalid_json_and_oversized_output(self):
        invalid = (
            "not-json",
            '{"confidence":NaN}',
            '{"findings":[],"findings":[]}',
            '{"ignored":"\\ud800"}',
            '{"nested":' + "[" * 10_000 + "0" + "]" * 10_000 + "}",
        )
        for content in invalid:
            with self.subTest(content=content[:40]), self.assertRaises(ModelOutputError):
                self.gateway(FakeProvider(content), max_response_bytes=30_000).complete(
                    self.request()
                )
        with self.assertRaises(ModelOutputError):
            self.gateway(
                FakeProvider(json.dumps({"value": "x" * 100})), max_response_bytes=20
            ).complete(self.request())

    def test_enforces_input_and_output_limits(self):
        with self.assertRaises(ClientInputError):
            self.gateway(max_input_tokens=1).complete(self.request("more than four bytes"))
        messages = tuple(ModelMessage("user", "x") for _ in range(4))
        with (
            mock.patch("evoagent.model_gateway._estimated_tokens", return_value=1) as estimate,
            self.assertRaises(ClientInputError),
        ):
            self.gateway(max_input_tokens=2).complete(self.request(messages=messages))
        self.assertEqual(3, estimate.call_count)
        provider = FakeProvider()
        with self.assertRaisesRegex(ClientInputError, "valid UTF-8"):
            self.gateway(provider).complete(self.request("\ud800"))
        self.assertEqual([], provider.calls)
        for value in (0, True, 1.5, 11):
            with self.subTest(value=value), self.assertRaises(ClientInputError):
                self.gateway(max_output_tokens=10).complete(self.request(max_output_tokens=value))
        with self.assertRaises(ClientInputError):
            self.gateway().complete(self.request(messages=(ModelMessage("tool", "x"),)))

    def test_enforces_repository_model_policy(self):
        for changes in (
            {"allowed_providers": ("other",)},
            {"allowed_models": ("other",)},
            {"required_region": "us"},
        ):
            with self.subTest(changes=changes), self.assertRaises(AccessDeniedError):
                self.gateway().complete(self.request(**changes))

    def test_governance_request_types_cannot_use_python_coercion(self):
        for changes in (
            {"tenant_id": 7},
            {"repository": ""},
            {"purpose": ""},
            {"allowed_providers": "prefix-test-provider-suffix"},
            {"allowed_providers": (7,)},
            {"allowed_models": "prefix-model-a-suffix"},
            {"required_region": 7},
            {"messages": [ModelMessage("user", "review")]},
            {"messages": ("review",)},
            {"require_json_object": "false"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ClientInputError):
                self.gateway().complete(self.request(**changes))

    def test_unconfigured_gateway_and_route_metadata(self):
        gateway = ModelGateway(None, None)
        self.assertFalse(gateway.configured)
        self.assertEqual("local", gateway.route_info()["provider"])
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            gateway.complete(self.request())
        self.assertEqual("default", self.gateway().route_info()["route_id"])

    def test_route_configuration_fails_before_the_first_request(self):
        for route, provider, message in (
            (self.route, None, "configured together"),
            (None, FakeProvider(), "configured together"),
            (
                ModelRoute("test", "model", "http://models.example/v1", "key"),
                FakeProvider(),
                "HTTPS",
            ),
            (
                ModelRoute("test", "model", "https://blocked.example/v1", "key"),
                FakeProvider(),
                "allowed",
            ),
            (
                ModelRoute("test", "model", " https://models.example/v1", "key"),
                FakeProvider(),
                "whitespace",
            ),
            (
                ModelRoute("test", "model", "https://models.example/v1\n", "key"),
                FakeProvider(),
                "whitespace",
            ),
            (
                ModelRoute("test", "model", "https://models.example:bad/v1", "key"),
                FakeProvider(),
                "port",
            ),
            (
                ModelRoute("test", "model", "https://models.example:70000/v1", "key"),
                FakeProvider(),
                "port",
            ),
            (
                ModelRoute("test", "model", "https://models.example:0/v1", "key"),
                FakeProvider(),
                "port",
            ),
            (
                ModelRoute("test", "model", "https://models.example:8443/v1", "key"),
                FakeProvider(),
                "port 443",
            ),
        ):
            with self.subTest(route=route, provider=provider):
                with self.assertRaisesRegex(ValueError, message):
                    ModelGateway(route, provider, self.gateway().options)

        ModelGateway(
            ModelRoute("test", "model", "http://127.0.0.1:8080/v1", "key"),
            FakeProvider(),
        )

    def test_execution_revision_binds_endpoint_and_limits_but_not_secrets(self):
        original = self.gateway().execution_revision()
        rotated = ModelRoute(
            self.route.provider,
            self.route.model,
            self.route.base_url,
            "rotated-secret",
            region=self.route.region,
        )

        self.assertEqual(
            original,
            ModelGateway(rotated, FakeProvider(), self.gateway().options).execution_revision(),
        )
        self.assertNotEqual(
            original,
            ModelGateway(
                ModelRoute(
                    self.route.provider,
                    self.route.model,
                    "https://models.example/v2",
                    self.route.api_key,
                    region=self.route.region,
                ),
                FakeProvider(),
                self.gateway().options,
            ).execution_revision(),
        )
        self.assertNotEqual(original, self.gateway(max_output_tokens=99).execution_revision())
        self.assertNotIn("secret", original)

    def test_provider_exception_cannot_echo_route_secret(self):
        class FailingProvider:
            def complete(self, *_args):
                raise RuntimeError("upstream echoed secret-key")

        with self.assertRaises(ModelProviderError) as raised:
            self.gateway(FailingProvider()).complete(self.request())
        self.assertNotIn("secret-key", str(raised.exception))

    def test_provider_response_must_satisfy_the_gateway_contract(self):
        class MalformedProvider:
            def __init__(self, response):
                self.response = response

            def complete(self, *_args):
                return self.response

        invalid = (
            None,
            ModelResponse("{}", "impersonated", self.route.model, 1, 1, "request-1"),
            ModelResponse("{}", self.route.provider, self.route.model, True, 1, "request-1"),
            ModelResponse("{}", self.route.provider, self.route.model, 1, 101, "request-1"),
            ModelResponse("\ud800", self.route.provider, self.route.model, 1, 1, "request-1"),
        )

        for response in invalid:
            with (
                self.subTest(response=response),
                self.assertRaisesRegex(ModelOutputError, "gateway contract"),
            ):
                self.gateway(MalformedProvider(response)).complete(self.request())

    def test_redaction_preserves_message_roles_and_line_count(self):
        original = (ModelMessage("user", "token='value123'\nnext"),)
        redacted, count = redact_model_messages(original)
        self.assertEqual("user", redacted[0].role)
        self.assertEqual(original[0].content.count("\n"), redacted[0].content.count("\n"))
        self.assertEqual(1, count)


class RouteFileTests(unittest.TestCase):
    def write(self, content):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_loads_one_environment_backed_route(self):
        path = self.write(
            'version = 1\n[[routes]]\nid = "primary"\nprovider = "test"\n'
            'model = "model-a"\nbase_url = "https://models.example/v1"\n'
            'api_key_env = "TEST_MODEL_KEY"\nregion = "eu"\n'
        )
        with mock.patch.dict(os.environ, {"TEST_MODEL_KEY": "secret"}):
            route = load_model_route(path)
        self.assertEqual(("primary", "model-a", "eu"), (route.route_id, route.model, route.region))
        self.assertNotIn("secret", repr(route))

    def test_rejects_multiple_or_speculative_route_fields(self):
        documents = (
            'version = 1\n[[routes]]\nid="a"\nprovider="p"\nmodel="m"\n'
            'base_url="https://a.example"\napi_key_env="TEST_KEY"\n'
            '[[routes]]\nid="b"\nprovider="p"\nmodel="m"\n'
            'base_url="https://b.example"\napi_key_env="TEST_KEY"\n',
            'version = 1\n[[routes]]\nid="a"\nprovider="p"\nmodel="m"\n'
            'base_url="https://a.example"\napi_key_env="TEST_KEY"\nweight=2\n',
        )
        with mock.patch.dict(os.environ, {"TEST_KEY": "secret"}):
            for document in documents:
                with self.subTest(document=document), self.assertRaises(ValueError):
                    load_model_route(self.write(document))

    def test_version_boolean_is_not_version_one(self):
        document = (
            'version = true\n[[routes]]\nid="a"\nprovider="p"\nmodel="m"\n'
            'base_url="https://a.example"\napi_key_env="TEST_KEY"\n'
        )
        with (
            mock.patch.dict(os.environ, {"TEST_KEY": "secret"}),
            self.assertRaisesRegex(ValueError, "version 1"),
        ):
            load_model_route(self.write(document))

    def test_rejects_oversized_route_before_toml_parsing(self):
        path = self.write(" " * (MAX_MODEL_ROUTE_BYTES + 1))

        with self.assertRaisesRegex(ValueError, "exceeds the 64 KiB limit"):
            load_model_route(path)


class ProviderTests(unittest.TestCase):
    def test_transport_limits_cannot_be_disabled(self):
        for kwargs in (
            {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": True},
            {"max_response_bytes": 0},
            {"max_response_bytes": True},
            {"max_response_bytes": float("nan")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                OpenAICompatibleModelProvider(**kwargs)

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def setUp(self):
        self.route = ModelRoute("test", "model", "https://approved.example/v1", "key-value")

    def test_disables_environment_proxies(self):
        with mock.patch("urllib.request.build_opener", wraps=urllib.request.build_opener) as build:
            OpenAICompatibleModelProvider(("approved.example",))

        proxy = next(
            arg for arg in build.call_args.args if isinstance(arg, urllib.request.ProxyHandler)
        )
        self.assertEqual({}, proxy.proxies)

    def test_uses_allowlisted_https_endpoint_and_bounds_response(self):
        body = json.dumps(
            {
                "id": "request-1",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        ).encode()
        provider = OpenAICompatibleModelProvider(("approved.example",), max_response_bytes=1024)
        with mock.patch.object(
            provider._opener, "open", return_value=self.Response(body)
        ) as opened:
            response = provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)
        self.assertEqual("request-1", response.request_id)
        self.assertEqual(
            "https://approved.example/v1/chat/completions", opened.call_args.args[0].full_url
        )

    def test_rejects_malformed_metadata_without_coercion(self):
        provider = OpenAICompatibleModelProvider(("approved.example",), max_response_bytes=1024)
        valid_usage = {"prompt_tokens": 2, "completion_tokens": 1}
        for request_id, usage in (
            ("request-1", [1]),
            ("request-1", {"prompt_tokens": True, "completion_tokens": 1}),
            ("request-1", {"prompt_tokens": "2", "completion_tokens": 1}),
            ("request-1", {"prompt_tokens": 1.5, "completion_tokens": 1}),
            ("request-1", {"prompt_tokens": -1, "completion_tokens": 1}),
            (7, valid_usage),
        ):
            body = json.dumps(
                {
                    "id": request_id,
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": usage,
                }
            ).encode()
            with (
                self.subTest(request_id=request_id, usage=usage),
                mock.patch.object(provider._opener, "open", return_value=self.Response(body)),
                self.assertRaises(ModelProviderError),
            ):
                provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)

    def test_rejects_nonstandard_json_in_provider_envelope(self):
        bodies = (
            b'{"id":"request-1","choices":[{"message":{"content":"{}"}}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":1},"extra":NaN}',
            b'{"id":"first","id":"second","choices":[{"message":{"content":"{}"}}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":1}}',
        )
        provider = OpenAICompatibleModelProvider(("approved.example",), max_response_bytes=1024)
        for body in bodies:
            with (
                self.subTest(body=body),
                mock.patch.object(provider._opener, "open", return_value=self.Response(body)),
                self.assertRaises(ModelProviderError),
            ):
                provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)

    def test_malformed_success_response_opens_the_breaker(self):
        breaker = CircuitBreaker("model", failure_threshold=1, reset_seconds=999)
        provider = OpenAICompatibleModelProvider(
            ("approved.example",), max_response_bytes=1024, breaker=breaker
        )

        with mock.patch.object(provider._opener, "open", return_value=self.Response(b"not-json")):
            with self.assertRaises(ModelProviderError):
                provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)

        with (
            mock.patch.object(provider._opener, "open") as opened,
            self.assertRaises(CircuitOpenError),
        ):
            provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)
        opened.assert_not_called()

    def test_rejects_non_allowlisted_host_before_network(self):
        provider = OpenAICompatibleModelProvider(("approved.example",))
        route = ModelRoute("test", "model", "https://blocked.example/v1", "key")
        with mock.patch.object(provider._opener, "open") as opened, self.assertRaises(ValueError):
            provider.complete(route, (ModelMessage("user", "hi"),), 20, True)
        opened.assert_not_called()

    def test_http_error_redacts_credentials(self):
        provider = OpenAICompatibleModelProvider(("approved.example",))
        error = urllib.error.HTTPError(
            self.route.base_url, 500, "error", {}, BytesIO(b"echo key-value")
        )
        with mock.patch.object(provider._opener, "open", side_effect=error):
            with self.assertRaises(ModelProviderError) as raised:
                provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)
        self.assertNotIn("key-value", str(raised.exception))
        self.assertTrue(error.fp.closed)

    def test_rejects_redirects(self):
        request = urllib.request.Request(
            "https://approved.example/v1/chat/completions",
            headers={"Authorization": "Bearer key-value"},
        )
        with self.assertRaisesRegex(ValueError, "redirects are not allowed"):
            _NoRedirectHandler().redirect_request(
                request, None, 307, "Temporary Redirect", {}, "https://evil.example/steal"
            )
