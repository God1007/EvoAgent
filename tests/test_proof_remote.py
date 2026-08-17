import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from evoagent.config import Settings
from evoagent.proof import EvidenceLevel, ProofRunner
from evoagent.proof_remote import (
    HEADER_BODY_SHA256,
    HEADER_INPUT_SHA256,
    HEADER_ISSUED_AT,
    HEADER_KEY_ID,
    HEADER_REQUEST_ID,
    HEADER_SIGNATURE,
    BoundedThreadingHTTPServer,
    ContentAddressedArtifactStore,
    ProofProtocolError,
    ProofRunnerServer,
    ProofRunnerSettings,
    ProofRunnerUnavailableError,
    RedisProofReplayStore,
    RemoteProofExecutor,
    _handler,
    _sign,
    canonical_json,
    sha256_hex,
)

SECRET = "proof-test-signing-secret-at-least-32-bytes"
NEXT_SECRET = "next-proof-signing-secret-at-least-32-bytes"


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.available = True

    def set(self, key, value, nx=False, ex=None):
        if not self.available:
            raise ConnectionError("redis unavailable")
        if nx and key in self.values:
            return False
        self.values[key] = (value, ex)
        return True

    def ping(self):
        if not self.available:
            raise ConnectionError("redis unavailable")
        return True

    def close(self):
        return None


def _outcome(status="passed"):
    return {
        "passed": status == "passed",
        "status": status,
        "checks": [
            {
                "name": "repository-tests",
                "passed": status == "passed",
                "status": status,
                "detail": "bounded output",
            }
        ],
        "duration_seconds": 0.01,
    }


class FakeExecutor:
    def __init__(self, outcome=None):
        self.outcome = outcome or _outcome()
        self.calls = []

    def execute(self, files, command):
        self.calls.append((files, command))
        return dict(self.outcome)


class FakeResponse:
    def __init__(self, body, headers, status=200):
        self._body = body
        self.headers = headers
        self.status = status

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class LoopbackOpener:
    def __init__(self, server, mutate_request=None, mutate_response=None):
        self.server = server
        self.mutate_request = mutate_request
        self.mutate_response = mutate_response
        self.last_request = None

    def open(self, request, timeout):
        self.last_request = request
        body = request.data
        headers = dict(request.header_items())
        if self.mutate_request:
            body, headers = self.mutate_request(body, headers)
        response = self.server.execute(body, headers)
        response_body, response_headers = response.body, dict(response.headers)
        if self.mutate_response:
            response_body, response_headers = self.mutate_response(response_body, response_headers)
        return FakeResponse(response_body, response_headers)


class RemoteProtocolTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        self.server = ProofRunnerServer(self.executor, SECRET, clock=lambda: 1000)

    def _client(self, opener=None, clock=lambda: 1000):
        return RemoteProofExecutor(
            "http://127.0.0.1:8091/v1/execute",
            SECRET,
            ("127.0.0.1",),
            opener=opener or LoopbackOpener(self.server),
            clock=clock,
        )

    def test_signed_round_trip_returns_bound_attestation(self):
        result = self._client().execute({"app.py": "print('ok')\n"}, "pytest -q")

        self.assertTrue(result["passed"])
        self.assertEqual("pytest -q", self.executor.calls[0][1])
        self.assertEqual(64, len(result["attestation"]["input_sha256"]))
        self.assertEqual(64, len(result["attestation"]["evidence_sha256"]))

    def test_request_body_tampering_is_rejected_before_execution(self):
        def tamper(body, headers):
            changed = body.replace(b"pytest -q", b"pytest -x")
            return changed, headers

        client = self._client(LoopbackOpener(self.server, mutate_request=tamper))
        with self.assertRaisesRegex(ProofProtocolError, "digest mismatch"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")
        self.assertEqual([], self.executor.calls)

    def test_response_body_tampering_is_rejected(self):
        def tamper(body, headers):
            document = json.loads(body)
            document["outcome"]["passed"] = False
            return canonical_json(document), headers

        client = self._client(LoopbackOpener(self.server, mutate_response=tamper))
        with self.assertRaisesRegex(ProofProtocolError, "body digest mismatch"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")

    def test_response_signature_tampering_is_rejected(self):
        def tamper(body, headers):
            headers["X-EvoAgent-Signature"] = "sha256=" + "0" * 64
            return body, headers

        client = self._client(LoopbackOpener(self.server, mutate_response=tamper))
        with self.assertRaisesRegex(ProofProtocolError, "signature is invalid"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")

    def test_wrong_request_signing_key_is_rejected(self):
        client = RemoteProofExecutor(
            "http://127.0.0.1:8091/v1/execute",
            "different-proof-signing-secret-at-least-32-bytes",
            ("127.0.0.1",),
            opener=LoopbackOpener(self.server),
            clock=lambda: 1000,
        )
        with self.assertRaisesRegex(ProofProtocolError, "signature is invalid"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")
        self.assertEqual([], self.executor.calls)

    def test_request_replay_is_rejected(self):
        opener = LoopbackOpener(self.server)
        self._client(opener).execute({"app.py": "x = 1\n"}, "pytest -q")
        request = opener.last_request

        with self.assertRaisesRegex(ProofProtocolError, "replay detected"):
            self.server.execute(request.data, dict(request.header_items()))
        self.assertEqual(1, len(self.executor.calls))

    def test_shared_redis_replay_store_rejects_replay_on_a_second_replica(self):
        redis = FakeRedis()
        first = ProofRunnerServer(
            self.executor,
            SECRET,
            replay_store=RedisProofReplayStore(
                "redis://127.0.0.1/0", client=redis, clock=lambda: 1000
            ),
            clock=lambda: 1000,
        )
        second = ProofRunnerServer(
            self.executor,
            SECRET,
            replay_store=RedisProofReplayStore(
                "redis://127.0.0.1/0", client=redis, clock=lambda: 1000
            ),
            clock=lambda: 1000,
        )
        opener = LoopbackOpener(first)
        self._client(opener).execute({"app.py": "x = 1\n"}, "pytest -q")
        request = opener.last_request

        with self.assertRaisesRegex(ProofProtocolError, "replay detected"):
            second.execute(request.data, dict(request.header_items()))
        self.assertEqual(1, len(self.executor.calls))

    def test_replay_store_outage_fails_closed_before_execution_and_readiness(self):
        redis = FakeRedis()
        redis.available = False
        server = ProofRunnerServer(
            self.executor,
            SECRET,
            replay_store=RedisProofReplayStore(
                "redis://127.0.0.1/0", client=redis, clock=lambda: 1000
            ),
            clock=lambda: 1000,
        )
        client = self._client(LoopbackOpener(server))

        with self.assertRaisesRegex(ProofRunnerUnavailableError, "replay store"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")
        self.assertEqual([], self.executor.calls)
        self.assertEqual(
            {"status": "not-ready", "replay_backend": "redis", "signing_key_ids": 1},
            server.readiness(),
        )

    def test_current_and_previous_signing_keys_overlap_without_downgrade(self):
        server = ProofRunnerServer(
            self.executor,
            NEXT_SECRET,
            signing_key_id="2026-09",
            verification_keys={"2026-08": SECRET},
            clock=lambda: 1000,
        )
        previous = RemoteProofExecutor(
            "http://127.0.0.1:8091/v1/execute",
            SECRET,
            ("127.0.0.1",),
            signing_key_id="2026-08",
            opener=LoopbackOpener(server),
            clock=lambda: 1000,
        )
        current = RemoteProofExecutor(
            "http://127.0.0.1:8091/v1/execute",
            NEXT_SECRET,
            ("127.0.0.1",),
            signing_key_id="2026-09",
            opener=LoopbackOpener(server),
            clock=lambda: 1000,
        )

        self.assertEqual(
            "2026-08", previous.execute({"old.py": "x=1\n"}, "test")["attestation"]["key_id"]
        )
        self.assertEqual(
            "2026-09", current.execute({"new.py": "x=2\n"}, "test")["attestation"]["key_id"]
        )
        retired = ProofRunnerServer(
            self.executor,
            NEXT_SECRET,
            signing_key_id="2026-09",
            clock=lambda: 1000,
        )
        rejected = RemoteProofExecutor(
            "http://127.0.0.1:8091/v1/execute",
            SECRET,
            ("127.0.0.1",),
            signing_key_id="2026-08",
            opener=LoopbackOpener(retired),
            clock=lambda: 1000,
        )
        with self.assertRaisesRegex(ProofProtocolError, "signature is invalid"):
            rejected.execute({"old.py": "x=1\n"}, "test")

    def test_legacy_request_without_key_id_can_only_select_literal_default(self):
        def legacy_request(body, headers):
            document = json.loads(body)
            document.pop("key_id")
            body = canonical_json(document)
            replaced_headers = {
                HEADER_KEY_ID.lower(),
                HEADER_BODY_SHA256.lower(),
                HEADER_SIGNATURE.lower(),
            }
            headers = {
                key: value for key, value in headers.items() if key.lower() not in replaced_headers
            }

            def header(name):
                return next(value for key, value in headers.items() if key.lower() == name.lower())

            body_sha = sha256_hex(body)
            headers[HEADER_BODY_SHA256] = body_sha
            headers[HEADER_SIGNATURE] = _sign(
                SECRET.encode("utf-8"),
                "request",
                header(HEADER_REQUEST_ID),
                header(HEADER_ISSUED_AT),
                body_sha,
                header(HEADER_INPUT_SHA256),
            )
            return body, headers

        overlap = ProofRunnerServer(
            self.executor,
            NEXT_SECRET,
            signing_key_id="2026-09",
            verification_keys={"default": SECRET},
            clock=lambda: 1000,
        )
        result = self._client(LoopbackOpener(overlap, mutate_request=legacy_request)).execute(
            {"old.py": "x=1\n"}, "test"
        )
        self.assertEqual("default", result["attestation"]["key_id"])

        named_only = ProofRunnerServer(
            self.executor,
            NEXT_SECRET,
            signing_key_id="2026-09",
            clock=lambda: 1000,
        )
        with self.assertRaisesRegex(ProofProtocolError, "signature is invalid"):
            self._client(LoopbackOpener(named_only, mutate_request=legacy_request)).execute(
                {"old.py": "x=1\n"}, "test"
            )

    def test_response_key_id_tampering_is_rejected(self):
        def tamper(body, headers):
            headers[HEADER_KEY_ID] = "attacker-key"
            return body, headers

        client = self._client(LoopbackOpener(self.server, mutate_response=tamper))
        with self.assertRaisesRegex(ProofProtocolError, "key id mismatch"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")

    def test_replay_guard_memory_is_bounded(self):
        server = ProofRunnerServer(self.executor, SECRET, max_replay_entries=1, clock=lambda: 1000)
        self._client(LoopbackOpener(server)).execute({"a.py": "x = 1\n"}, "pytest -q")
        with self.assertRaisesRegex(ProofProtocolError, "guard capacity"):
            self._client(LoopbackOpener(server)).execute({"b.py": "x = 2\n"}, "pytest -q")

    def test_expired_request_is_rejected(self):
        client = self._client(LoopbackOpener(self.server), clock=lambda: 1)
        with self.assertRaisesRegex(ProofProtocolError, "replay window"):
            client.execute({"app.py": "x = 1\n"}, "pytest -q")

    def test_invalid_executor_outcome_becomes_signed_uncertainty(self):
        server = ProofRunnerServer(FakeExecutor({"passed": True}), SECRET, clock=lambda: 1000)
        result = self._client(LoopbackOpener(server)).execute({"app.py": "x = 1\n"}, "pytest -q")
        self.assertEqual("error", result["status"])
        self.assertFalse(result["passed"])

    def test_exact_host_allowlist_and_transport_rules(self):
        with self.assertRaisesRegex(ValueError, "exact allowlist"):
            RemoteProofExecutor("https://runner.example/v1/execute", SECRET, ("example",))
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            RemoteProofExecutor("http://runner.example/v1/execute", SECRET, ("runner.example",))
        with self.assertRaisesRegex(ValueError, "path must be"):
            RemoteProofExecutor("https://runner.example/other", SECRET, ("runner.example",))

    def test_request_and_response_byte_limits_fail_closed(self):
        client = RemoteProofExecutor(
            "http://127.0.0.1:8091",
            SECRET,
            ("127.0.0.1",),
            max_request_bytes=1024,
            opener=LoopbackOpener(self.server),
            clock=lambda: 1000,
        )
        with self.assertRaisesRegex(ProofProtocolError, "request exceeds"):
            client.execute({"large.py": "x" * 2000}, "pytest -q")

        def inflate(_body, headers):
            body = b"x" * 1025
            headers["Content-Length"] = str(len(body))
            return body, headers

        client = RemoteProofExecutor(
            "http://127.0.0.1:8091",
            SECRET,
            ("127.0.0.1",),
            max_response_bytes=1024,
            opener=LoopbackOpener(self.server, mutate_response=inflate),
            clock=lambda: 1000,
        )
        with self.assertRaisesRegex(ProofProtocolError, "response exceeds"):
            client.execute({"a.py": "x = 1\n"}, "pytest -q")

    def test_capacity_exhaustion_is_a_signed_error_not_a_failed_test(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingExecutor:
            @staticmethod
            def execute(_files, _command):
                entered.set()
                release.wait(2)
                return _outcome()

            @staticmethod
            def health():
                return {"healthy": True}

        server = ProofRunnerServer(
            BlockingExecutor(), SECRET, max_concurrency=1, clock=lambda: 1000
        )
        first_result = []

        def execute_first():
            first_result.append(
                self._client(LoopbackOpener(server)).execute({"a.py": "x = 1\n"}, "pytest -q")
            )

        thread = threading.Thread(target=execute_first)
        thread.start()
        self.assertTrue(entered.wait(1))
        second = self._client(LoopbackOpener(server)).execute({"b.py": "x = 2\n"}, "pytest -q")
        release.set()
        thread.join(2)

        self.assertEqual("error", second["status"])
        self.assertIn("capacity", second["checks"][0]["detail"])
        self.assertTrue(first_result[0]["passed"])

    def test_proof_ladder_preserves_remote_attestation(self):
        outcomes = iter((_outcome("failed"), _outcome("passed")))

        class SequenceExecutor:
            def execute(self, _files, _command):
                result = next(outcomes)
                result["attestation"] = {"request_id": "verified"}
                return result

        result = ProofRunner(executor=SequenceExecutor()).prove(
            {"a.py": "bug\n"}, {"a.py": "fixed\n"}, "pytest -q"
        )
        self.assertEqual(int(EvidenceLevel.L3_FIX_VERIFIED), result["evidence_level"])
        self.assertEqual("verified", result["steps"][0]["attestation"]["request_id"])

    def test_executor_transport_failure_is_inconclusive_not_reproduced(self):
        class BrokenExecutor:
            def execute(self, _files, _command):
                raise ProofProtocolError("proof runner transport failed")

        result = ProofRunner(executor=BrokenExecutor()).prove(
            {"a.py": "bug\n"}, {"a.py": "fixed\n"}, "pytest -q"
        )
        self.assertEqual(int(EvidenceLevel.L1_STATIC), result["evidence_level"])
        self.assertEqual("error", result["steps"][0]["status"])
        self.assertIn("inconclusive", result["note"])


class ArtifactStoreTests(unittest.TestCase):
    def test_content_addressed_artifacts_are_immutable_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as root:
            store = ContentAddressedArtifactStore(root)
            first = store.put("inputs", b'{"a":1}')
            second = store.put("inputs", b'{"a":1}')
            other = store.put("inputs", b'{"a":2}')

            self.assertEqual(first, second)
            self.assertNotEqual(first, other)
            digest = first.removeprefix("sha256:")
            path = os.path.join(root, "inputs", digest[:2], digest + ".json")
            with open(path, "rb") as handle:
                self.assertEqual(b'{"a":1}', handle.read())

    def test_required_artifacts_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "required proof artifact"):
            ProofRunnerServer(FakeExecutor(), SECRET, require_artifacts=True, artifact_store=None)

    def test_attestation_contains_persisted_input_and_evidence_addresses(self):
        with tempfile.TemporaryDirectory() as root:
            server = ProofRunnerServer(
                FakeExecutor(),
                SECRET,
                artifact_store=ContentAddressedArtifactStore(root),
                require_artifacts=True,
                clock=lambda: 1000,
            )
            client = RemoteProofExecutor(
                "http://127.0.0.1:8091",
                SECRET,
                ("127.0.0.1",),
                opener=LoopbackOpener(server),
                clock=lambda: 1000,
            )
            result = client.execute({"a.py": "x = 1\n"}, "pytest -q")
            artifacts = result["attestation"]["artifacts"]
            self.assertTrue(artifacts["input"].startswith("sha256:"))
            self.assertTrue(artifacts["evidence"].startswith("sha256:"))


class RunnerDeploymentTests(unittest.TestCase):
    def test_runner_settings_read_only_the_dedicated_environment_contract(self):
        with mock.patch.dict(
            os.environ,
            {
                "EVOAGENT_PROOF_RUNNER_SIGNING_KEY": SECRET,
                "EVOAGENT_PROOF_RUNNER_SIGNING_KEY_ID": "2026-09",
                "EVOAGENT_PROOF_RUNNER_PREVIOUS_SIGNING_KEY_ID": "2026-08",
                "EVOAGENT_PROOF_RUNNER_PREVIOUS_SIGNING_KEY": NEXT_SECRET,
                "EVOAGENT_PROOF_RUNNER_REPLAY_REDIS_URL": "redis://replay:secret@redis/0",
                "EVOAGENT_PROOF_RUNNER_REQUIRE_SHARED_REPLAY": "true",
                "EVOAGENT_PROOF_RUNNER_CONTAINER_IMAGE": "python:3.12-slim",
                "EVOAGENT_GITHUB_TOKEN": "must-not-be-read",
            },
            clear=True,
        ):
            settings = ProofRunnerSettings.from_env()
        self.assertEqual("127.0.0.1", settings.host)
        self.assertEqual("python:3.12-slim", settings.container_image)
        self.assertEqual("2026-09", settings.signing_key_id)
        self.assertTrue(settings.require_shared_replay)
        self.assertNotIn(SECRET, repr(settings))
        self.assertNotIn(NEXT_SECRET, repr(settings))
        self.assertNotIn("redis:secret", repr(settings))
        self.assertFalse(hasattr(settings, "github_token"))

    def test_application_settings_fail_closed_for_partial_remote_configuration(self):
        base = Settings(
            host="127.0.0.1",
            port=8080,
            db_path=":memory:",
            max_diff_bytes=10000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
            proof_runner_url="https://proof.example/v1/execute",
        )
        with self.assertRaisesRegex(ValueError, "SIGNING_KEY"):
            base.validate_evolution()
        no_endpoint = Settings(
            **{
                **base.__dict__,
                "proof_runner_url": "",
                "proof_require_remote": True,
            }
        )
        with self.assertRaisesRegex(ValueError, "URL is required"):
            no_endpoint.validate_evolution()
        invalid_key_id = Settings(
            **{
                **base.__dict__,
                "proof_runner_signing_key": SECRET,
                "proof_runner_allowed_hosts": ("proof.example",),
                "proof_runner_signing_key_id": "bad key id",
            }
        )
        with self.assertRaisesRegex(ValueError, "KEY_ID"):
            invalid_key_id.validate_evolution()

    def test_runner_requires_container_and_strong_key(self):
        with self.assertRaisesRegex(ValueError, "CONTAINER_IMAGE"):
            ProofRunnerSettings(
                host="127.0.0.1",
                port=8091,
                signing_key=SECRET,
                container_image="",
            ).validate()
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            ProofRunnerSettings(
                host="127.0.0.1",
                port=8091,
                signing_key="short",
                container_image="python:3.12-slim",
            ).validate()

    def test_non_loopback_binding_requires_tls(self):
        with self.assertRaisesRegex(ValueError, "TLS is required"):
            ProofRunnerSettings(
                host="0.0.0.0",
                port=8091,
                signing_key=SECRET,
                container_image="python:3.12-slim",
            ).validate()

    def test_shared_replay_and_rotation_configuration_fail_closed(self):
        base = {
            "host": "127.0.0.1",
            "port": 8091,
            "signing_key": SECRET,
            "container_image": "python:3.12-slim",
        }
        with self.assertRaisesRegex(ValueError, "REPLAY_REDIS_URL"):
            ProofRunnerSettings(**base, require_shared_replay=True).validate()
        with self.assertRaisesRegex(ValueError, "configured together"):
            ProofRunnerSettings(
                **base,
                previous_signing_key_id="2026-08",
            ).validate()
        with self.assertRaisesRegex(ValueError, "must differ"):
            ProofRunnerSettings(
                **base,
                signing_key_id="same",
                previous_signing_key_id="same",
                previous_signing_key=NEXT_SECRET,
            ).validate()

    def test_actual_loopback_http_transport(self):
        executor = FakeExecutor()
        service = ProofRunnerServer(executor, SECRET)
        server = BoundedThreadingHTTPServer(("127.0.0.1", 0), _handler(service, 1024 * 1024), 8)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        client = RemoteProofExecutor(
            "http://127.0.0.1:%d" % port,
            SECRET,
            ("127.0.0.1",),
        )

        result = client.execute({"a.py": "x = 1\n"}, "pytest -q")

        self.assertTrue(result["passed"])
        self.assertTrue(client.health()["healthy"])

    def test_client_does_not_follow_runner_redirects(self):
        hits = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = RemoteProofExecutor(
            "http://127.0.0.1:%d" % server.server_address[1],
            SECRET,
            ("127.0.0.1",),
        )

        with self.assertRaisesRegex(ProofProtocolError, "HTTP 302"):
            client.execute({"a.py": "x = 1\n"}, "pytest -q")
        self.assertEqual(["/v1/execute"], hits)


if __name__ == "__main__":
    unittest.main()
