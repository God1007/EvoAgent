import json
import os
import tempfile
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

from evoagent.errors import AccessDeniedError, ClientInputError
from evoagent.model_gateway import (
    ModelGateway,
    ModelGatewayOptions,
    ModelMessage,
    ModelOutputError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelRoute,
    OpenAICompatibleModelProvider,
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
        with self.assertRaises(ModelOutputError):
            self.gateway(FakeProvider("not-json")).complete(self.request())
        with self.assertRaises(ModelOutputError):
            self.gateway(
                FakeProvider(json.dumps({"value": "x" * 100})), max_response_bytes=20
            ).complete(self.request())

    def test_enforces_input_and_output_limits(self):
        with self.assertRaises(ClientInputError):
            self.gateway(max_input_tokens=1).complete(self.request("more than four bytes"))
        with self.assertRaises(ClientInputError):
            self.gateway(max_output_tokens=10).complete(self.request(max_output_tokens=11))
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

    def test_unconfigured_gateway_and_route_metadata(self):
        gateway = ModelGateway(None, None)
        self.assertFalse(gateway.configured)
        self.assertEqual("local", gateway.route_info()["provider"])
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            gateway.complete(self.request())
        self.assertEqual("default", self.gateway().route_info()["route_id"])

    def test_provider_exception_cannot_echo_route_secret(self):
        class FailingProvider:
            def complete(self, *_args):
                raise RuntimeError("upstream echoed secret-key")

        with self.assertRaises(ModelProviderError) as raised:
            self.gateway(FailingProvider()).complete(self.request())
        self.assertNotIn("secret-key", str(raised.exception))

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


class ProviderTests(unittest.TestCase):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def setUp(self):
        self.route = ModelRoute("test", "model", "https://approved.example/v1", "key-value")

    def test_uses_allowlisted_https_endpoint_and_bounds_response(self):
        body = json.dumps(
            {
                "id": "request-1",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        ).encode()
        provider = OpenAICompatibleModelProvider(("approved.example",), max_response_bytes=1024)
        with mock.patch("urllib.request.urlopen", return_value=self.Response(body)) as opened:
            response = provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)
        self.assertEqual("request-1", response.request_id)
        self.assertEqual(
            "https://approved.example/v1/chat/completions", opened.call_args.args[0].full_url
        )

    def test_rejects_non_allowlisted_host_before_network(self):
        provider = OpenAICompatibleModelProvider(("approved.example",))
        route = ModelRoute("test", "model", "https://blocked.example/v1", "key")
        with mock.patch("urllib.request.urlopen") as opened, self.assertRaises(ValueError):
            provider.complete(route, (ModelMessage("user", "hi"),), 20, True)
        opened.assert_not_called()

    def test_http_error_redacts_credentials(self):
        provider = OpenAICompatibleModelProvider(("approved.example",))
        error = urllib.error.HTTPError(
            self.route.base_url, 500, "error", {}, BytesIO(b"echo key-value")
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ModelProviderError) as raised:
                provider.complete(self.route, (ModelMessage("user", "hi"),), 20, True)
        self.assertNotIn("key-value", str(raised.exception))
