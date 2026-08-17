import ast
import os
import tempfile
import unittest
from unittest import mock

from evoagent.bootstrap import build_application_runtime
from evoagent.capabilities import FIX_RULE, GITHUB_CLIENT, MODEL_GATEWAY, STORE
from evoagent.config import Settings
from evoagent.fix_rules import RuleMutation
from evoagent.plugins import (
    CapabilityKey,
    PluginActivationError,
    PluginConfigurationError,
    PluginContext,
    PluginManifest,
    PluginProfile,
    PluginRuntime,
    PluginShutdownError,
    ProviderPlugin,
    RuntimeState,
    discover_plugins,
)
from evoagent.service import ReviewService


class _CallbackPlugin:
    def __init__(self, manifest, callback):
        self.manifest = manifest
        self.callback = callback

    def start(self, context):
        return self.callback(context)


def _provider(plugin_id, key, value, *, requires=(), priority=0, close=None):
    return ProviderPlugin(
        PluginManifest(
            plugin_id=plugin_id,
            version="1.0.0",
            provides=(key.name,),
            requires=tuple(item.name for item in requires),
            priority=priority,
        ),
        key,
        lambda _context: value,
        close=close,
    )


def _settings(path, **overrides):
    values = dict(
        host="127.0.0.1",
        port=8080,
        db_path=path,
        max_diff_bytes=10000,
        max_steps=8,
        timeout_seconds=10,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        github_webhook_secret="",
        github_token="",
        auto_post_review=False,
    )
    values.update(overrides)
    return Settings(**values)


class PluginRuntimeTests(unittest.TestCase):
    def test_manifest_rejects_incompatible_api_and_invalid_version(self):
        with self.assertRaisesRegex(ValueError, "unsupported API"):
            PluginManifest("test.plugin", "1.0.0", (), api_version="evoagent.plugin/v2")
        with self.assertRaisesRegex(ValueError, "semantic version"):
            PluginManifest("test.plugin", "latest", ())

    def test_dependencies_activate_before_consumers(self):
        source = CapabilityKey[str]("test.source")
        result = CapabilityKey[str]("test.result")
        order = []
        source_plugin = _CallbackPlugin(
            PluginManifest("test.source", "1.0.0", (source.name,)),
            lambda context: (order.append("source"), context.provide(source, "ready"))[1],
        )

        def start_consumer(context):
            order.append("consumer")
            context.provide(result, context.require(source) + "-consumed")

        consumer = _CallbackPlugin(
            PluginManifest(
                "test.consumer",
                "1.0.0",
                (result.name,),
                requires=(source.name,),
            ),
            start_consumer,
        )
        runtime = PluginRuntime([consumer, source_plugin]).start()
        self.addCleanup(runtime.stop)

        self.assertEqual(["source", "consumer"], order)
        self.assertEqual("ready-consumed", runtime.require(result))

    def test_activation_failure_rolls_back_every_effect(self):
        source = CapabilityKey[str]("test.source")
        result = CapabilityKey[str]("test.result")
        cleanup = []

        def start_source(context):
            context.provide(source, "ready")
            context.defer(lambda: cleanup.append("source-closed"))

        source_plugin = _CallbackPlugin(
            PluginManifest("test.source", "1.0.0", (source.name,)), start_source
        )
        broken = _CallbackPlugin(
            PluginManifest(
                "test.broken",
                "1.0.0",
                (result.name,),
                requires=(source.name,),
            ),
            lambda _context: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        runtime = PluginRuntime([broken, source_plugin])

        with self.assertRaisesRegex(PluginActivationError, "boom"):
            runtime.start()

        self.assertEqual(RuntimeState.FAILED, runtime.state)
        self.assertFalse(runtime.capabilities.has(source.name))
        self.assertEqual(["source-closed"], cleanup)

    def test_missing_dependency_and_cycles_fail_before_activation(self):
        a = CapabilityKey[str]("test.a")
        b = CapabilityKey[str]("test.b")
        missing = _CallbackPlugin(
            PluginManifest("test.missing", "1.0.0", (a.name,), requires=(b.name,)),
            lambda context: context.provide(a, "a"),
        )
        with self.assertRaisesRegex(PluginConfigurationError, "test.b"):
            PluginRuntime([missing]).start()

        first = _CallbackPlugin(
            PluginManifest("test.first", "1.0.0", (a.name,), requires=(b.name,)),
            lambda context: context.provide(a, "a"),
        )
        second = _CallbackPlugin(
            PluginManifest("test.second", "1.0.0", (b.name,), requires=(a.name,)),
            lambda context: context.provide(b, "b"),
        )
        with self.assertRaisesRegex(PluginConfigurationError, "cycle"):
            PluginRuntime([first, second]).start()

    def test_plugin_cannot_register_or_consume_undeclared_capability(self):
        declared = CapabilityKey[str]("test.declared")
        hidden = CapabilityKey[str]("test.hidden")
        plugin = _CallbackPlugin(
            PluginManifest("test.invalid", "1.0.0", (declared.name,)),
            lambda context: context.provide(hidden, "secret"),
        )
        runtime = PluginRuntime([plugin])
        with self.assertRaisesRegex(PluginConfigurationError, "undeclared"):
            runtime.start()
        self.assertFalse(runtime.capabilities.has(hidden.name))

    def test_plugin_must_register_every_declared_capability(self):
        key = CapabilityKey[str]("test.promised")
        plugin = _CallbackPlugin(
            PluginManifest("test.empty", "1.0.0", (key.name,)),
            lambda _context: None,
        )
        with self.assertRaisesRegex(PluginConfigurationError, "did not register"):
            PluginRuntime([plugin]).start()

    def test_shutdown_drains_all_cleanup_callbacks_before_reporting(self):
        calls = []

        def start(context):
            context.defer(lambda: calls.append("last"))

            def broken():
                calls.append("broken")
                raise RuntimeError("cleanup failed")

            context.defer(broken)
            context.defer(lambda: calls.append("first"))

        plugin = _CallbackPlugin(PluginManifest("test.cleanup", "1.0.0", ()), start)
        runtime = PluginRuntime([plugin]).start()
        with self.assertRaisesRegex(PluginShutdownError, "cleanup failed"):
            runtime.stop()
        self.assertEqual(["first", "broken", "last"], calls)
        self.assertEqual(RuntimeState.STOPPED, runtime.state)

    def test_priority_and_child_scope_shadow_parent(self):
        key = CapabilityKey[str]("test.value")
        parent = PluginRuntime(
            [
                _provider("test.low", key, "low", priority=1),
                _provider("test.high", key, "high", priority=10),
            ]
        ).start()
        self.addCleanup(parent.stop)
        self.assertEqual("high", parent.require(key))

        child = parent.create_scope("repository:org/repo", [_provider("test.repo", key, "repo")])
        child.start()
        self.assertEqual("repo", child.require(key))
        child.stop()
        self.assertEqual("high", parent.require(key))

    def test_parent_shutdown_owns_and_stops_child_scopes_first(self):
        calls = []

        def lifecycle(plugin_id, label):
            return _CallbackPlugin(
                PluginManifest(plugin_id, "1.0.0", ()),
                lambda context: context.defer(lambda: calls.append(label)),
            )

        parent = PluginRuntime([lifecycle("test.parent", "parent")]).start()
        child = parent.create_scope("tenant:acme", [lifecycle("test.child", "child")]).start()
        self.assertEqual(["tenant:acme"], parent.describe()["child_scopes"])

        parent.stop()

        self.assertEqual(["child", "parent"], calls)
        self.assertEqual(RuntimeState.STOPPED, child.state)

    def test_observer_errors_are_isolated_and_subscriptions_are_reversible(self):
        seen = []

        def start(context):
            def observe(event):
                seen.append(event.name)
                raise RuntimeError("observer unavailable")

            context.subscribe("review.completed", observe)

        observer = _CallbackPlugin(
            PluginManifest("test.observer", "1.0.0", ()),
            start,
        )
        runtime = PluginRuntime([observer]).start()
        failures = runtime.publish("review.completed", {"task_id": "1"})
        self.assertEqual(["review.completed"], seen)
        self.assertEqual("test.observer", failures[0].plugin_id)

        runtime.stop()
        self.assertEqual([], runtime.publish("review.completed", {"task_id": "2"}))
        self.assertEqual(["review.completed"], seen)

    def test_profile_parses_config_and_filters_plugins(self):
        handle, path = tempfile.mkstemp(suffix=".toml")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as output:
            output.write(
                '[profile]\nname = "strict"\n'
                'enabled = ["test.selected"]\n\n'
                '[plugins."test.selected".config]\nthreshold = 0.9\n'
            )
        profile = PluginProfile.from_toml(path)
        selected = CapabilityKey[str]("test.selected.value")
        ignored = CapabilityKey[str]("test.ignored.value")

        def build(context: PluginContext):
            return str(context.config["threshold"])

        runtime = PluginRuntime(
            [
                ProviderPlugin(
                    PluginManifest("test.selected", "1.0.0", (selected.name,)),
                    selected,
                    build,
                ),
                _provider("test.ignored", ignored, "ignored"),
            ],
            profile,
        ).start()
        self.addCleanup(runtime.stop)
        self.assertEqual("0.9", runtime.require(selected))
        self.assertFalse(runtime.capabilities.has(ignored.name))

    def test_profile_rejects_unknown_plugin_references(self):
        profile = PluginProfile(disabled=frozenset({"test.typo"}))
        with self.assertRaisesRegex(PluginConfigurationError, "test.typo"):
            PluginRuntime([], profile)

    def test_entry_point_discovery_loads_only_explicit_allowlist(self):
        key = CapabilityKey[str]("test.discovered.value")
        plugin = _provider("test.discovered", key, "loaded")

        class Entry:
            name = "test.discovered"

            @staticmethod
            def load():
                return lambda: plugin

        class Entries(list):
            def select(self, **kwargs):
                self.group = kwargs["group"]
                return self

        entries = Entries([Entry()])
        with mock.patch("evoagent.plugins.importlib.metadata.entry_points", return_value=entries):
            discovered = discover_plugins(["test.discovered"])
        self.assertEqual([plugin], discovered)
        self.assertEqual("evoagent.plugins", entries.group)

    def test_entry_point_discovery_fails_when_allowlisted_plugin_is_missing(self):
        class Entries(list):
            def select(self, **_kwargs):
                return self

        with (
            mock.patch("evoagent.plugins.importlib.metadata.entry_points", return_value=Entries()),
            self.assertRaisesRegex(PluginConfigurationError, "not installed"),
        ):
            discover_plugins(["test.missing"])


class ApplicationCompositionTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)

    def test_default_application_graph_exposes_runtime_inventory(self):
        runtime = build_application_runtime(_settings(self.path))
        self.addCleanup(runtime.stop)

        status = runtime.describe()
        self.assertEqual("running", status["state"])
        self.assertGreaterEqual(len(status["plugins"]), 10)
        self.assertIsNotNone(runtime.require(STORE))

    def test_service_can_replace_a_builtin_provider_by_stable_plugin_id(self):
        fake_github = object()
        replacement = ProviderPlugin(
            PluginManifest(
                "evoagent.codehost.github",
                "2.0.0",
                (GITHUB_CLIENT.name,),
            ),
            GITHUB_CLIENT,
            lambda _context: fake_github,
        )
        service = ReviewService(_settings(self.path), plugins=[replacement])
        self.addCleanup(service.close)

        self.assertIs(fake_github, service.github)
        self.assertEqual("running", service.plugin_status()["state"])

    def test_service_can_replace_the_model_gateway_provider(self):
        class FakeGateway:
            configured = False

            @staticmethod
            def route_info():
                return {}

            @staticmethod
            def route_catalog(_tenant_id, _repository):
                return ()

            @staticmethod
            def complete(_request):
                raise AssertionError("disabled fake gateway must not be called")

        gateway = FakeGateway()
        replacement = ProviderPlugin(
            PluginManifest(
                "evoagent.model-gateway",
                "2.0.0",
                (MODEL_GATEWAY.name,),
            ),
            MODEL_GATEWAY,
            lambda _context: gateway,
        )
        service = ReviewService(_settings(self.path), plugins=[replacement])
        self.addCleanup(service.close)

        self.assertIs(gateway, service.model_gateway)
        self.assertEqual({}, service.llm_config)

    def test_route_file_composes_multiple_routes_with_independent_breakers(self):
        handle, route_path = tempfile.mkstemp(suffix=".toml")
        os.close(handle)
        self.addCleanup(os.unlink, route_path)
        with open(route_path, "w", encoding="utf-8") as output:
            output.write(
                'version = 1\n[[routes]]\nid = "primary"\npriority = 10\n'
                'provider = "provider-a"\nmodel = "model-a"\n'
                'base_url = "https://a.example/v1"\napi_key_env = "ROUTE_A_KEY"\n'
                '[[routes]]\nid = "fallback"\npriority = 20\n'
                'provider = "provider-b"\nmodel = "model-b"\n'
                'base_url = "https://b.example/v1"\napi_key_env = "ROUTE_B_KEY"\n'
            )
        configured = _settings(
            self.path,
            llm_routes_file=route_path,
            llm_allowed_hosts=("a.example", "b.example"),
        )
        with mock.patch.dict(
            os.environ,
            {"ROUTE_A_KEY": "secret-a", "ROUTE_B_KEY": "secret-b"},
        ):
            service = ReviewService(configured)
        self.addCleanup(service.close)

        self.assertEqual(2, service.llm_config["route_count"])
        providers = list(service.model_gateway.providers.values())
        self.assertIsNot(providers[0].breaker, providers[1].breaker)
        self.assertNotIn("secret-a", repr(service.llm_config))

    def test_service_publishes_sanitized_lifecycle_events(self):
        events = []

        def start(context):
            context.subscribe("*", lambda event: events.append((event.name, dict(event.payload))))

        observer = _CallbackPlugin(
            PluginManifest("test.audit-events", "1.0.0", ()),
            start,
        )
        service = ReviewService(_settings(self.path), plugins=[observer])
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        result = service.create_review("org/repo", diff, 1)
        service.close()

        names = [name for name, _payload in events]
        self.assertEqual("SUCCESS", result["state"])
        self.assertIn("service.started", names)
        self.assertIn("review.started", names)
        self.assertIn("review.completed", names)
        self.assertIn("service.stopping", names)
        self.assertNotIn("diff", events[names.index("review.started")][1])

    def test_external_fix_rule_is_composed_without_changing_safe_fixer(self):
        class ReplaceZeroRule:
            rule_id = "TEST-REPLACE-ZERO"

            def apply_python(self, tree, target_lines):
                changed = False
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Constant)
                        and node.lineno in target_lines
                        and node.value == 0
                    ):
                        node.value = 1
                        changed = True
                return RuleMutation(changed)

            def propose_line(self, line):
                return line

        plugin = ProviderPlugin(
            PluginManifest("test.fix-rule.replace-zero", "1.0.0", (FIX_RULE.name,)),
            FIX_RULE,
            lambda _context: ReplaceZeroRule(),
        )
        service = ReviewService(_settings(self.path), plugins=[plugin])
        self.addCleanup(service.close)

        result = service.fixer.apply(
            "value = 0\n",
            [{"path": "app.py", "line": 1, "rule_id": "TEST-REPLACE-ZERO"}],
            "app.py",
        )
        self.assertIn("value = 1", result["content"])
        self.assertIn("TEST-REPLACE-ZERO", service.fixer.rule_ids)

    def test_profile_can_disable_one_builtin_fix_rule(self):
        profile = PluginProfile(disabled=frozenset({"evoagent.fix-rule.debug-print"}))
        service = ReviewService(_settings(self.path), plugin_profile=profile)
        self.addCleanup(service.close)

        self.assertNotIn("REL-DEBUG-PRINT", service.fixer.rule_ids)
        self.assertIn("SEC-YAML-LOAD", service.fixer.rule_ids)

    def test_service_loads_profile_from_settings_path(self):
        handle, profile_path = tempfile.mkstemp(suffix=".toml")
        os.close(handle)
        self.addCleanup(os.unlink, profile_path)
        with open(profile_path, "w", encoding="utf-8") as output:
            output.write(
                '[profile]\nname = "no-cookie-fix"\n'
                'disabled = ["evoagent.fix-rule.secure-cookie"]\n'
            )
        service = ReviewService(_settings(self.path, plugin_profile_path=profile_path))
        self.addCleanup(service.close)

        self.assertEqual("no-cookie-fix", service.plugin_status()["profile"])
        self.assertNotIn("SEC-INSECURE-COOKIE", service.fixer.rule_ids)

    def test_trusted_discovery_requires_an_allowlist(self):
        with self.assertRaisesRegex(ValueError, "PLUGIN_ALLOWLIST"):
            ReviewService(_settings(self.path, plugin_discovery=True))


if __name__ == "__main__":
    unittest.main()
