"""User-created definitions must execute, survive publication, and remain tenant-bound."""

import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import mock
from urllib.parse import quote

from evoagent.api import _make_server
from evoagent.auth import AuthManager
from evoagent.console_view import console_response
from evoagent.errors import (
    AccessDeniedError,
    ClientInputError,
    ResourceNotFoundError,
    StateConflictError,
)
from evoagent.model_gateway import (
    ModelGateway,
    ModelGovernanceContext,
    ModelResponse,
    ModelRoute,
)
from evoagent.models import Finding, Severity
from evoagent.service import ReviewService
from evoagent.studio import (
    DIFF,
    FINDINGS,
    _rules,
    build_agent,
    compile_workflow,
    draft_definition,
    validate_agent,
)
from evoagent.workflow import HandoffError, Step, Workflow
from tests.db_support import postgres_url, reset_postgres
from tests.test_http_server import _settings

DIFF_TEXT = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+eval(value)\n+print(value)\n"


def rules(name="安全审查", selected=None):
    return {
        "name": name,
        "kind": "rules",
        "inputs": {"diff": DIFF},
        "outputs": {"findings": FINDINGS},
        "config": {"rules": selected or ["SEC-EVAL"], "checks": []},
    }


def merge():
    return {
        "name": "报告汇总",
        "kind": "merge",
        "inputs": {"security": FINDINGS, "business": FINDINGS},
        "outputs": {"findings": FINDINGS},
        "config": {},
    }


def model_agent():
    return {
        "name": "业务提示词",
        "kind": "llm",
        "inputs": {"diff": DIFF},
        "outputs": {"summary": "text@1"},
        "config": {
            "prompt": "Describe the change.",
            "model": "fixture-model",
            "tools": ["diff-summary"],
            "max_output_tokens": 100,
        },
    }


class AgentDefinitionTests(unittest.TestCase):
    def test_merge_is_conservative_and_independent_of_port_or_finding_order(self):
        agent = build_agent("merge", merge(), ModelGateway(None, None), lambda _: None)
        workflow = Workflow(
            "merge",
            agent.inputs,
            [Step("report", agent, {port: "$input." + port for port in agent.inputs})],
            {"verified": "report.findings"},
        )
        high = replace(
            Finding.from_dict(_rules(rules()["config"], DIFF_TEXT)[0]),
            severity=Severity.HIGH,
            confidence=0.7,
        )
        low = replace(high, severity=Severity.LOW, confidence=0.99, fix="General advice")
        stronger = replace(high, confidence=0.9, fix="Specific remediation")
        alternate = replace(stronger, fix="Another remediation")
        critical = replace(low, severity=Severity.CRITICAL, confidence=0.6)
        other_line = replace(low, line=2)
        other_claim = replace(low, title="Another issue")
        for left, right, expected in (
            (high, low, high),
            (critical, stronger, critical),
            (high, stronger, stronger),
            (stronger, alternate, None),
        ):
            results = []
            for candidates in ((left, right), (right, left)):
                for reverse_items in (False, True):
                    inputs = {
                        "security": [candidates[0].to_dict(), other_line.to_dict()],
                        "business": [candidates[1].to_dict(), other_claim.to_dict()],
                    }
                    # Derived fingerprints are optional and do not affect deduplication.
                    duplicate = candidates[1].to_dict()
                    duplicate.pop("fingerprint")
                    inputs["business"].append(duplicate)
                    if reverse_items:
                        inputs = {port: list(reversed(items)) for port, items in inputs.items()}
                    original = copy.deepcopy(inputs)
                    result = workflow.run(inputs)["verified"]
                    self.assertEqual(original, inputs)
                    self.assertEqual(3, len(result))
                    self.assertIn(other_line.to_dict(), result)
                    self.assertIn(other_claim.to_dict(), result)
                    if expected is not None:
                        self.assertEqual(expected.to_dict(), result[0])
                    else:
                        self.assertIn(result[0], [stronger.to_dict(), alternate.to_dict()])
                    results.append(result)
            self.assertTrue(all(item == results[0] for item in results))
        first = [replace(high, line=index + 1).to_dict() for index in range(100)]
        result = workflow.run({"security": first, "business": list(reversed(first))})
        self.assertEqual(first, result["verified"])
        with self.assertRaises(HandoffError):
            workflow.run({"security": first, "business": [replace(high, line=101).to_dict()]})

    def test_drafts_normalize_only_missing_fields_and_reject_unsafe_shapes(self):
        partial = {"name": "Draft", "config": {"checks": [{"contains": "TODO"}]}}
        before = copy.deepcopy(partial)
        normalized = draft_definition("agents", partial)
        self.assertEqual("", normalized["config"]["checks"][0]["rule_id"])
        self.assertEqual("TODO", normalized["config"]["checks"][0]["contains"])
        self.assertEqual(before, partial)
        with self.assertRaises(ClientInputError):
            validate_agent(normalized)
        model = draft_definition(
            "agents", {"name": "Draft", "kind": "llm", "inputs": {}, "outputs": {}}
        )
        self.assertEqual("", model["config"]["model"])
        self.assertEqual("", model["config"]["prompt"])
        self.assertEqual({}, model["inputs"])
        with self.assertRaises(ClientInputError):
            validate_agent(model)
        step = {"id": "missing", "agent": "unknown", "version": 0}
        normalized = draft_definition("workflows", {"name": "Draft", "steps": [step]})
        self.assertEqual({}, normalized["steps"][0]["sources"])
        invalid = [
            ("agents", {"kind": []}),
            ("agents", {"config": None}),
            ("agents", {"inputs": []}),
            ("agents", {"config": {"checks": [None]}}),
            ("agents", {"config": {"checks": [{}] * 33}}),
            ("agents", {"config": {"module": "os:system"}}),
            ("agents", {"kind": "llm", "config": {"prompt": {"private": "object"}}}),
            ("agents", {"kind": "llm", "config": {"max_output_tokens": True}}),
            ("agents", {"config": {"checks": [{"title": "x" * 201}]}}),
            ("workflows", {"steps": None}),
            ("workflows", {"steps": [None]}),
            ("workflows", {"steps": [step, step]}),
            ("workflows", {"steps": [step] * 65}),
            ("workflows", {"steps": [{**step, "sources": {"diff": []}}]}),
            ("workflows", {"steps": [{**step, "sources": {"diff": "a.b.c"}}]}),
            ("workflows", {"steps": [{**step, "version": True}]}),
            ("workflows", {"steps": [{**step, "version": 1}]}),
            ("workflows", {"outputs": []}),
            ("workflows", {"outputs": {"hidden": "a.b"}}),
            ("workflows", {"unknown_field": "keep, do not discard"}),
        ]
        for kind, fields in invalid:
            with self.subTest(kind=kind, fields=fields), self.assertRaises(ClientInputError):
                draft_definition(kind, {"name": "Draft", **fields})

    def test_model_receives_only_wired_inputs_and_selected_tools_under_gateway_policy(self):
        provider = mock.Mock()
        provider.complete.return_value = ModelResponse(
            '{"summary":"changed"}', "fixture", "fixture-model", 10, 3, "request"
        )
        gateway = ModelGateway(
            ModelRoute("fixture", "fixture-model", "http://127.0.0.1:9999/v1", "fixture-key"),
            provider,
        )

        def context(_task):
            return ModelGovernanceContext("tenant", "demo/repo", ("fixture",), ("fixture-model",))

        agent = build_agent("custom", model_agent(), gateway, context)
        workflow = Workflow(
            "model",
            agent.inputs,
            [Step("review", agent, {"diff": "$input.diff"})],
            {"summary": "review.summary"},
        )
        self.assertEqual({"summary": "changed"}, workflow.run({"diff": DIFF_TEXT}))
        sent = provider.complete.call_args.args[1]
        self.assertIn("Describe the change.", sent[0].content)
        self.assertEqual({"diff-summary"}, json.loads(sent[1].content)["tool_results"].keys())
        provider.complete.return_value = ModelResponse(
            '{"summary":false}', "fixture", "fixture-model", 10, 3, "request"
        )
        with self.assertRaises(HandoffError):
            workflow.run({"diff": DIFF_TEXT})
        denied = build_agent(
            "denied",
            model_agent(),
            gateway,
            lambda _task: ModelGovernanceContext("tenant", "demo/repo", ("other",)),
        )
        with self.assertRaises(AccessDeniedError):
            Workflow(
                "denied",
                denied.inputs,
                [Step("r", denied, {"diff": "$input.diff"})],
                {"summary": "r.summary"},
            ).run({"diff": DIFF_TEXT})
        self.assertEqual(2, provider.complete.call_count)

        definition = model_agent()
        definition["outputs"] = {"findings": FINDINGS}
        agent = build_agent("grounded", definition, gateway, context)
        workflow = Workflow(
            "grounded",
            agent.inputs,
            [Step("r", agent, {"diff": "$input.diff"})],
            {"findings": "r.findings"},
        )
        finding = _rules(rules()["config"], DIFF_TEXT)[0]
        provider.complete.return_value = ModelResponse(
            json.dumps({"findings": [finding]}), "fixture", "fixture-model", 10, 3, "request"
        )
        self.assertEqual({"findings": [finding]}, workflow.run({"diff": DIFF_TEXT}))
        for content in ("not json", json.dumps({"findings": [{**finding, "line": 999}]})):
            provider.complete.return_value = ModelResponse(
                content, "fixture", "fixture-model", 10, 3, "request"
            )
            with self.subTest(content=content), self.assertRaises(HandoffError):
                workflow.run({"diff": DIFF_TEXT})

    def test_definitions_reject_executable_extensions_and_unbounded_or_unknown_fields(self):
        invalid = []
        value = model_agent()
        value["config"]["tools"] = ["shell"]
        invalid.append(value)
        value = rules()
        value["config"]["module"] = "os:system"
        invalid.append(value)
        value = rules()
        value["outputs"]["findings"] = "untyped-json"
        invalid.append(value)
        value = rules()
        value["name"] = "\x00"
        invalid.append(value)
        value = rules()
        value["config"]["rules"] = ["SEC-EVAL", "SEC-EVAL"]
        invalid.append(value)
        value = model_agent()
        value["config"]["max_output_tokens"] = True
        invalid.append(value)
        value = model_agent()
        value["inputs"] = {"summary": "text@1"}
        value["outputs"] = {"findings": FINDINGS}
        value["config"]["tools"] = []
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ClientInputError):
                validate_agent(value)


class StudioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.url = postgres_url(self)
        reset_postgres(self.url)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = _settings(
            self.url,
            port=8080,
            skills_dir=self.directory.name,
            auth_secret="studio-test-secret-" * 3,
        )
        self.service = ReviewService(self.settings)
        self.addCleanup(self.service.close)
        self.studio, self.store = self.service.studio, self.service.store

    def test_library_pagination_preserves_boundaries_and_tenant_scope(self):
        timestamp = "2026-01-01T00:00:00.123456+00:00"
        ids = [f"{index:032x}" for index in range(102)]
        rows = [("default", "agents", key, json.dumps(rules(key))) for key in ids]
        rows += [
            ("other", "agents", f"{999:032x}", json.dumps(rules("other-tenant-private"))),
            ("default", "workflows", f"{998:032x}", json.dumps({"name": "other-kind-private"})),
        ]
        with self.store._connect() as conn, conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO studio_documents(tenant_id,kind,id,revision,definition_json,updated_at) "
                "VALUES (%s,%s,%s,1,%s::jsonb,%s)",
                [(*row, timestamp) for row in rows],
            )
        first = self.studio.list_documents("default", "agents")
        self.assertEqual(ids[:100], [item["id"] for item in first["documents"]])
        self.assertTrue(first["next_cursor"])
        self.assertNotIn("config", json.dumps(first))
        self.assertNotIn("private", json.dumps(first))

        # A cursor retains its position even if the anchor is removed and newer
        # edits/creations arrive; it does not re-look up the anchor or use OFFSET.
        with self.store._connect() as conn:
            conn.execute(
                "DELETE FROM studio_documents WHERE tenant_id='default' AND kind='agents' AND id=%s",
                (ids[99],),
            )
            conn.execute(
                "UPDATE studio_documents SET updated_at='2026-01-02T00:00:00Z' "
                "WHERE tenant_id='default' AND kind='agents' AND id=%s",
                (ids[0],),
            )
            conn.execute(
                "INSERT INTO studio_documents(tenant_id,kind,id,revision,definition_json,updated_at) "
                "VALUES ('default','agents',%s,1,%s::jsonb,'2026-01-03T00:00:00Z')",
                (f"{200:032x}", json.dumps(rules("new"))),
            )
        second = self.studio.list_documents("default", "agents", cursor=first["next_cursor"])
        self.assertEqual(ids[100:], [item["id"] for item in second["documents"]])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(
            [f"{999:032x}"],
            [
                item["id"]
                for item in self.studio.list_documents(
                    "other", "agents", cursor=first["next_cursor"]
                )["documents"]
            ],
        )
        self.assertEqual(
            [f"{998:032x}"],
            [
                item["id"]
                for item in self.studio.list_documents(
                    "default", "workflows", cursor=first["next_cursor"]
                )["documents"]
            ],
        )
        refreshed = self.studio.list_documents("default", "agents", limit=1)
        self.assertEqual(f"{200:032x}", refreshed["documents"][0]["id"])
        self.assertEqual([], self.studio.list_documents("empty", "agents")["documents"])
        with mock.patch.object(self.store, "list_studio_documents") as query:
            for value in (0, 101, True, 1.5, "1"):
                with self.subTest(limit=value), self.assertRaises(ClientInputError):
                    self.studio.list_documents("default", "agents", limit=value)
            for value in (
                None,
                [],
                "bad",
                "x" * 129,
                "2026-01-01|" + ids[0],
                timestamp + "|bad",
                timestamp + "|" + ids[0] + "|extra",
            ):
                with self.subTest(cursor=value), self.assertRaises(ClientInputError):
                    self.studio.list_documents("default", "agents", cursor=value)
            query.assert_not_called()

    def publish(self, kind, definition, tenant="default", previous=None):
        payload = {"revision": previous["revision"] if previous else 0, "definition": definition}
        if previous:
            payload["id"] = previous["id"]
        saved = self.studio.save(tenant, kind, payload, "test-author")
        version = self.studio.publish(tenant, kind, saved["id"], saved["revision"], "test-author")
        return saved, version

    def example(self):
        security, _ = self.publish("agents", rules())
        business = rules("业务审查")
        business["config"] = {
            "rules": [],
            "checks": [
                {
                    "contains": "print(",
                    "rule_id": "BIZ-LOG",
                    "severity": "medium",
                    "title": "使用结构化日志",
                    "explanation": "业务日志需要可检索的字段。",
                    "fix": "使用结构化日志。",
                    "test": "验证日志字段。",
                }
            ],
        }
        business, _ = self.publish("agents", business)
        aggregator, _ = self.publish("agents", merge())
        definition = {
            "name": "我的审查流程",
            "steps": [
                {
                    "id": "security",
                    "agent": security["id"],
                    "version": 1,
                    "sources": {"diff": "$input.diff"},
                },
                {
                    "id": "business",
                    "agent": business["id"],
                    "version": 1,
                    "sources": {"diff": "$input.diff"},
                },
                {
                    "id": "report",
                    "agent": aggregator["id"],
                    "version": 1,
                    "sources": {"security": "security.findings", "business": "business.findings"},
                },
            ],
            "outputs": {"verified": "report.findings"},
        }
        flow, published = self.publish("workflows", definition)
        return flow, published, security

    def test_published_merge_preserves_risk_and_original_branch_claims_when_rewired(self):
        security, _ = self.publish("agents", rules())
        high = _rules(rules()["config"], DIFF_TEXT)[0]
        business = rules("一般建议")
        business["config"] = {
            "rules": [],
            "checks": [
                {
                    **{key: high[key] for key in ("rule_id", "title", "explanation", "test")},
                    "contains": "eval(",
                    "severity": "low",
                    "fix": "General advice, not a replacement for the security fix",
                }
            ],
        }
        low = _rules(business["config"], DIFF_TEXT)[0]
        business, _ = self.publish("agents", business)
        aggregator, _ = self.publish("agents", merge())
        definition = {
            "name": "Conservative merge",
            "steps": [
                {"id": key, "agent": agent["id"], "version": 1, "sources": {"diff": "$input.diff"}}
                for key, agent in (("security", security), ("business", business))
            ]
            + [
                {
                    "id": "report",
                    "agent": aggregator["id"],
                    "version": 1,
                    "sources": {"security": "security.findings", "business": "business.findings"},
                }
            ],
            "outputs": {"verified": "report.findings"},
        }
        flow, recorded = None, []
        for version, expected_inputs in (
            (1, {"security": [high], "business": [low]}),
            (2, {"security": [low], "business": [high]}),
        ):
            if version == 2:
                definition = copy.deepcopy(definition)
                definition["steps"][-1]["sources"] = {
                    "security": "business.findings",
                    "business": "security.findings",
                }
            flow, _ = self.publish("workflows", definition, previous=flow)
            result = self.service.create_review(
                "demo/repo", DIFF_TEXT, workflow_selection={"id": flow["id"], "version": version}
            )
            self.assertEqual("SUCCESS", result["state"])
            task_id = result["task_id"]
            task = self.store.get(task_id)
            self.assertEqual("high", task["report"]["risk"])
            self.assertEqual([high], task["report"]["findings"])
            artifact = self.studio.artifact("default", task_id, "report")
            self.assertEqual(expected_inputs, artifact["inputs"])
            self.assertEqual([high], artifact["outputs"]["findings"])
            view = console_response("GET", "/v1/tasks/" + task_id, task)
            self.assertEqual(version, view["workflow"]["version"])
            self.assertEqual("high", view["report"]["risk"])
            self.assertEqual(high["fix"], view["report"]["findings"][0]["fix"])
            self.assertNotIn("fingerprint", view["report"]["findings"][0])
            recorded.append((task, artifact))
        for task, artifact in recorded:
            self.assertEqual(task, self.store.get(task["id"]))
            self.assertEqual(artifact, self.studio.artifact("default", task["id"], "report"))

    def test_model_egress_redacts_wired_diff_and_tools_without_rewriting_task_artifacts(self):
        self.service.close()
        self.service = ReviewService(
            replace(
                self.settings,
                llm_provider="custom",
                llm_base_url="https://models.example/v1",
                llm_api_key="fixture-route-key",
                llm_model="fixture-model",
                llm_allowed_hosts=("models.example",),
            )
        )
        self.addCleanup(self.service.close)
        self.studio, self.store = self.service.studio, self.service.store
        definition = model_agent()
        definition["outputs"] = {"findings": FINDINGS}
        definition["config"]["tools"] = ["local-rules", "diff-summary"]
        agent, _ = self.publish("agents", definition)
        flow, _ = self.publish(
            "workflows",
            {
                "name": "Governed model trial",
                "steps": [
                    {
                        "id": "review",
                        "agent": agent["id"],
                        "version": 1,
                        "sources": {"diff": "$input.diff"},
                    }
                ],
                "outputs": {"verified": "review.findings"},
            },
        )
        diff = '--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+password = "studio-synthetic-secret"\n'
        with mock.patch.object(
            self.service.model_gateway.provider,
            "complete",
            return_value=ModelResponse(
                '{"findings":[]}', "custom", "fixture-model", 30, 5, "fixture"
            ),
        ) as provider:
            result = self.service.create_review(
                "demo/repo", diff, workflow_selection={"id": flow["id"], "version": 1}
            )
        self.assertEqual("SUCCESS", result["state"])
        provider.assert_called_once()
        sent = json.loads(provider.call_args.args[1][1].content)
        self.assertNotIn("studio-synthetic-secret", json.dumps(sent))
        self.assertIn("<redacted>", sent["inputs"]["diff"])
        evidence = sent["tool_results"]["local-rules"]
        self.assertEqual("SEC-HARDCODED-SECRET", evidence[0]["rule_id"])
        self.assertIn("<redacted>", evidence[0]["evidence"])
        self.assertEqual(["app.py"], sent["tool_results"]["diff-summary"]["files"])
        self.assertEqual(diff, self.store.get_task_payload(result["task_id"]))
        artifact = self.studio.artifact("default", result["task_id"], "review")
        self.assertEqual(diff, artifact["inputs"]["diff"])
        self.assertEqual({"findings": []}, artifact["outputs"])
        self.assertEqual(
            definition,
            self.store.get_studio_version("default", "agents", agent["id"], 1)["definition"],
        )

    def test_real_execution_publication_binding_and_old_task_snapshot(self):
        flow, _published, security = self.example()
        binding = self.store.bind_studio_workflow(
            "default", "demo/repo", flow["id"], "author", version=1, expected_revision=0
        )
        self.assertEqual((1, 1), (binding["version"], binding["revision"]))
        selection = {"id": flow["id"], "version": 1}
        task_id, _ = self.service.review_use_cases.create_task(
            "demo/repo", DIFF_TEXT, 1, "api", workflow_selection=selection
        )
        before = self.store.get(task_id)["input"]["studio_workflow"]
        self.publish("agents", rules("新版安全审查", ["REL-DEBUG-PRINT"]), previous=security)
        newer = copy.deepcopy(flow["definition"])
        newer["steps"][0]["version"] = 2
        latest, v2 = self.publish("workflows", newer, previous=flow)
        self.assertEqual(2, v2["version"])
        report = self.service._run_review(task_id, "demo/repo", 1, DIFF_TEXT, "default", 1)
        self.assertEqual({"SEC-EVAL", "BIZ-LOG"}, {item.rule_id for item in report.findings})
        self.assertEqual(before, self.store.get(task_id)["input"]["studio_workflow"])
        status = self.store.workflow_status(task_id, "default")
        self.assertEqual(["completed"] * 3, [step["status"] for step in status["steps"]])
        artifact = self.studio.artifact("default", task_id, "report")
        self.assertEqual({"security", "business"}, artifact["inputs"].keys())
        self.assertEqual(2, len(artifact["outputs"]["findings"]))
        self.assertEqual([], artifact["unavailable_inputs"])
        self.assertIsNone(self.studio.artifact("other", task_id, "report"))
        unchanged = self.service.create_review("demo/repo", DIFF_TEXT)
        self.assertEqual(
            {"SEC-EVAL", "BIZ-LOG"}, {f["rule_id"] for f in unchanged["report"]["findings"]}
        )
        self.assertEqual(binding, self.store.get_studio_binding("default", "demo/repo"))
        self.store.bind_studio_workflow(
            "default", "demo/repo", flow["id"], "author", version=2, expected_revision=1
        )
        new = self.service.create_review("demo/repo", DIFF_TEXT)
        self.assertEqual(
            {"REL-DEBUG-PRINT", "BIZ-LOG"}, {f["rule_id"] for f in new["report"]["findings"]}
        )
        deferred, payload = self.service.review_use_cases.prepare_deferred_task(
            "demo/repo", "github", "default", {}
        )
        self.assertTrue(deferred)
        self.assertEqual(2, payload["studio_workflow"]["version"])
        self.store.create_review_task(deferred, "demo/repo", 2, payload, "default", None, None, 0)
        self.store.bind_studio_workflow(
            "default", "demo/repo", flow["id"], "author", version=1, expected_revision=2
        )
        rollback = self.service.create_review("demo/repo", DIFF_TEXT)
        self.assertEqual(
            {"SEC-EVAL", "BIZ-LOG"}, {f["rule_id"] for f in rollback["report"]["findings"]}
        )
        self.assertEqual(2, self.store.get(deferred)["input"]["studio_workflow"]["version"])
        self.assertEqual(before, self.store.get(task_id)["input"]["studio_workflow"])
        self.store.bind_studio_workflow(
            "default", "demo/repo", None, "author", version=None, expected_revision=3
        )
        self.assertIsNone(self.studio.select("default", "demo/repo"))
        self.assertEqual(4, self.store.get_studio_binding("default", "demo/repo")["revision"])
        self.assertEqual(
            v2,
            self.studio.publish("default", "workflows", latest["id"], latest["revision"], "author"),
        )

    def test_binding_preflight_matches_repository_policy_without_running_agents(self):
        self.service.close()
        provider = mock.Mock()
        gateway = ModelGateway(
            ModelRoute("fixture", "fixture-model", "https://models.example/v1", "key", region="eu"),
            provider,
        )
        with mock.patch("evoagent.bootstrap._model_gateway", return_value=gateway):
            self.service = ReviewService(self.settings)
        self.addCleanup(self.service.close)
        self.studio, self.store = self.service.studio, self.service.store
        flow, _published, _agent = self.example()
        self.publish("workflows", {**flow["definition"], "name": "Candidate"}, previous=flow)
        original = self.store.bind_studio_workflow(
            "default", "demo/repo", flow["id"], "operator", version=1, expected_revision=0
        )
        server = _make_server(_settings(self.url), self.service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def bind(version, revision=1):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request(
                    "POST",
                    "/v1/studio/binding",
                    json.dumps(
                        {
                            "repository": "Demo/Repo",
                            "workflow_id": flow["id"] if version is not None else None,
                            "version": version,
                            "revision": revision,
                        }
                    ),
                    {"Content-Type": "application/json", "X-EvoAgent-View": "console"},
                )
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        def binding_audits():
            return [
                item
                for item in self.store.list_audit("default")
                if item["action"] == "studio.repository_bound"
            ]

        allowed = {
            "allowed_reviewers": [self.service.reviewer.name],
            "allowed_llm_providers": ["fixture"],
            "allowed_llm_models": ["fixture-model"],
            "llm_region": "eu",
        }
        before = binding_audits()
        for denied in (
            {"allowed_reviewers": ["other-reviewer"]},
            {"allowed_llm_providers": ["other-provider"]},
            {"allowed_llm_models": ["other-model"]},
            {"llm_region": "us"},
            {"enabled": False},
        ):
            with self.subTest(denied=denied):
                self.service.policies.save("default", "demo/repo", allowed | denied, "operator")
                with self.assertRaises(AccessDeniedError):
                    self.service.review_use_cases.authorize_review(
                        "default", "demo/repo", DIFF_TEXT
                    )
                self.assertEqual(403, bind(2)[0])
                self.assertEqual(original, self.store.get_studio_binding("default", "demo/repo"))
                self.assertEqual(before, binding_audits())

        # Tenant policy does not leak into another tenant's route check.
        self.service.policies.save("other", "demo/repo", {"enabled": False}, "operator")
        self.service.policies.save("default", "demo/repo", allowed, "operator")
        status, switched = bind(2)
        self.assertEqual(200, status)
        self.assertEqual((2, 2), (switched["binding"]["version"], switched["binding"]["revision"]))
        self.assertEqual(409, bind(1)[0])
        self.service.policies.save(
            "default", "demo/repo", allowed | {"allowed_llm_models": ["other-model"]}, "operator"
        )
        status, unbound = bind(None, 2)
        self.assertEqual(200, status, "an enabled repository can still restore the default")
        self.assertIsNone(unbound["binding"]["workflow_id"])
        self.assertEqual(3, unbound["binding"]["revision"])
        self.assertEqual(3, len(binding_audits()))
        self.assertEqual([], self.store.list_tasks())
        self.assertEqual(0, self.store.outbox_stats()["total"])
        provider.complete.assert_not_called()

    def test_binding_concurrency_tombstones_and_transactional_audit(self):
        flow, _, _ = self.example()
        self.publish("workflows", {**flow["definition"], "name": "新版"}, previous=flow)

        def bind(version, revision, tenant="default"):
            return self.store.bind_studio_workflow(
                tenant,
                "demo/repo",
                flow["id"] if version else None,
                "operator",
                version=version,
                expected_revision=revision,
            )

        self.assertIsNone(self.store.get_studio_binding("default", "demo/repo"))
        for revision in (0, 1):
            barrier = threading.Barrier(2)

            def compete(version, barrier=barrier, revision=revision):
                barrier.wait(timeout=5)
                try:
                    return bind(version, revision)
                except StateConflictError:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(compete, (1, 2)))
            winners = [result for result in results if result is not None]
            self.assertEqual(1, len(winners))
            current = self.store.get_studio_binding("default", "demo/repo")
            self.assertEqual(winners[0], current)
            self.assertEqual(revision + 1, current["revision"])
            self.assertEqual("新版" if current["version"] == 2 else "我的审查流程", current["name"])

        for tenant, version in (("default", 999), ("other", 1)):
            with self.assertRaises(ResourceNotFoundError):
                bind(version, 2 if tenant == "default" else 0, tenant)
        self.assertEqual(current, self.store.get_studio_binding("default", "demo/repo"))
        self.assertIsNone(self.store.get_studio_binding("other", "demo/repo"))
        with mock.patch.object(
            self.store, "_audit_studio", side_effect=RuntimeError("audit failed")
        ):
            with self.assertRaises(RuntimeError):
                bind(None, 2)
        self.assertEqual(current, self.store.get_studio_binding("default", "demo/repo"))

        unbound = bind(None, 2)
        self.assertEqual(3, unbound["revision"])
        self.assertIsNone(unbound["workflow_id"])
        self.assertIsNone(unbound["name"])
        for stale_revision in (0, 1, 2):
            with self.assertRaises(StateConflictError):
                bind(1, stale_revision)
        rebound = bind(1, 3)
        with self.assertRaises(StateConflictError):
            bind(None, 3)
        self.assertEqual(rebound, self.store.get_studio_binding("default", "demo/repo"))
        other = bind(None, 0, "other")
        self.assertEqual(1, other["revision"])
        with self.store._connect() as conn:
            audits = conn.execute(
                "SELECT actor,detail_json FROM audit_log WHERE tenant_id='default' "
                "AND action='studio.repository_bound' ORDER BY id"
            ).fetchall()
        self.assertEqual(4, len(audits))
        self.assertEqual({"operator"}, {item["actor"] for item in audits})
        self.assertIsNone(audits[0]["detail_json"]["previous"])
        self.assertEqual(
            {"workflow_id": current["workflow_id"], "version": current["version"], "revision": 2},
            audits[2]["detail_json"]["previous"],
        )
        self.assertEqual(
            {"workflow_id": None, "version": None, "revision": 3},
            audits[3]["detail_json"]["previous"],
        )

    def test_conflicts_tenant_isolation_and_tampered_snapshot(self):
        flow, published, _security = self.example()
        with self.assertRaises(StateConflictError):
            self.studio.save(
                "default",
                "workflows",
                {"id": flow["id"], "revision": 99, "definition": flow["definition"]},
                "author",
            )
        self.assertIsNone(self.store.get_studio_document("other", "workflows", flow["id"]))
        self.assertEqual([], self.store.list_studio_documents("other", "agents"))
        with self.assertRaises(ClientInputError):
            self.studio.select("other", "demo/repo", {"id": flow["id"], "version": 1})
        with self.assertRaises(ClientInputError):
            self.studio.resolve("other", flow["definition"])
        selected = self.studio.select("default", "demo/repo", {"id": flow["id"], "version": 1})
        selected["bundle"]["definition"]["name"] = "tampered"
        with self.assertRaises(HandoffError):
            self.service.review_engine.build_studio_harness(selected)
        invalid = copy.deepcopy(published["definition"])
        invalid["definition"]["steps"][2]["sources"]["security"] = "report.findings"
        with self.assertRaises(ClientInputError):
            compile_workflow(
                invalid, self.studio.builtins(), self.service.model_gateway, self.studio.context
            )
        invalid = copy.deepcopy(published["definition"])
        invalid["definition"]["steps"][2]["sources"]["security"] = "$input.diff"
        with self.assertRaises(ClientInputError):
            compile_workflow(
                invalid, self.studio.builtins(), self.service.model_gateway, self.studio.context
            )
        self.assertIsNone(self.studio.artifact("other", "not-a-task", "report"))

    def test_incomplete_drafts_have_editable_shapes_but_cannot_publish(self):
        for kind in ("agents", "workflows"):
            with self.subTest(kind=kind):
                definition = {"name": "Incomplete draft"}
                saved = self.studio.save(
                    "default", kind, {"revision": 0, "definition": definition}, "author"
                )
                self.assertEqual({"name": "Incomplete draft"}, definition)
                if kind == "workflows":
                    self.assertEqual([], saved["definition"]["steps"])
                    self.assertEqual({"verified": ""}, saved["definition"]["outputs"])
                else:
                    self.assertEqual("rules", saved["definition"]["kind"])
                    self.assertEqual({"rules": [], "checks": []}, saved["definition"]["config"])
                with self.assertRaises(ClientInputError):
                    self.studio.publish("default", kind, saved["id"], 1, "author")
                self.assertEqual(
                    [], self.store.get_studio_document("default", kind, saved["id"])["versions"]
                )

    def test_retry_uses_pinned_workflow_and_reuses_completed_upstream_artifacts(self):
        flow, _, security = self.example()

        def temporarily_broken(key, definition, gateway, context):
            agent = build_agent(key, definition, gateway, context)
            if definition["kind"] == "merge":

                def unavailable(_handoff):
                    raise RuntimeError("temporary dependency outage")

                return replace(agent, run=unavailable)
            return agent

        with mock.patch("evoagent.studio.build_agent", side_effect=temporarily_broken):
            with self.assertRaises(RuntimeError):
                self.service.create_review(
                    "demo/repo",
                    DIFF_TEXT,
                    workflow_selection={"id": flow["id"], "version": 1},
                )
        task = self.store.list_tasks(tenant_id="default")[0]
        self.assertEqual("FAILED", task["state"])
        task_id = task["id"]
        snapshot = self.store.get(task_id)["input"]["studio_workflow"]
        before = self.store.load_checkpoints(task_id)
        self.assertEqual("completed", before["workflow:security"]["status"])
        self.assertEqual("completed", before["workflow:business"]["status"])
        self.assertEqual("failed", before["workflow:report"]["status"])
        self.publish("agents", rules("新版安全审查", ["REL-DEBUG-PRINT"]), previous=security)
        newer = copy.deepcopy(flow["definition"])
        newer["steps"][0]["version"] = 2
        self.publish("workflows", newer, previous=flow)
        self.store.bind_studio_workflow(
            "default", "demo/repo", flow["id"], "author", version=2, expected_revision=0
        )
        self.assertTrue(self.service.resume_task(task_id, "default")["resumed"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.store.get(task_id)["state"] != "SUCCESS":
            time.sleep(0.02)
        finished = self.store.get(task_id)
        self.assertEqual("SUCCESS", finished["state"])
        self.assertEqual(snapshot, finished["input"]["studio_workflow"])
        self.assertEqual(
            {"SEC-EVAL", "BIZ-LOG"}, {item["rule_id"] for item in finished["report"]["findings"]}
        )
        after = self.store.load_checkpoints(task_id)
        for node in ("workflow:security", "workflow:business"):
            self.assertEqual(before[node], after[node])
        self.assertEqual(
            before["workflow:report"]["attempt"] + 1, after["workflow:report"]["attempt"]
        )

    def test_async_trial_replay_after_draft_changes_and_api_permissions(self):
        flow, _, _ = self.example()
        selection = {"id": flow["id"], "draft_revision": flow["revision"]}
        accepted = self.service.enqueue_review(
            "demo/repo", DIFF_TEXT, workflow_selection=selection, idempotency_key="studio-trial"
        )
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and self.store.get(accepted["task_id"])["state"] != "SUCCESS"
        ):
            time.sleep(0.02)
        self.assertEqual("SUCCESS", self.store.get(accepted["task_id"])["state"])
        changed = {**flow["definition"], "name": "renamed draft"}
        self.studio.save(
            "default",
            "workflows",
            {"id": flow["id"], "revision": flow["revision"], "definition": changed},
            "author",
        )
        replay = self.service.enqueue_review(
            "demo/repo", DIFF_TEXT, workflow_selection=selection, idempotency_key="studio-trial"
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(accepted["task_id"], replay["task_id"])
        with self.assertRaises(ClientInputError):
            self.service.enqueue_review(
                "demo/repo",
                DIFF_TEXT,
                workflow_selection={"id": flow["id"], "version": 1},
                idempotency_key="studio-trial",
            )

        auth = AuthManager(
            self.store,
            self.settings.auth_secret,
            bootstrap_username="studio-admin",
            bootstrap_password="studio-password",
        )
        self.service.auth = auth
        token = auth.login("studio-admin", "studio-password")["access_token"]
        principal = auth.authenticate("Bearer " + token)
        auth.provision_user(principal, "studio-reader", "reader-password", "auditor")
        reader = auth.login("studio-reader", "reader-password")["access_token"]
        settings = _settings(self.url, auth_required=True)
        server = _make_server(settings, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, body=None, bearer="", console=False):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request(
                    method,
                    path,
                    json.dumps(body) if body is not None else None,
                    {
                        "Content-Type": "application/json",
                        **({"Authorization": "Bearer " + bearer} if bearer else {}),
                        **({"X-EvoAgent-View": "console"} if console else {}),
                    },
                )
                response = conn.getresponse()
                self.assertEqual("X-EvoAgent-View", response.getheader("Vary"))
                self.assertEqual("no-store", response.getheader("Cache-Control"))
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        self.assertEqual(401, request("GET", "/v1/studio/catalog")[0])
        self.assertEqual(403, request("GET", "/v1/studio/catalog", bearer=reader)[0])
        status, first_page = request(
            "GET", "/v1/studio/agents?limit=1", bearer=reader, console=True
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(first_page["documents"]))
        cursor = quote(first_page["next_cursor"], safe="")
        status, next_page = request(
            "GET", "/v1/studio/agents?limit=1&cursor=" + cursor, bearer=reader, console=True
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(next_page["documents"]))
        self.assertNotEqual(first_page["documents"][0]["id"], next_page["documents"][0]["id"])
        for document in first_page["documents"] + next_page["documents"]:
            self.assertNotIn("definition", document)
        for query in ("limit=101", "limit=1&limit=2", "cursor=bad", "cursor=a&cursor=b"):
            self.assertEqual(
                400, request("GET", "/v1/studio/agents?" + query, bearer=reader, console=True)[0]
            )
        binding_path = "/v1/studio/binding"
        binding_query = binding_path + "?repository=demo/repo"
        body = {"repository": "demo/repo", "workflow_id": flow["id"], "version": 1, "revision": 0}
        self.assertEqual(403, request("POST", binding_path, body)[0])
        self.assertEqual(403, request("POST", binding_path, body, "invalid-token")[0])
        self.assertEqual(403, request("POST", binding_path, body, reader, True)[0])
        self.assertIsNone(request("GET", binding_query, bearer=reader, console=True)[1]["binding"])
        for invalid in (
            {"repository": "demo/repo", "workflow_id": flow["id"]},
            {**body, "version": True},
            {**body, "version": None},
            {**body, "version": 0},
            {**body, "revision": True},
            {**body, "revision": -1},
            {**body, "revision": "0"},
            {**body, "unknown": "field"},
            {**body, "workflow_id": None},
        ):
            self.assertEqual(400, request("POST", binding_path, invalid, token, True)[0])
        self.assertEqual(404, request("POST", binding_path, {**body, "version": 999}, token)[0])
        status, pinned = request("POST", binding_path, body, token, True)
        self.assertEqual(200, status)
        self.assertEqual("我的审查流程", pinned["binding"]["name"])
        self.assertEqual(1, pinned["binding"]["version"])
        self.assertEqual(1, pinned["binding"]["revision"])
        self.assertEqual(pinned, request("GET", binding_query, bearer=reader, console=True)[1])
        self.assertEqual(409, request("POST", binding_path, body, token, True)[0])
        publication = request(
            "GET", "/v1/studio/workflows/%s/versions/1" % flow["id"], bearer=reader, console=True
        )[1]
        self.assertEqual({"version", "draft_revision", "created_at", "name"}, publication.keys())
        self.assertEqual("我的审查流程", publication["name"])
        status, unbound = request(
            "POST",
            binding_path,
            {**body, "workflow_id": None, "version": None, "revision": 1},
            token,
            True,
        )
        self.assertEqual(200, status)
        self.assertEqual(2, unbound["binding"]["revision"])
        self.assertIsNone(unbound["binding"]["workflow_id"])
        self.assertEqual(409, request("POST", binding_path, body, token, True)[0])
        self.assertEqual(unbound, request("GET", binding_query, bearer=reader, console=True)[1])
        for kind, key in (("agents", "1" * 32), ("workflows", "2" * 32)):
            sparse = {"name": "Legacy draft"}
            self.store.save_studio_draft("default", kind, key, 0, sparse, "fixture")
            draft_path = "/v1/studio/%s/%s" % (kind, key)
            status, editable = request("GET", draft_path, bearer=token, console=True)
            self.assertEqual(200, status)
            self.assertEqual(draft_definition(kind, sparse), editable["definition"])
            original_draft = request("GET", draft_path, bearer=token)[1]
            self.assertEqual(sparse, original_draft["definition"])
            self.assertEqual(1, original_draft["revision"])
            invalid = {
                "name": "Invalid",
                **({"steps": None} if kind == "workflows" else {"config": None}),
            }
            self.assertEqual(
                400,
                request(
                    "POST",
                    "/v1/studio/" + kind,
                    {"id": key, "revision": 1, "definition": invalid},
                    token,
                    True,
                )[0],
            )
            self.assertEqual(original_draft, request("GET", draft_path, bearer=token)[1])
        invalid = {"name": "Legacy invalid", "steps": None}
        self.store.save_studio_draft("default", "workflows", "3" * 32, 0, invalid, "fixture")
        draft_path = "/v1/studio/workflows/" + "3" * 32
        status, refused = request("GET", draft_path, bearer=token, console=True)
        self.assertEqual(400, status)
        self.assertEqual({"error_code": "unsupported_draft"}, refused)
        self.assertEqual(invalid, request("GET", draft_path, bearer=token)[1]["definition"])
        self.assertEqual(
            403,
            request("POST", "/v1/studio/agents", {"revision": 0, "definition": rules()}, reader)[0],
        )
        status, saved = request(
            "POST", "/v1/studio/agents", {"revision": 0, "definition": rules("HTTP-created")}, token
        )
        self.assertEqual(201, status)
        self.assertEqual(
            201,
            request(
                "POST",
                "/v1/studio/agents/%s/publish" % saved["id"],
                {"revision": saved["revision"]},
                token,
            )[0],
        )
        self.assertEqual(
            200,
            request(
                "GET", "/v1/studio/agents/%s/versions/1" % saved["id"], bearer=reader, console=True
            )[0],
        )

        task_path = "/v1/tasks/" + accepted["task_id"]
        original = request("GET", task_path, bearer=token)[1]
        self.assertIn("studio_workflow", original["input"])
        self.assertEqual(401, request("GET", task_path, console=True)[0])
        status, view = request("GET", task_path, bearer=reader, console=True)
        self.assertEqual(200, status)
        self.assertEqual("SUCCESS", view["state"])
        self.assertEqual("我的审查流程", view["workflow"]["name"])
        self.assertEqual("安全审查", view["workflow"]["steps"]["security"])
        self.assertFalse({"input", "trace", "collaboration", "tenant_id", "error"} & view.keys())
        self.assertNotIn("fingerprint", json.dumps(view))
        self.assertEqual(original, request("GET", task_path, bearer=token)[1])
        snapshot = request("GET", task_path + "/workflow", bearer=reader, console=True)[1]
        self.assertEqual(3, len(snapshot["steps"]))
        self.assertNotIn("revision", snapshot["workflow"])
        self.assertFalse(
            {"agent_id", "agent_revision", "generation", "input_sha256", "idempotency_key"}
            & snapshot["steps"][0].keys()
        )
        artifact = request("GET", task_path + "/workflow/report", bearer=reader, console=True)[1]
        self.assertEqual(2, len(artifact["outputs"]["findings"]))
        self.assertNotIn("fingerprint", json.dumps(artifact))
        self.assertNotIn("output_sha256", artifact)
        self.assertEqual({"business", "security"}, artifact["inputs"].keys())
        page = request("GET", "/api/dashboard", bearer=reader, console=True)[1]
        self.assertEqual({"stats", "tasks", "capabilities"}, page.keys())
        self.assertEqual("auditor", page["capabilities"]["role"])
        self.assertFalse(page["capabilities"]["review"])
        self.assertFalse(page["capabilities"]["github_install_configured"])
        self.assertNotIn("tenant_id", json.dumps(page))

        secret_agent = model_agent()
        secret_agent["config"]["prompt"] = "PRIVATE-PROMPT-FOR-EDITOR"
        saved_agent, _ = self.publish("agents", secret_agent)
        path = "/v1/studio/agents/" + saved_agent["id"]
        palette = request("GET", path + "/versions/1", bearer=reader, console=True)[1]
        self.assertNotIn("PRIVATE-PROMPT", json.dumps(palette))
        self.assertNotIn("digest", palette)
        editor = request("GET", path, bearer=token, console=True)[1]
        self.assertEqual(secret_agent, editor["definition"])
        publication = request(
            "POST", path + "/publish", {"revision": saved_agent["revision"]}, token, True
        )[1]
        self.assertEqual(1, publication["version"])
        self.assertNotIn("definition", publication)
        self.assertNotIn("digest", publication)
        workflow_path = "/v1/studio/workflows/" + flow["id"]
        raw_paths = [
            task_path,
            task_path + "/workflow",
            task_path + "/workflow/report",
            path + "/versions/1",
            workflow_path + "/versions/1",
        ]
        admin = auth.provision_user(principal, "tenant-admin", "admin-password", "admin")
        admin_token = auth.login("tenant-admin", "admin-password")["access_token"]
        auth.provision_user(principal, "maintainer", "review-password", "maintainer")
        maintainer = auth.login("maintainer", "review-password")["access_token"]
        for bearer in (reader, maintainer):
            for resource in raw_paths:
                with self.subTest(resource=resource):
                    status, denied = request("GET", resource, bearer=bearer)
                    self.assertEqual(403, status)
                    self.assertEqual({"error": "permission denied"}, denied)
                    status, readable = request("GET", resource, bearer=bearer, console=True)
                    self.assertEqual(200, status)
                    self.assertNotIn("PRIVATE-PROMPT", json.dumps(readable))
            for resource in (path, workflow_path, "/v1/studio/catalog"):
                for console in (False, True):
                    status, denied = request("GET", resource, bearer=bearer, console=console)
                    self.assertEqual(403, status)
                    self.assertNotIn("PRIVATE-PROMPT", json.dumps(denied))
            # Report-only access remains useful without a diagnostic view header.
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request(
                    "GET", task_path + "/report", headers={"Authorization": "Bearer " + bearer}
                )
                report_response = conn.getresponse()
                self.assertEqual(200, report_response.status)
                report_text = report_response.read().decode()
                self.assertIn("# EvoAgent PR Review", report_text)
                self.assertNotIn("PRIVATE-PROMPT", report_text)
            finally:
                conn.close()
        for bearer in (admin_token, token):
            for resource in (*raw_paths, path, workflow_path, "/v1/studio/catalog"):
                self.assertEqual(200, request("GET", resource, bearer=bearer)[0])
            self.assertIn("PRIVATE-PROMPT", json.dumps(request("GET", path, bearer=bearer)[1]))
        for target, method, resource, console in (
            (self.store, "get", task_path, False),
            (self.store, "workflow_status", task_path + "/workflow", False),
            (self.studio, "artifact", task_path + "/workflow/report", False),
            (self.store, "get_studio_document", path, True),
            (self.store, "get_studio_version", path + "/versions/1", False),
            (self.studio, "catalog", "/v1/studio/catalog", True),
        ):
            with mock.patch.object(
                target, method, side_effect=AssertionError("lookup before authorization")
            ) as lookup:
                self.assertEqual(403, request("GET", resource, bearer=reader, console=console)[0])
                lookup.assert_not_called()
        # Role membership is re-read: an old admin token does not retain access.
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE memberships SET role='auditor' WHERE user_id=%s AND tenant_id='default'",
                (admin["id"],),
            )
        self.assertEqual(403, request("GET", path, bearer=admin_token, console=True)[0])
        self.assertEqual(403, request("GET", task_path, bearer=admin_token)[0])
        self.assertEqual(200, request("GET", task_path, bearer=admin_token, console=True)[0])
        self.assertEqual(
            403, request("GET", "/v1/evolution/status", bearer=reader, console=True)[0]
        )
        self.assertEqual(
            403,
            request(
                "POST", "/v1/studio/agents", {"revision": 0, "definition": rules()}, reader, True
            )[0],
        )
        status, submitted = request(
            "POST",
            "/v1/reviews?async=true",
            {
                "repository": "demo/repo",
                "diff": DIFF_TEXT,
                "workflow": {"id": flow["id"], "version": 1},
            },
            token,
            True,
        )
        self.assertEqual(202, status)
        self.assertEqual({"task_id", "state", "replayed"}, submitted.keys())
        status, login = request(
            "POST",
            "/v1/auth/login",
            {"username": "studio-reader", "password": "reader-password"},
            console=True,
        )
        self.assertEqual(200, status)
        self.assertEqual({"access_token", "role"}, login.keys())

        other, _ = self.service.review_use_cases.create_task(
            "demo/other", DIFF_TEXT, None, "api", tenant_id="other"
        )
        for suffix in ("", "/workflow", "/workflow/report"):
            self.assertEqual(
                404, request("GET", "/v1/tasks/" + other + suffix, bearer=reader, console=True)[0]
            )
            self.assertEqual(404, request("GET", "/v1/tasks/" + other + suffix, bearer=token)[0])
        other_draft = self.studio.save(
            "other", "agents", {"revision": 0, "definition": rules("Other tenant")}, "author"
        )
        self.studio.publish("other", "agents", other_draft["id"], other_draft["revision"], "author")
        other_path = "/v1/studio/agents/" + other_draft["id"]
        for resource in (other_path, other_path + "/versions/1"):
            for console in (False, True):
                self.assertEqual(404, request("GET", resource, bearer=token, console=console)[0])
        self.assertEqual(
            404, request("GET", other_path + "/versions/1", bearer=reader, console=True)[0]
        )
        with mock.patch.object(self.service, "run_proof") as unexpected_write:
            self.assertEqual(400, request("POST", "/v1/proofs", {}, token, True)[0])
            unexpected_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
