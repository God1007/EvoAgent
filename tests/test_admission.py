import hashlib
import hmac
import http.client
import io
import json
import re
import socket
import threading
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from unittest import mock

from evoagent.api import (
    DRAINING,
    ApiHandler,
    EvoAgentHTTPServer,
    _async_review_requested,
    _query_parameters,
    _request_target,
)
from evoagent.auth import Principal
from evoagent.config import Settings
from evoagent.errors import (
    AccessDeniedError,
    ClientInputError,
    ResourceNotFoundError,
    StateConflictError,
)
from evoagent.harness import TaskCancelled
from evoagent.metrics import Metrics
from evoagent.migrations import CURRENT_SCHEMA_VERSION
from evoagent.service import ReviewService
from tests.db_support import postgres_url, reset_postgres


class RequestFramingTests(unittest.TestCase):
    @staticmethod
    def handler(headers):
        handler = object.__new__(ApiHandler)
        handler.headers = http.client.HTTPMessage()
        for name, value in headers:
            handler.headers.add_header(name, value)
        handler.close_connection = False
        handler.settings = mock.Mock(max_diff_bytes=1)
        handler.rfile = mock.Mock()
        return handler

    def test_rejects_ambiguous_request_body_framing(self):
        cases = (
            (("Transfer-Encoding", "chunked"),),
            (("Content-Length", "1"), ("Content-Length", "2")),
        )
        for headers in cases:
            handler = self.handler(headers)
            with (
                self.subTest(headers=headers),
                self.assertRaisesRegex(ClientInputError, "ambiguous"),
            ):
                handler._declared_body_length()
            self.assertTrue(handler.close_connection)

    def test_oversized_body_closes_without_reading(self):
        handler = self.handler((("Content-Length", str(256 * 1024 + 2)),))

        with self.assertRaisesRegex(ClientInputError, "too large"):
            handler._read_body()

        self.assertTrue(handler.close_connection)
        handler.rfile.read.assert_not_called()

    def test_invalid_content_length_closes_connection(self):
        for value in ("-1", "+1", "1.0", "1 ", "9" * 4301):
            handler = self.handler((("Content-Length", value),))
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ClientInputError, "invalid Content-Length"),
            ):
                handler._declared_body_length()
            self.assertTrue(handler.close_connection)

    def test_admission_rejection_does_not_read_a_slow_request_body(self):
        handler = self.handler((("Content-Length", "100"),))
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()

        handler._reject(429, 2, "rate limit exceeded")

        handler.rfile.read.assert_not_called()
        self.assertTrue(handler.close_connection)


class RequestExceptionBoundaryTests(unittest.TestCase):
    def test_cancelled_review_is_a_conflict_without_internal_error_details(self):
        handler = object.__new__(ApiHandler)
        handler.headers = http.client.HTTPMessage()
        handler._request_id_value = mock.Mock(return_value="request-id")
        handler._dispatch_with_admission = mock.Mock(
            side_effect=TaskCancelled("password=internal-cancellation-detail")
        )
        handler._send_json = mock.Mock()
        handler._send_internal_error = mock.Mock()

        handler._dispatch("POST", mock.Mock())

        handler._send_json.assert_called_once_with(409, {"error": "review was cancelled"})
        handler._send_internal_error.assert_not_called()


class RequestTargetBoundaryTests(unittest.TestCase):
    def test_rejects_malformed_or_oversized_request_target_structure(self):
        with self.assertRaisesRegex(ClientInputError, "invalid request target"):
            _request_target("http://[invalid")

        query = "&".join("field%d=value" % index for index in range(101))
        with self.assertRaisesRegex(ClientInputError, "too many fields"):
            _query_parameters(query)

    def test_oversized_task_id_never_reaches_the_store(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/tasks/" + "a" * 37
        handler.service = mock.Mock()
        handler._authenticate_or_send = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "reviewer")
        )
        handler._send_json = mock.Mock()

        handler._do_GET()

        handler.service.store.get.assert_not_called()
        handler._send_json.assert_called_once_with(404, {"error": "not found"})


class WorkflowReadBoundaryTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/tasks/" + "a" * 32 + "/workflow"
        handler._console_view = True
        handler.service = mock.Mock()
        handler._authenticate_or_send = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "maintainer")
        )
        handler._send_json = mock.Mock()
        return handler

    def test_workflow_endpoint_uses_authenticated_tenant_and_metadata_query(self):
        handler = self.handler()
        snapshot = {"task_id": "a" * 32, "workflow": None, "steps": []}
        handler.service.store.workflow_status.return_value = snapshot
        handler._do_GET()
        handler._authenticate_or_send.assert_called_once_with("read")
        handler.service.store.workflow_status.assert_called_once_with("a" * 32, "tenant-a")
        handler.service.store.get.assert_not_called()
        handler._send_json.assert_called_once_with(200, snapshot)

    def test_unknown_or_other_tenant_workflow_is_not_found(self):
        handler = self.handler()
        handler.service.store.workflow_status.return_value = None
        handler._do_GET()
        handler._send_json.assert_called_once_with(404, {"error": "task not found"})

    def test_unauthenticated_workflow_never_reaches_store(self):
        handler = self.handler()
        handler._authenticate_or_send.return_value = None
        handler._do_GET()
        handler.service.store.workflow_status.assert_not_called()

    def test_raw_workflow_requires_manage_before_lookup(self):
        handler = self.handler()
        handler._console_view = False
        with self.assertRaises(AccessDeniedError):
            handler._do_GET()
        handler.service.store.workflow_status.assert_not_called()


class RequestJsonBoundaryTests(unittest.TestCase):
    def test_rejects_nonstandard_numeric_constants(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with (
                self.subTest(constant=constant),
                self.assertRaisesRegex(ClientInputError, "valid UTF-8 JSON"),
            ):
                ApiHandler._read_json(f'{{"value":{constant}}}'.encode())

    def test_rejects_ambiguous_or_non_encodable_json(self):
        malformed = (
            b'{"value":1,"value":2}',
            b'{"outer":{"value":1,"value":2}}',
            b'{"value":"\\ud800"}',
            b'{"value":1e999}',
            (('{"value":' * 10_000) + "0" + ("}" * 10_000)).encode(),
        )
        for body in malformed:
            with (
                self.subTest(body=body[:40]),
                self.assertRaisesRegex(ClientInputError, "valid UTF-8 JSON"),
            ):
                ApiHandler._read_json(body)

    def test_rejects_nonstandard_numeric_constants_before_sending_response(self):
        handler = object.__new__(ApiHandler)
        handler._headers = mock.Mock()
        handler.wfile = io.BytesIO()

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                handler._send_json(200, {"value": value})

        handler._headers.assert_not_called()
        self.assertEqual(b"", handler.wfile.getvalue())


class SecurityHeaderBoundaryTests(unittest.TestCase):
    def test_console_view_is_opt_in_and_rejected_before_dispatch_when_invalid(self):
        cases = (
            ([], "/api/tasks", True, False),
            (["console"], "/api/tasks", True, True),
            (["raw"], "/api/tasks", False, True),
            (["console", "console"], "/api/tasks", False, False),
            (["console"], "/v1/proofs", False, True),
        )
        for values, path, allowed, projected in cases:
            with self.subTest(values=values, path=path):
                handler = object.__new__(ApiHandler)
                handler.headers = http.client.HTTPMessage()
                for value in values:
                    handler.headers.add_header("X-EvoAgent-View", value)
                handler.path = path
                handler._console_view = True
                handler._request_id_value = mock.Mock(return_value="request-id")
                handler._dispatch_with_admission = mock.Mock()
                handler._send_json = mock.Mock()
                handler._send_internal_error = mock.Mock()
                action = mock.Mock()

                handler._dispatch("GET", action)

                self.assertEqual(projected, handler._console_view)
                handler._send_internal_error.assert_not_called()
                if allowed:
                    handler._dispatch_with_admission.assert_called_once_with("GET", action)
                    handler._send_json.assert_not_called()
                else:
                    handler._dispatch_with_admission.assert_not_called()
                    self.assertEqual(400, handler._send_json.call_args.args[0])

    def test_duplicate_request_ids_are_replaced(self):
        handler = object.__new__(ApiHandler)
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("X-Request-ID", "first")
        handler.headers.add_header("X-Request-ID", "second")

        request_id = handler._request_id_value()

        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        self.assertNotIn(request_id, {"first", "second"})

    def test_duplicate_authorization_is_rejected_before_authentication(self):
        handler = object.__new__(ApiHandler)
        handler.settings = mock.Mock(auth_required=True)
        handler.service = mock.Mock()
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Authorization", "Bearer first")
        handler.headers.add_header("Authorization", "Bearer second")

        with self.assertRaisesRegex(ClientInputError, "Authorization"):
            handler._principal()

        handler.service.auth.authenticate.assert_not_called()


class ConnectionBoundaryTests(unittest.TestCase):
    def test_accepted_connections_have_bounded_io(self):
        server = EvoAgentHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.addCleanup(server.server_close)
        client = socket.create_connection(server.server_address, timeout=1)
        self.addCleanup(client.close)
        connection, _address = server.get_request()
        self.addCleanup(connection.close)

        self.assertEqual(server.request_io_timeout_seconds, connection.gettimeout())


class ProbeBoundaryTests(unittest.TestCase):
    def test_authenticated_metrics_are_rate_limited_before_authentication(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/metrics"
        handler.settings = mock.Mock(auth_required=True)
        handler.service = mock.Mock()
        handler.service.rate_limiter.check.return_value = (False, 2)
        handler._client_identity_value = mock.Mock(return_value=mock.Mock(address="client"))
        handler._reject = mock.Mock()
        endpoint = mock.Mock()

        handler._dispatch_with_admission("GET", endpoint)

        handler._reject.assert_called_once_with(429, 2, "rate limit exceeded")
        endpoint.assert_not_called()

    def test_health_is_liveness_only(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/health"
        handler.service = mock.Mock()
        handler._send_json = mock.Mock()

        handler._do_GET()

        handler._send_json.assert_called_once_with(200, {"status": "ok"})
        handler.service.assert_not_called()


class GitHubInstallationBoundaryTests(unittest.TestCase):
    def test_setup_callbacks_are_authenticated_by_signed_state_not_bearer_navigation(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/github/setup?state=signed&installation_id=77"
        handler.service = mock.Mock()
        handler.service.authorize_github_installation.return_value = "https://github.com/oauth"
        handler._redirect = mock.Mock()
        handler._authenticate_or_send = mock.Mock()

        handler._do_GET()

        handler.service.authorize_github_installation.assert_called_once_with("signed", "77")
        handler._redirect.assert_called_once_with("https://github.com/oauth")
        handler._authenticate_or_send.assert_not_called()

        handler.path = "/github/oauth/callback?state=oauth-state&code=oauth-code"
        handler._redirect.reset_mock()
        handler._do_GET()
        handler.service.complete_github_installation.assert_called_once_with(
            "oauth-state", "oauth-code"
        )
        handler._redirect.assert_called_once_with("/#github")
        handler._authenticate_or_send.assert_not_called()


class PlatformAuthorizationBoundaryTests(unittest.TestCase):
    def test_skill_inventory_returns_its_atomic_revision(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/api/skills"
        handler.service = mock.Mock()
        handler.service.skill_inventory.return_value = ([{"name": "security"}], "revision-2")
        handler._authenticate_or_send = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "admin")
        )
        handler._send_json = mock.Mock()

        handler._do_GET()

        handler._send_json.assert_called_once_with(
            200,
            {"skills": [{"name": "security"}], "reviewer_revision": "revision-2"},
        )

    def test_tenant_admin_cannot_read_global_evolution_data(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/evolution/runs"
        handler.service = mock.Mock()
        handler._authenticate_or_send = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "admin")
        )
        handler._send_json = mock.Mock()

        handler._do_GET()

        handler._send_json.assert_called_once_with(403, {"error": "permission denied"})
        handler.service.store.list_evolution_runs.assert_not_called()

    def test_tenant_admin_cannot_read_global_process_metrics(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/metrics"
        handler.service = mock.Mock()
        handler._authenticate_or_send = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "admin")
        )
        handler._send_json = mock.Mock()
        handler._send_text = mock.Mock()

        handler._do_GET()

        handler._send_json.assert_called_once_with(403, {"error": "permission denied"})
        handler._send_text.assert_not_called()

    def test_platform_admin_can_read_global_process_metrics(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/metrics"
        handler.service = mock.Mock()
        handler._authenticate_or_send = mock.Mock(
            return_value=Principal("user-1", "platform", "tenant-a", "platform_admin")
        )
        handler._send_json = mock.Mock()
        handler._send_text = mock.Mock()

        handler._do_GET()

        self.assertEqual(200, handler._send_text.call_args.args[0])
        handler._send_json.assert_not_called()


class PasswordChangeBoundaryTests(unittest.TestCase):
    def test_password_change_delegates_audit_to_the_auth_transaction(self):
        body = json.dumps(
            {"current_password": "correct-horse", "new_password": "even-better-password"}
        ).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/auth/password"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000, auth_required=True)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("user-1", "alice", "tenant-a", "admin", 3)
        handler._principal = mock.Mock(return_value=principal)
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.auth.change_password.assert_called_once_with(
            principal, "correct-horse", "even-better-password"
        )
        handler.service.store.audit.assert_not_called()
        handler._send_json.assert_called_once_with(200, {"changed": True, "reauthenticate": True})

    def test_user_creation_delegates_tenant_and_role_policy_to_auth(self):
        body = json.dumps(
            {"username": "bob", "password": "correct-horse", "role": "maintainer"}
        ).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/users"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000, auth_required=True)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("admin-1", "alice", "tenant-a", "admin")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.auth.provision_user.return_value = {"id": "user-2"}
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.auth.provision_user.assert_called_once_with(
            principal, "bob", "correct-horse", "maintainer"
        )
        handler._send_json.assert_called_once_with(201, {"id": "user-2"})

    def test_user_status_requires_platform_identity_and_strict_boolean(self):
        body = json.dumps({"username": "bob", "active": False}).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/users/status"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000, auth_required=True)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("platform-1", "root", "tenant-a", "platform_admin")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.auth.set_user_active.return_value = {"id": "user-2", "active": False}
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler._principal.assert_called_once_with("platform")
        handler.service.auth.set_user_active.assert_called_once_with(principal, "bob", False)
        handler._send_json.assert_called_once_with(200, {"id": "user-2", "active": False})

        handler._read_body = mock.Mock(return_value=b"{}")
        handler._read_json = mock.Mock(return_value={"username": "bob", "active": 0})
        handler._send_json.reset_mock()
        handler._do_POST()
        handler._send_json.assert_called_once_with(400, {"error": "active must be a boolean"})
        self.assertEqual(1, handler.service.auth.set_user_active.call_count)


class ReleaseConfigurationBoundaryTests(unittest.TestCase):
    def test_release_configuration_delegates_audit_to_the_store_transaction(self):
        body = json.dumps({"candidate_version": 2}).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/deployments/llm-review"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("admin-1", "alice", "tenant-a", "admin")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.releases.configure.return_value = {"status": "running"}
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.releases.configure.assert_called_once_with(
            "tenant-a", "llm-review", {"candidate_version": 2}, "alice"
        )
        handler.service.store.audit.assert_not_called()
        handler._send_json.assert_called_once_with(201, {"status": "running"})


class OutboxReplayBoundaryTests(unittest.TestCase):
    def test_replay_delegates_tenant_and_audit_to_the_store_transaction(self):
        body = json.dumps({"message_id": "review:task-1"}).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/outbox/replay"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("admin-1", "alice", "tenant-a", "admin")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.replay_outbox.return_value = True
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.replay_outbox.assert_called_once_with("review:task-1", "tenant-a", "alice")
        handler.service.store.audit.assert_not_called()
        handler._send_json.assert_called_once_with(202, {"replayed": True})


class TaskCancellationBoundaryTests(unittest.TestCase):
    def test_cancel_delegates_actor_and_rejects_parameters(self):
        task_id = "a" * 32
        body = b"{}"
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/tasks/%s/cancel" % task_id
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("reviewer-1", "alice", "tenant-a", "maintainer")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.cancel_task.return_value = {
            "accepted": True,
            "cancel_requested": True,
            "state": "CANCELLED",
        }
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.cancel_task.assert_called_once_with(task_id, "tenant-a", "alice")
        handler.service.store.audit.assert_not_called()
        handler._send_json.assert_called_once_with(
            202,
            {"accepted": True, "cancel_requested": True, "state": "CANCELLED"},
        )

        handler.rfile = io.BytesIO(body)
        handler.service.cancel_task.return_value = {
            "accepted": True,
            "cancel_requested": False,
            "state": "SUCCESS",
        }
        handler._send_json.reset_mock()
        handler._do_POST()
        handler._send_json.assert_called_once_with(
            409,
            {"accepted": True, "cancel_requested": False, "state": "SUCCESS"},
        )

        unexpected = b'{"reason":"override"}'
        handler.headers.replace_header("Content-Length", str(len(unexpected)))
        handler.rfile = io.BytesIO(unexpected)
        handler.service.cancel_task.reset_mock()
        handler._send_json.reset_mock()
        handler._do_POST()
        handler.service.cancel_task.assert_not_called()
        handler._send_json.assert_called_once_with(
            400, {"error": "cancel request does not accept parameters"}
        )


class TaskResumeBoundaryTests(unittest.TestCase):
    def test_resume_delegates_actor_and_rejects_parameters(self):
        task_id = "b" * 32
        body = b"{}"
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/tasks/%s/resume" % task_id
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("reviewer-1", "alice", "tenant-a", "maintainer")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.resume_task.return_value = {"task_id": task_id, "resumed": True}
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.resume_task.assert_called_once_with(task_id, "tenant-a", "alice")
        handler.service.store.audit.assert_not_called()
        handler._send_json.assert_called_once_with(202, {"task_id": task_id, "resumed": True})

        handler.service.resume_task.return_value = {
            "task_id": task_id,
            "resumed": False,
            "already_active": True,
        }
        handler.service.resume_task.reset_mock()
        handler._send_json.reset_mock()
        handler.rfile = io.BytesIO(body)
        handler._do_POST()
        handler._send_json.assert_called_once_with(
            200,
            {"task_id": task_id, "resumed": False, "already_active": True},
        )

        for error, status in (
            (ResourceNotFoundError("task not found"), 404),
            (StateConflictError("cancelled task cannot be resumed"), 409),
        ):
            with self.subTest(status=status):
                handler.service.resume_task.side_effect = error
                handler._send_json.reset_mock()
                handler.rfile = io.BytesIO(body)
                handler._do_POST()
                handler._send_json.assert_called_once_with(status, {"error": str(error)})

        unexpected = b'{"force":true}'
        handler.headers.replace_header("Content-Length", str(len(unexpected)))
        handler.rfile = io.BytesIO(unexpected)
        handler.service.resume_task.side_effect = None
        handler.service.resume_task.reset_mock()
        handler._send_json.reset_mock()
        handler._do_POST()
        handler.service.resume_task.assert_not_called()
        handler._send_json.assert_called_once_with(
            400, {"error": "resume request does not accept parameters"}
        )


class SessionInputBoundaryTests(unittest.TestCase):
    def test_input_delegates_actor_and_rejects_extra_fields(self):
        session_id = "c" * 32
        body = b'{"message":"production"}'
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/sessions/%s/input" % session_id
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("reviewer-1", "alice", "tenant-a", "maintainer")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.provide_session_input.return_value = {
            "session_id": session_id,
            "status": "open",
        }
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.provide_session_input.assert_called_once_with(
            session_id, "production", "tenant-a", "alice"
        )
        handler._send_json.assert_called_once_with(
            200, {"session_id": session_id, "status": "open"}
        )

        unexpected = b'{"message":"production","force":true}'
        handler.headers.replace_header("Content-Length", str(len(unexpected)))
        handler.rfile = io.BytesIO(unexpected)
        handler.service.provide_session_input.reset_mock()
        handler._send_json.reset_mock()
        handler._do_POST()
        handler.service.provide_session_input.assert_not_called()
        handler._send_json.assert_called_once_with(
            400, {"error": "session input requires only message"}
        )


class FixBoundaryTests(unittest.TestCase):
    def test_idempotent_replay_returns_the_existing_result(self):
        task_id = "d" * 32
        body = b"{}"
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/tasks/%s/fix" % task_id
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        handler._principal = mock.Mock(
            return_value=Principal("reviewer-1", "alice", "tenant-a", "maintainer")
        )
        handler._send_json = mock.Mock()

        for replayed, status in ((False, 201), (True, 200)):
            attestation = {
                "execution_mode": "container",
                "container_image": "sha256:image",
                "test_command_sha256": "command-digest",
            }
            result = {
                "branch": "evoagent/fix",
                "commits": [],
                "replayed": replayed,
                "verification": {"passed": True, "attestation": attestation},
            }
            handler.service.create_fix.return_value = result
            handler.rfile = io.BytesIO(body)
            handler._send_json.reset_mock()
            handler.service.create_fix.reset_mock()
            with self.subTest(replayed=replayed):
                handler._do_POST()
                handler.service.create_fix.assert_called_once_with(task_id, "tenant-a", "alice")
                handler.service.store.audit.assert_not_called()
                handler._send_json.assert_called_once_with(status, result)


class FeedbackBoundaryTests(unittest.TestCase):
    def test_feedback_delegates_actor_and_rejects_extra_fields(self):
        task_id = "d" * 32
        body = b'{"category":"accepted","finding":null,"note":"useful"}'
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/tasks/%s/feedback" % task_id
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        principal = Principal("reviewer-1", "alice", "tenant-a", "maintainer")
        handler._principal = mock.Mock(return_value=principal)
        handler.service.record_feedback.return_value = {
            "recorded": True,
            "category": "accepted",
        }
        handler._send_json = mock.Mock()

        handler._do_POST()

        handler.service.record_feedback.assert_called_once_with(
            task_id, "accepted", None, "useful", "tenant-a", "alice"
        )
        handler._send_json.assert_called_once_with(201, {"recorded": True, "category": "accepted"})

        unexpected = b'{"category":"accepted","note":"useful","override":true}'
        handler.headers.replace_header("Content-Length", str(len(unexpected)))
        handler.rfile = io.BytesIO(unexpected)
        handler.service.record_feedback.reset_mock()
        handler._send_json.reset_mock()
        handler._do_POST()
        handler.service.record_feedback.assert_not_called()
        handler._send_json.assert_called_once_with(
            400, {"error": "feedback accepts only category, finding and note"}
        )


class WebhookBoundaryTests(unittest.TestCase):
    _SECRET = "webhook-secret"

    def handler(self, payload, **headers):
        body = json.dumps(payload).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/webhooks/github"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            handler.headers.add_header(name.replace("_", "-"), value)
        handler.settings = mock.Mock(
            max_diff_bytes=10_000,
            github_webhook_secret=self._SECRET,
            github_webhook_previous_secret="",
            webhook_max_age_seconds=600,
        )
        handler.service = mock.Mock()
        handler.service.handle_github_pull_request.return_value = {"accepted": True}
        handler.rfile = io.BytesIO(body)
        handler._send_json = mock.Mock()
        handler.close_connection = False
        return handler, body

    def test_webhook_requires_signature_timestamp_and_delivery_id(self):
        now = datetime.now(UTC).isoformat()
        cases = (
            ({}, {"X_GitHub_Event": "issues"}, 401),
            ({"pull_request": {}}, {"X_GitHub_Event": "pull_request"}, 400),
            ({"pull_request": []}, {"X_GitHub_Event": "pull_request"}, 400),
            (
                {"pull_request": {"updated_at": now}},
                {"X_GitHub_Event": "pull_request"},
                400,
            ),
        )
        for payload, headers, status in cases:
            handler, body = self.handler(payload, **headers)
            if status != 401:
                handler.headers.add_header(
                    "X-Hub-Signature-256",
                    "sha256=" + hmac.new(self._SECRET.encode(), body, hashlib.sha256).hexdigest(),
                )
            with self.subTest(status=status, payload=payload):
                handler._do_POST()
                self.assertEqual(status, handler._send_json.call_args.args[0])
                handler.service.handle_github_pull_request.assert_not_called()

        handler, body = self.handler(
            {"pull_request": {"updated_at": now}},
            X_GitHub_Event="pull_request",
            X_GitHub_Delivery="delivery-1",
        )
        handler.headers.add_header(
            "X-Hub-Signature-256",
            "sha256=" + hmac.new(self._SECRET.encode(), body, hashlib.sha256).hexdigest(),
        )
        handler._do_POST()
        self.assertEqual(202, handler._send_json.call_args.args[0])
        handler.service.handle_github_pull_request.assert_called_once()

    def test_previous_webhook_secret_is_accepted_during_rotation(self):
        now = datetime.now(UTC).isoformat()
        handler, body = self.handler(
            {"pull_request": {"updated_at": now}},
            X_GitHub_Event="pull_request",
            X_GitHub_Delivery="delivery-rotation",
        )
        handler.settings.github_webhook_secret = "new-secret"
        handler.settings.github_webhook_previous_secret = self._SECRET
        handler.headers.add_header(
            "X-Hub-Signature-256",
            "sha256=" + hmac.new(self._SECRET.encode(), body, hashlib.sha256).hexdigest(),
        )

        captured = Metrics()
        with mock.patch("evoagent.api.metrics", captured):
            handler._do_POST()

        self.assertEqual(202, handler._send_json.call_args.args[0])
        handler.service.handle_github_pull_request.assert_called_once()
        self.assertIn(
            "evoagent_github_webhook_previous_secret_verifications_total 1.0",
            captured.prometheus(),
        )

    def test_duplicate_webhook_security_header_is_rejected(self):
        now = datetime.now(UTC).isoformat()
        handler, body = self.handler(
            {"pull_request": {"updated_at": now}},
            X_GitHub_Event="pull_request",
            X_GitHub_Delivery="delivery-duplicate",
        )
        signature = "sha256=" + hmac.new(self._SECRET.encode(), body, hashlib.sha256).hexdigest()
        handler.headers.add_header("X-Hub-Signature-256", signature)
        handler.headers.add_header("X-Hub-Signature-256", signature)

        handler._do_POST()

        self.assertEqual(400, handler._send_json.call_args.args[0])
        handler.service.handle_github_pull_request.assert_not_called()

    def test_old_webhook_replays_only_an_exact_durable_delivery(self):
        payload = {"pull_request": {"updated_at": "2000-01-01T00:00:00Z"}}
        for stored_digest, expected_status in ((None, 409), ("wrong", 409), ("exact", 202)):
            handler, body = self.handler(
                payload,
                X_GitHub_Event="pull_request",
                X_GitHub_Delivery="delayed-delivery",
            )
            signature = hmac.new(self._SECRET.encode(), body, hashlib.sha256).hexdigest()
            handler.headers.add_header("X-Hub-Signature-256", "sha256=" + signature)
            digest = hashlib.sha256(body).hexdigest()
            handler.service.store.get_webhook.return_value = (
                None
                if stored_digest is None
                else {"payload_sha256": digest if stored_digest == "exact" else stored_digest}
            )

            with self.subTest(stored_digest=stored_digest):
                handler._do_POST()
                self.assertEqual(expected_status, handler._send_json.call_args.args[0])
                if expected_status == 202:
                    handler.service.handle_github_pull_request.assert_called_once_with(
                        payload, "delayed-delivery", digest
                    )
                else:
                    handler.service.handle_github_pull_request.assert_not_called()


class ReviewBoundaryTests(unittest.TestCase):
    def test_async_query_has_one_strict_boolean_value(self):
        self.assertFalse(_async_review_requested({}))
        for value, expected in (("true", True), ("TRUE", True), ("false", False)):
            with self.subTest(value=value):
                self.assertEqual(expected, _async_review_requested({"async": [value]}))
        for query in (
            {"async": [""]},
            {"async": ["1"]},
            {"async": ["true", "false"]},
            {"mode": ["async"]},
            {"async": ["true"], "mode": ["fast"]},
        ):
            with self.subTest(query=query), self.assertRaises(ClientInputError):
                _async_review_requested(query)
        with self.assertRaises(ClientInputError):
            _async_review_requested(_query_parameters("async="))

    def test_ambiguous_async_is_rejected_before_admission_classification(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/reviews?async=true&async=false"
        handler.service = mock.Mock()
        endpoint = mock.Mock()

        with self.assertRaisesRegex(ClientInputError, "one true or false"):
            handler._dispatch_with_admission("POST", endpoint)

        endpoint.assert_not_called()
        handler.service.rate_limiter.check.assert_not_called()

    def test_idempotency_key_is_forwarded_only_for_async_reviews(self):
        def handler(path, keys):
            body = json.dumps(
                {"repository": "org/repo", "diff": "patch", "pull_request": 7}
            ).encode()
            instance = object.__new__(ApiHandler)
            instance.path = path
            instance.headers = http.client.HTTPMessage()
            instance.headers.add_header("Content-Length", str(len(body)))
            for key in keys:
                instance.headers.add_header("Idempotency-Key", key)
            instance.settings = mock.Mock(max_diff_bytes=10_000)
            instance.service = mock.Mock()
            instance.service.enqueue_review.return_value = {
                "task_id": "task-1",
                "state": "PENDING",
                "replayed": False,
            }
            instance._principal = mock.Mock(
                return_value=Principal("user-1", "alice", "tenant-a", "admin")
            )
            instance.rfile = io.BytesIO(body)
            instance._send_json = mock.Mock()
            instance.close_connection = False
            return instance

        accepted = handler("/v1/reviews?async=true", ["retry-1"])
        accepted._do_POST()
        accepted.service.enqueue_review.assert_called_once_with(
            "org/repo",
            "patch",
            7,
            tenant_id="tenant-a",
            idempotency_key="retry-1",
            actor="alice",
            review_context=None,
        )
        self.assertEqual(202, accepted._send_json.call_args.args[0])
        accepted.service.store.audit.assert_not_called()

        replayed = handler("/v1/reviews?async=true", ["retry-1"])
        replayed.service.enqueue_review.return_value["replayed"] = True
        replayed._do_POST()
        self.assertEqual(200, replayed._send_json.call_args.args[0])

        synchronous = handler("/v1/reviews", [])
        synchronous.service.create_review.return_value = {
            "task_id": "task-2",
            "state": "SUCCESS",
        }
        synchronous._do_POST()
        synchronous.service.create_review.assert_called_once_with(
            "org/repo",
            "patch",
            7,
            tenant_id="tenant-a",
            actor="alice",
            review_context=None,
        )
        synchronous.service.store.audit.assert_not_called()

        for path, keys in (
            ("/v1/reviews", ["retry-1"]),
            ("/v1/reviews?async=true", ["bad key"]),
            ("/v1/reviews?async=true", ["first", "second"]),
        ):
            rejected = handler(path, keys)
            with self.subTest(path=path, keys=keys):
                rejected._do_POST()
                self.assertEqual(400, rejected._send_json.call_args.args[0])
                rejected.service.enqueue_review.assert_not_called()
                rejected.service.create_review.assert_not_called()

    def test_review_context_is_forwarded_without_becoming_request_metadata(self):
        context = {
            "title": "Bounded context",
            "spec": "Keep both review axes separate.",
            "standards": "Every finding needs evidence.",
        }
        body = json.dumps({"repository": "org/repo", "diff": "patch", "context": context}).encode()
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/reviews"
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler.service.create_review.return_value = {"task_id": "task-1", "state": "SUCCESS"}
        handler._principal = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "admin")
        )
        handler.rfile = io.BytesIO(body)
        handler._send_json = mock.Mock()
        handler.close_connection = False

        handler._do_POST()

        handler.service.create_review.assert_called_once_with(
            "org/repo",
            "patch",
            None,
            tenant_id="tenant-a",
            actor="alice",
            review_context=context,
        )
        self.assertEqual(201, handler._send_json.call_args.args[0])

    def test_review_json_contract_rejects_invalid_fields(self):
        malformed = (
            {"repository": {}, "diff": "patch"},
            {"repository": "org/repo", "diff": []},
            {"repository": "org/repo", "diff": "patch", "pull_request": True},
            {"repository": "org/repo", "diff": "patch", "pull_request": 2**31},
            {"repository": "org/repo", "diff": "patch", "priority": "high"},
        )
        for payload in malformed:
            body = json.dumps(payload).encode()
            handler = object.__new__(ApiHandler)
            handler.path = "/v1/reviews"
            handler.headers = http.client.HTTPMessage()
            handler.headers.add_header("Content-Length", str(len(body)))
            handler.settings = mock.Mock(max_diff_bytes=10_000)
            handler.service = mock.Mock()
            handler._principal = mock.Mock(
                return_value=Principal("user-1", "alice", "tenant-a", "admin")
            )
            handler.rfile = io.BytesIO(body)
            handler._send_json = mock.Mock()
            handler.close_connection = False

            with self.subTest(payload=payload):
                handler._do_POST()
                self.assertEqual(400, handler._send_json.call_args.args[0])
                handler.service.create_review.assert_not_called()
                handler.service.enqueue_review.assert_not_called()


class JsonScalarBoundaryTests(unittest.TestCase):
    @staticmethod
    def handler(path, payload):
        body = json.dumps(payload).encode()
        handler = object.__new__(ApiHandler)
        handler.path = path
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.settings = mock.Mock(max_diff_bytes=10_000)
        handler.service = mock.Mock()
        handler._principal = mock.Mock(
            return_value=Principal("user-1", "alice", "tenant-a", "platform_admin")
        )
        handler.rfile = io.BytesIO(body)
        handler._send_json = mock.Mock()
        handler.close_connection = False
        return handler

    def test_malformed_json_fields_never_reach_use_cases(self):
        cases = (
            (
                "/v1/repository-policies",
                {"repository": {}, "policy": {}},
                "set_repository_policy",
            ),
            (
                "/v1/proofs",
                {"original": {}, "patched": {}, "reproduction_command": []},
                "run_proof",
            ),
            (
                "/v1/evolution/propose",
                {"skill_name": "review", "prompt": "prompt", "regression_score": "0.5"},
                "propose",
            ),
            (
                "/v1/tasks/00000000-0000-0000-0000-000000000000/feedback",
                {"category": "accepted", "finding": []},
                "record_feedback",
            ),
        )
        for path, payload, method in cases:
            handler = self.handler(path, payload)
            target = (
                getattr(handler.service.evolution, method)
                if method == "propose"
                else getattr(handler.service, method)
            )

            with self.subTest(path=path):
                handler._do_POST()
                self.assertEqual(400, handler._send_json.call_args.args[0])
                target.assert_not_called()

    def test_repository_policy_expected_version_is_strict_and_forwarded(self):
        for expected_version in (True, -1, "1"):
            handler = self.handler(
                "/v1/repository-policies",
                {"repository": "org/repo", "policy": {}, "expected_version": expected_version},
            )
            with self.subTest(expected_version=expected_version):
                handler._do_POST()
                self.assertEqual(400, handler._send_json.call_args.args[0])
                handler.service.set_repository_policy.assert_not_called()

        handler = self.handler(
            "/v1/repository-policies",
            {"repository": "org/repo", "policy": {}, "expected_version": 7},
        )
        handler.service.set_repository_policy.return_value = {"version": 8}
        handler._do_POST()
        self.assertEqual(201, handler._send_json.call_args.args[0])
        handler.service.set_repository_policy.assert_called_once_with(
            "tenant-a", "org/repo", {}, "alice", 7
        )

        unknown = self.handler(
            "/v1/repository-policies",
            {"repository": "org/repo", "policy": {}, "metadata": "private"},
        )
        unknown._do_POST()
        self.assertEqual(400, unknown._send_json.call_args.args[0])
        unknown.service.set_repository_policy.assert_not_called()

    def test_evolution_approval_cannot_reload_or_use_the_removed_activate_endpoint(self):
        handler = self.handler(
            "/v1/evolution/propose",
            {
                "skill_name": "llm-review",
                "prompt": "Review the diff as JSON with severity, fix and test guidance.",
            },
        )
        handler.service.evolution.propose.return_value = {"decision": "approved"}

        handler._do_POST()

        self.assertEqual(201, handler._send_json.call_args.args[0])
        handler.service.reload_skills.assert_not_called()

        removed = self.handler(
            "/v1/skills/llm-review/versions/1/activate",
            {},
        )
        removed._do_POST()
        self.assertEqual(404, removed._send_json.call_args.args[0])

    def test_runtime_skill_reload_endpoint_is_not_exposed(self):
        handler = self.handler("/v1/skills/reload", {})

        handler._do_POST()

        handler.service.store.audit.assert_not_called()
        handler.service.reload_skills.assert_not_called()
        self.assertEqual(404, handler._send_json.call_args.args[0])


class HeavyAdmissionBoundaryTests(unittest.TestCase):
    def test_draining_rejects_new_work_before_other_admission_checks(self):
        handler = object.__new__(ApiHandler)
        handler.path = "/v1/reviews?async=true"
        handler.service = mock.Mock()
        handler._reject = mock.Mock()
        endpoint = mock.Mock()
        DRAINING.set()
        self.addCleanup(DRAINING.clear)

        handler._dispatch_with_admission("POST", endpoint)

        handler._reject.assert_called_once_with(503, 1.0, "service is draining")
        handler.service.rate_limiter.check.assert_not_called()
        endpoint.assert_not_called()

    def test_expensive_sync_paths_shed_before_running(self):
        for path in (
            "/v1/tasks/00000000-0000-0000-0000-000000000000/fix",
            "/v1/evolution/auto",
            "/v1/evolution/propose",
        ):
            handler = object.__new__(ApiHandler)
            handler.path = path
            handler.service = mock.Mock()
            handler.service.rate_limiter.check.return_value = (True, 0)
            handler.service.heavy_gate.try_acquire.return_value = False
            handler._client_identity_value = mock.Mock(return_value=mock.Mock(address="client"))
            handler._reject = mock.Mock()
            endpoint = mock.Mock()

            with self.subTest(path=path):
                handler._dispatch_with_admission("POST", endpoint)
                handler._reject.assert_called_once_with(
                    503, 1.0, "server is at capacity, retry shortly"
                )
                self.assertEqual("heavy", handler._metric_class)
                endpoint.assert_not_called()


class AdmissionControlTests(unittest.TestCase):
    def setUp(self):
        self.database_url = postgres_url(self)
        reset_postgres(self.database_url)

    def _serve(self, settings: Settings):
        service = ReviewService(settings)
        self.service = service
        handler = type(
            "ConfiguredApiHandler",
            (ApiHandler,),
            {"service": service, "settings": settings},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(service.close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def _settings(self, **overrides) -> Settings:
        base = dict(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=10000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
            auth_required=False,
            redis_url="",
            async_workers=1,
            database_url=self.database_url,
        )
        base.update(overrides)
        return Settings(**base)

    # A non-probe endpoint (probes are intentionally exempt from rate limiting).
    _TASK = "/v1/tasks/00000000-0000-0000-0000-000000000000"

    def test_rate_limit_returns_429_with_retry_after(self):
        host, port = self._serve(self._settings(rate_limit_rps=1, rate_limit_burst=1))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", self._TASK)
        first = conn.getresponse()
        first.read()
        self.assertEqual(404, first.status)  # allowed through, task simply missing

        conn.request("GET", self._TASK)
        second = conn.getresponse()
        second.read()
        self.assertEqual(429, second.status)
        self.assertIsNotNone(second.getheader("Retry-After"))

    def test_tenant_review_capacity_returns_retryable_429_after_body_is_consumed(self):
        host, port = self._serve(
            self._settings(
                tenant_max_active_reviews=1,
                tenant_capacity_retry_seconds=7,
            )
        )
        self.service.store.create_review_task(
            "occupied",
            "org/occupied",
            1,
            {"source": "test"},
            "default",
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        payload = json.dumps(
            {
                "repository": "org/rejected",
                "diff": "--- a/a.py\n+++ b/a.py\n+value = 1\n",
                "pull_request": 2,
            }
        )

        conn.request(
            "POST",
            "/v1/reviews?async=true",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = json.loads(response.read())

        self.assertEqual(429, response.status)
        self.assertEqual("7", response.getheader("Retry-After"))
        self.assertEqual("tenant review capacity is exhausted", body["error"])
        self.assertEqual(1, self.service.store.tenant_review_admission_stats("default")["active"])
        audit = self.service.store.list_audit("default", 10)
        self.assertEqual("review.capacity-rejected", audit[0]["action"])

        conn.request("GET", "/api/tenant-review-capacity")
        capacity_response = conn.getresponse()
        capacity = json.loads(capacity_response.read())
        self.assertEqual(200, capacity_response.status)
        self.assertEqual(1, capacity["active_reviews"])
        self.assertTrue(capacity["saturated"])

    def test_trusted_proxy_clients_receive_independent_rate_limit_buckets(self):
        host, port = self._serve(
            self._settings(
                rate_limit_rps=1,
                rate_limit_burst=1,
                trusted_proxy_cidrs=("127.0.0.1/32",),
            )
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        statuses = []
        for client in ("198.51.100.10", "198.51.100.11", "198.51.100.10"):
            conn.request("GET", self._TASK, headers={"X-Forwarded-For": client})
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)

        self.assertEqual([404, 404, 429], statuses)

    def test_untrusted_socket_peer_cannot_rotate_forwarded_rate_limit_keys(self):
        host, port = self._serve(
            self._settings(
                rate_limit_rps=1,
                rate_limit_burst=1,
                trusted_proxy_cidrs=("10.0.0.0/8",),
            )
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        statuses = []
        for client in ("198.51.100.10", "198.51.100.11"):
            conn.request("GET", self._TASK, headers={"X-Forwarded-For": client})
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)

        self.assertEqual([404, 429], statuses)

    def test_access_log_records_resolved_client_without_untrusted_left_prefix(self):
        host, port = self._serve(self._settings(trusted_proxy_cidrs=("127.0.0.1/32", "10.0.0.0/8")))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        output = io.StringIO()

        with redirect_stdout(output):
            conn.request(
                "GET",
                "/health",
                headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.20, 10.0.0.7"},
            )
            response = conn.getresponse()
            response.read()

        logs = output.getvalue()
        self.assertEqual(200, response.status)
        self.assertIn('"client": "198.51.100.20"', logs)
        self.assertIn('"peer": "127.0.0.1"', logs)
        self.assertIn('"client_source": "forwarded"', logs)
        self.assertIn('"forwarded_hops": 2', logs)
        self.assertNotIn("203.0.113.99", logs)

    def test_invalid_forwarded_chain_falls_back_to_peer_and_is_counted(self):
        host, port = self._serve(
            self._settings(
                rate_limit_rps=1,
                rate_limit_burst=1,
                trusted_proxy_cidrs=("127.0.0.1/32",),
            )
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        captured = Metrics()

        with mock.patch("evoagent.api.metrics", captured):
            statuses = []
            for invalid in ("proxy.internal", "198.51.100.8:443"):
                conn.request("GET", self._TASK, headers={"X-Forwarded-For": invalid})
                response = conn.getresponse()
                response.read()
                statuses.append(response.status)

        self.assertEqual([404, 429], statuses)
        self.assertIn(
            "evoagent_http_forwarded_invalid_total 2.0",
            captured.prometheus(),
        )

    def test_probes_are_exempt_from_rate_limiting(self):
        host, port = self._serve(self._settings(rate_limit_rps=1, rate_limit_burst=1))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(10):
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read()
            self.assertEqual(200, response.status)

    def test_health_is_minimal_and_readiness_reports_dependencies(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/health")
        health_response = conn.getresponse()
        health = json.loads(health_response.read())
        self.assertEqual({"status": "ok"}, health)

        conn.request("GET", "/ready")
        ready_response = conn.getresponse()
        ready = json.loads(ready_response.read())
        self.assertEqual(200, ready_response.status)
        self.assertEqual(CURRENT_SCHEMA_VERSION, ready["checks"]["schema_version"])

    def test_request_identity_and_security_headers_cover_every_response(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/health", headers={"X-Request-ID": "gateway-request.42"})
        accepted = conn.getresponse()
        accepted.read()
        self.assertEqual("gateway-request.42", accepted.getheader("X-Request-ID"))
        self.assertEqual("nosniff", accepted.getheader("X-Content-Type-Options"))
        self.assertEqual("DENY", accepted.getheader("X-Frame-Options"))
        self.assertIn("default-src 'self'", accepted.getheader("Content-Security-Policy", ""))
        self.assertIn("object-src 'none'", accepted.getheader("Content-Security-Policy", ""))
        self.assertEqual("same-origin", accepted.getheader("Referrer-Policy"))
        self.assertIn("camera=()", accepted.getheader("Permissions-Policy", ""))
        self.assertEqual("no-store", accepted.getheader("Cache-Control"))
        self.assertNotIn("Python", accepted.getheader("Server", ""))

        conn.request("GET", "/health", headers={"X-Request-ID": "invalid request id"})
        replaced = conn.getresponse()
        replaced.read()
        generated = replaced.getheader("X-Request-ID", "")
        self.assertNotEqual("invalid request id", generated)
        self.assertRegex(generated, re.compile(r"^[0-9a-f]{32}$"))

    def test_internal_error_is_correlated_without_leaking_exception_or_query(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        def fail_dashboard(*_args):
            raise RuntimeError("password=provider-secret-value")

        self.service.store.dashboard_stats = fail_dashboard
        output = io.StringIO()
        with redirect_stdout(output):
            conn.request(
                "GET",
                "/api/dashboard?access_token=query-secret-value",
                headers={"X-Request-ID": "edge-error-17"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())

        logs = output.getvalue()
        self.assertEqual(500, response.status)
        self.assertEqual("edge-error-17", response.getheader("X-Request-ID"))
        self.assertEqual({"error": "internal server error", "request_id": "edge-error-17"}, payload)
        self.assertNotIn("provider-secret-value", json.dumps(payload))
        self.assertNotIn("provider-secret-value", logs)
        self.assertNotIn("query-secret-value", logs)
        self.assertIn('"event": "http_internal_error"', logs)
        self.assertIn('"error_type": "builtins.RuntimeError"', logs)
        self.assertIn('"path": "/api/dashboard"', logs)

    def test_post_internal_error_uses_the_same_safe_boundary(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        def fail_policy_update(*_args):
            # Built-in ValueError is not assumed to be public-safe. Only the
            # explicit ClientInputError marker may cross the HTTP boundary.
            raise ValueError("postgresql://admin:database-secret@db/reviews")

        self.service.set_repository_policy = fail_policy_update
        body = json.dumps(
            {
                "repository": "org/repo",
                "policy": {"max_diff_bytes": 1024},
            }
        )
        output = io.StringIO()
        with redirect_stdout(output):
            conn.request(
                "POST",
                "/v1/repository-policies",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Request-ID": "post-error-18",
                },
            )
            response = conn.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(500, response.status)
        self.assertEqual({"error": "internal server error", "request_id": "post-error-18"}, payload)
        self.assertNotIn("database-secret", output.getvalue())

    def test_list_limit_is_bounded_and_returns_a_reviewed_client_error(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/api/tasks?limit=1001")
        response = conn.getresponse()
        payload = json.loads(response.read())

        self.assertEqual(400, response.status)
        self.assertEqual({"error": "limit must be an integer between 1 and 1000"}, payload)

    def test_console_errors_are_public_codes_on_reads_writes_and_admission(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"X-EvoAgent-View": "console", "Content-Type": "application/json"}

        def request(method, path, body=None, console=True):
            conn.request(method, path, body=body, headers=headers if console else {})
            response = conn.getresponse()
            payload = json.loads(response.read())
            self.assertEqual("X-EvoAgent-View", response.getheader("Vary"))
            self.assertEqual("no-store", response.getheader("Cache-Control"))
            self.assertTrue(response.getheader("X-Request-ID"))
            return response, payload

        private = 'private-prompt={"credential":"must-not-display"}'
        with mock.patch.object(
            self.service.store, "dashboard_stats", side_effect=ClientInputError(private)
        ):
            response, payload = request("GET", "/api/dashboard")
            self.assertEqual(400, response.status)
            self.assertEqual({"error_code": "invalid_request"}, payload)
            self.assertEqual({"error": private}, request("GET", "/api/dashboard", console=False)[1])
        with mock.patch.object(self.service.studio, "save", side_effect=ClientInputError(private)):
            response, payload = request("POST", "/v1/studio/agents", "{}")
            self.assertEqual(400, response.status)
            self.assertEqual({"error_code": "invalid_request"}, payload)

        for error, status, code in (
            (StateConflictError("published workflow digest mismatch"), 409, "invalid_version"),
            (StateConflictError("draft changed; save and retry"), 409, "draft_conflict"),
            (RuntimeError(private), 500, "internal_error"),
        ):
            with mock.patch.object(self.service.store, "dashboard_stats", side_effect=error):
                response, payload = request("GET", "/api/dashboard")
                self.assertEqual(status, response.status)
                self.assertEqual({"error_code": code}, payload)

        response, payload = request(
            "POST",
            "/v1/studio/validate",
            json.dumps({"definition": {"name": "Empty", "steps": [], "outputs": {}}}),
        )
        self.assertEqual(400, response.status)
        self.assertEqual({"error_code": "workflow_steps"}, payload)
        response, payload = request("POST", "/v1/users", "{}")
        self.assertEqual(400, response.status)
        self.assertEqual({"error_code": "unsupported_view"}, payload)

        with mock.patch.object(self.service.rate_limiter, "check", return_value=(False, 7)):
            response, payload = request("GET", "/api/dashboard")
            self.assertEqual(429, response.status)
            self.assertEqual("7", response.getheader("Retry-After"))
            self.assertEqual({"error_code": "rate_limited"}, payload)
        DRAINING.set()
        try:
            response, payload = request("GET", "/api/dashboard")
            self.assertEqual(503, response.status)
            self.assertEqual("1", response.getheader("Retry-After"))
            self.assertEqual({"error_code": "unavailable"}, payload)
        finally:
            DRAINING.clear()

    def test_repository_policy_api_versions_and_reads_tenant_policy(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        body = json.dumps(
            {
                "repository": "org/repo",
                "expected_version": 0,
                "policy": {
                    "auto_fix": True,
                    "allowed_fix_rules": ["SEC-YAML-LOAD"],
                    "max_diff_bytes": 4096,
                },
            }
        )
        conn.request(
            "POST",
            "/v1/repository-policies",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        created_response = conn.getresponse()
        created = json.loads(created_response.read())
        self.assertEqual(201, created_response.status)
        self.assertEqual(1, created["version"])

        conn.request("GET", "/v1/repository-policies?repository=org%2Frepo")
        fetched_response = conn.getresponse()
        fetched = json.loads(fetched_response.read())
        self.assertEqual(200, fetched_response.status)
        self.assertEqual("configured", fetched["source"])
        self.assertEqual(4096, fetched["policy"]["max_diff_bytes"])
        self.assertEqual([1], [item["version"] for item in fetched["history"]])

        stale = json.dumps(
            {
                "repository": "org/repo",
                "expected_version": 0,
                "policy": {"enabled": False},
            }
        )
        conn.request(
            "POST",
            "/v1/repository-policies",
            body=stale,
            headers={"Content-Type": "application/json", "X-EvoAgent-View": "console"},
        )
        conflict_response = conn.getresponse()
        conflict = json.loads(conflict_response.read())
        self.assertEqual(409, conflict_response.status)
        self.assertEqual({"error_code": "policy_conflict"}, conflict)

        conn.request(
            "GET",
            "/v1/repository-policies?repository=org%2Frepo",
            headers={"X-EvoAgent-View": "console"},
        )
        projected_response = conn.getresponse()
        projected = json.loads(projected_response.read())
        self.assertEqual(200, projected_response.status)
        self.assertEqual(1, projected["version"])
        self.assertNotIn("tenant_id", projected)
        self.assertNotIn("policy", projected["history"][0])
        self.assertIn("available_reviewers", projected)
        self.assertIn("available_fix_rules", projected)

    def test_rejected_requests_are_counted_in_metrics(self):
        host, port = self._serve(self._settings(rate_limit_rps=1, rate_limit_burst=1))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(3):
            conn.request("GET", self._TASK)
            conn.getresponse().read()

        conn.request("GET", "/metrics")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        self.assertIn("evoagent_http_rejected_total", body)
        self.assertIn("evoagent_http_in_flight", body)
        self.assertIn("evoagent_http_requests_total", body)
        self.assertIn("evoagent_http_read_responses_4xx_total", body)
        self.assertIn("evoagent_http_request_read_count", body)

    def test_sync_review_and_login_shed_but_async_exempt_when_heavy_gate_full(self):
        host, port = self._serve(self._settings(max_inflight_heavy=1))
        # Occupy the only heavy slot so the gate is full for the next request.
        self.assertTrue(self.service.heavy_gate.try_acquire())
        self.addCleanup(self.service.heavy_gate.release)

        payload = (
            '{"repository": "org/repo", "diff": "--- a/a\\n+++ b/a\\n@@ -1 +1 @@\\n-x\\n+y\\n"}'
        )
        headers = {"Content-Type": "application/json"}

        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", "/v1/reviews", body=payload, headers=headers)
        sync = conn.getresponse()
        sync.read()
        self.assertEqual(503, sync.status)  # synchronous heavy work is shed
        self.assertIsNotNone(sync.getheader("Retry-After"))

        for path in ("/v1/auth/login", "/v1/auth/password", "/v1/users"):
            conn.request("POST", path, body="{}", headers=headers)
            auth_response = conn.getresponse()
            auth_response.read()
            self.assertEqual(503, auth_response.status)  # PBKDF2 shares the same CPU bound

        conn.request("POST", "/v1/reviews?async=true", body=payload, headers=headers)
        async_response = conn.getresponse()
        async_response.read()
        self.assertNotEqual(503, async_response.status)  # async intake is exempt

        conn.request("POST", "/v1/reviews?async=yes", body=payload, headers=headers)
        ambiguous = conn.getresponse()
        ambiguous.read()
        self.assertEqual(400, ambiguous.status)  # invalid mode is rejected before admission

    def test_async_review_idempotency_reuses_task_and_rejects_changed_content(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "client-request-1",
        }
        payload = json.dumps(
            {
                "repository": "org/repo",
                "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
                "pull_request": 7,
                "context": {"title": "First", "spec": "Return new", "standards": ""},
            }
        )

        responses = []
        for _ in range(2):
            conn.request("POST", "/v1/reviews?async=true", body=payload, headers=headers)
            response = conn.getresponse()
            responses.append((response.status, json.loads(response.read())))

        self.assertEqual([202, 200], [item[0] for item in responses])
        self.assertEqual(responses[0][1]["task_id"], responses[1][1]["task_id"])
        changed = payload.replace("+new", "+different")
        conn.request("POST", "/v1/reviews?async=true", body=changed, headers=headers)
        conflict = conn.getresponse()
        conflict_body = json.loads(conflict.read())
        self.assertEqual(400, conflict.status)
        self.assertIn("different review", conflict_body["error"])
        changed_context = json.loads(payload)
        changed_context["context"]["spec"] = "Return something else"
        conn.request(
            "POST",
            "/v1/reviews?async=true",
            body=json.dumps(changed_context),
            headers=headers,
        )
        context_conflict = conn.getresponse()
        self.assertEqual(400, context_conflict.status)
        self.assertIn("different review", json.loads(context_conflict.read())["error"])
        self.assertEqual(1, len(self.service.store.list_tasks(10, "default")))

    def test_async_review_retry_recovers_a_committed_task_after_response_loss(self):
        host, port = self._serve(self._settings())
        headers = {
            "Content-Type": "application/json",
            "X-EvoAgent-View": "console",
            "Idempotency-Key": "lost-review-ack",
        }
        payload = json.dumps(
            {
                "repository": "org/repo",
                "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
            }
        )
        original_send = ApiHandler._send_json
        committed = []

        def lose_ack(handler, status, value):
            if (
                handler.command == "POST"
                and handler.path == "/v1/reviews?async=true"
                and status == 202
            ):
                committed.append(value["task_id"])
                handler.close_connection = True
                return  # Simulate disconnect after task/outbox/audit commit, before any response.
            original_send(handler, status, value)

        with mock.patch.object(ApiHandler, "_send_json", lose_ack):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.request("POST", "/v1/reviews?async=true", body=payload, headers=headers)
                with self.assertRaises(http.client.RemoteDisconnected):
                    conn.getresponse()
            finally:
                conn.close()
        self.assertEqual(1, len(committed))
        retry = http.client.HTTPConnection(host, port, timeout=5)
        try:
            retry.request("POST", "/v1/reviews?async=true", body=payload, headers=headers)
            response = retry.getresponse()
            recovered = json.loads(response.read())
        finally:
            retry.close()
        self.assertEqual(200, response.status)
        self.assertEqual(committed[0], recovered["task_id"])
        self.assertTrue(recovered["replayed"])
        self.assertEqual({"task_id", "state", "replayed"}, recovered.keys())
        self.assertEqual(1, len(self.service.store.list_tasks(10, "default")))
        with self.service.store._connect() as conn:
            outbox = conn.execute(
                "SELECT count(*) AS count FROM outbox_messages WHERE message_key=%s",
                (committed[0],),
            ).fetchone()
            audits = conn.execute(
                "SELECT count(*) AS count FROM audit_log WHERE action='review.create' AND resource=%s",
                ("org/repo",),
            ).fetchone()
        self.assertEqual(1, outbox["count"])
        self.assertEqual(
            2, audits["count"]
        )  # Both HTTP attempts remain audited; only one task is created.

    def test_disabled_rate_limit_allows_burst(self):
        host, port = self._serve(self._settings())  # rate_limit_rps defaults to 0
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(20):
            conn.request("GET", self._TASK)
            response = conn.getresponse()
            response.read()
            self.assertEqual(404, response.status)


if __name__ == "__main__":
    unittest.main()
