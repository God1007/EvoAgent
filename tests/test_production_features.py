import hashlib
import hmac
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from evoagent.auth import AuthManager, Principal, _b64, hash_password, verify_password
from evoagent.config import Settings, _bool
from evoagent.errors import AccessDeniedError, ClientInputError
from evoagent.evolution import EvolutionEngine, RegressionEvaluator
from evoagent.harness import ReviewHarness
from evoagent.metrics import Metrics
from evoagent.reviewer import LocalRuleReviewer
from evoagent.rollout import ReleaseManager
from evoagent.service import ReviewService
from evoagent.task_queue import TaskQueue
from evoagent.time_utils import utc_now
from evoagent.verifier import RepairVerifier
from tests.db_support import postgres_store, postgres_url

DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"


class DeploymentScriptTests(unittest.TestCase):
    def test_up_refuses_to_replace_a_running_service(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            docker = Path(temporary) / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  info|"compose version") exit 0 ;;\n'
                '  "compose ps -q evoagent") echo running-container; exit 0 ;;\n'
                "  *) exit 99 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            result = subprocess.run(
                ["bash", str(root / "scripts" / "deploy.sh"), "up"],
                cwd=root,
                env={**os.environ, "PATH": temporary + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("Refusing to replace a running EvoAgent", result.stderr)


class ConfigurationBoundaryTests(unittest.TestCase):
    def test_boolean_environment_values_reject_typos(self):
        for value, expected in ((" YES ", True), ("off", False)):
            with mock.patch.dict("os.environ", {"TEST_BOOLEAN": value}):
                self.assertIs(expected, _bool("TEST_BOOLEAN"))
        with (
            mock.patch.dict("os.environ", {"EVOAGENT_SKILL_REQUIRE_CONTAINER": "ture"}),
            self.assertRaisesRegex(ValueError, "EVOAGENT_SKILL_REQUIRE_CONTAINER"),
        ):
            Settings.from_env()

    def test_shutdown_grace_rejects_negative_or_non_finite_values(self):
        for value in ("-1", "nan", "inf"):
            with (
                self.subTest(value=value),
                mock.patch.dict("os.environ", {"EVOAGENT_SHUTDOWN_GRACE_SECONDS": value}),
                self.assertRaisesRegex(ValueError, "EVOAGENT_SHUTDOWN_GRACE_SECONDS"),
            ):
                Settings.from_env()

    def test_auth_previous_secret_requires_current_full_length_secret(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        for changes in (
            {"auth_previous_secret": "p" * 32},
            {"auth_secret": "c" * 32, "auth_previous_secret": "short"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "AUTH_PREVIOUS_SECRET"),
            ):
                configured.__class__(**{**configured.__dict__, **changes}).validate_evolution()

    def test_webhook_previous_secret_requires_a_current_secret(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
            github_webhook_previous_secret="old-secret",
        )

        with self.assertRaisesRegex(ValueError, "GITHUB_WEBHOOK_SECRET"):
            configured.validate_evolution()

    def test_default_tenant_id_is_a_bounded_stable_identifier(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        for tenant_id in ("", " ", " tenant", "tenant ", "t" * 201):
            with (
                self.subTest(tenant_id=tenant_id),
                self.assertRaisesRegex(ValueError, "EVOAGENT_DEFAULT_TENANT_ID"),
            ):
                configured.__class__(
                    **{**configured.__dict__, "default_tenant_id": tenant_id}
                ).validate_evolution()

        configured.__class__(
            **{**configured.__dict__, "default_tenant_id": "tenant-a"}
        ).validate_evolution()

    def test_runtime_resource_limits_must_be_finite_and_positive(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        for name, value in (
            ("port", True),
            ("port", 0),
            ("port", 65_536),
            ("max_diff_bytes", 0),
            ("max_steps", 0),
            ("timeout_seconds", 0),
            ("llm_max_input_tokens", 0),
            ("llm_max_output_tokens", 0),
            ("skill_timeout_seconds", 0),
            ("skill_memory_mb", 0),
            ("repair_verify_timeout_seconds", 0),
            ("repair_memory_mb", 0),
            ("repair_pids_limit", 0),
            ("repair_max_output_bytes", 0),
            ("outbox_poll_seconds", float("inf")),
            ("outbox_batch_size", 0),
            ("outbox_lease_seconds", 0),
            ("outbox_max_attempts", 0),
            ("pg_statement_timeout_seconds", 0),
            ("repair_cpus", float("nan")),
            ("repair_cpus", 0.0),
        ):
            with (
                self.subTest(name=name, value=value),
                self.assertRaisesRegex(ValueError, "EVOAGENT_"),
            ):
                configured.__class__(**{**configured.__dict__, name: value}).validate_evolution()

        for host in ("", " localhost", "localhost ", "bad\nhost"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "EVOAGENT_HOST"),
            ):
                configured.__class__(**{**configured.__dict__, "host": host}).validate_evolution()

        with self.assertRaisesRegex(ValueError, "EVOAGENT_HISTORY_RETENTION_DAYS"):
            configured.__class__(
                **{
                    **configured.__dict__,
                    "history_retention_days": 1,
                    "webhook_max_age_seconds": 86_400,
                }
            ).validate_evolution()

    def test_async_worker_pool_is_bounded(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        for workers in (0, 257):
            with (
                self.subTest(workers=workers),
                self.assertRaisesRegex(ValueError, "EVOAGENT_ASYNC_WORKERS"),
            ):
                configured.__class__(
                    **{**configured.__dict__, "async_workers": workers}
                ).validate_evolution()

    def test_queue_retry_and_lease_values_are_positive(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        for name in ("queue_max_attempts", "queue_lease_seconds"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "EVOAGENT_QUEUE_"):
                configured.__class__(**{**configured.__dict__, name: 0}).validate_evolution()

    def test_security_windows_and_alert_sample_floor_are_positive(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        for name in ("session_ttl_seconds", "webhook_max_age_seconds", "alert_min_samples"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "EVOAGENT_"):
                configured.__class__(**{**configured.__dict__, name: 0}).validate_evolution()

    def test_effect_lease_cannot_expire_during_bounded_provider_calls(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
            effect_lease_seconds=299,
        )

        with self.assertRaisesRegex(ValueError, "EVOAGENT_EFFECT_LEASE_SECONDS"):
            configured.validate_evolution()

    def test_repository_evidence_archive_limit_is_positive_and_bounded(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )

        for value in (0, 1024 * 1024 * 1024 + 1):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "EVOAGENT_REPOSITORY_EVIDENCE"),
            ):
                configured.__class__(
                    **{**configured.__dict__, "repository_evidence_max_bytes": value}
                ).validate_evolution()

    def test_github_writes_require_complete_runtime_credentials(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        invalid = (
            {"github_app_id": "123"},
            {"github_private_key_path": "/run/secrets/github.pem"},
            {"github_app_id": " ", "github_private_key_path": "/run/secrets/github.pem"},
            {"auto_post_review": True},
            {"auto_post_review": True, "github_token": " "},
        )
        for changes in invalid:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "GITHUB|GitHub"),
            ):
                configured.__class__(**{**configured.__dict__, **changes}).validate_evolution()

        for credentials in (
            {"github_token": "token"},
            {
                "github_app_id": "123",
                "github_private_key_path": "/run/secrets/github.pem",
            },
        ):
            configured.__class__(
                **{**configured.__dict__, **credentials, "auto_post_review": True}
            ).validate_evolution()

        production = {
            **configured.__dict__,
            "host": "0.0.0.0",
            "auth_required": True,
            "auth_secret": "s" * 32,
            "redis_url": "redis://queue:6379/0",
            "rate_limit_rps": 1,
            "max_inflight_heavy": 1,
            "tenant_max_active_reviews": 1,
            "skill_require_container": True,
            "github_token": "token",
            "auto_post_review": True,
        }
        with self.assertRaisesRegex(ValueError, "installation OAuth"):
            configured.__class__(**production).validate_evolution()

        with self.assertRaisesRegex(ValueError, "tenant-bound"):
            configured.__class__(
                **{
                    **production,
                    "auto_post_review": False,
                    "github_token": "",
                    "github_webhook_secret": "w" * 32,
                }
            ).validate_evolution()

        app_production = {
            **production,
            "github_token": "",
            "github_app_id": "123",
            "github_app_slug": "evoagent",
            "github_private_key_path": "/run/secrets/github.pem",
            "github_client_id": "Iv1.client",
            "github_client_secret": "secret",
            "github_oauth_callback_url": "https://review.example/github/oauth/callback",
        }
        for changes in (
            {},
            {"github_webhook_secret": "short"},
            {
                "github_webhook_secret": "w" * 32,
                "github_webhook_previous_secret": "short",
            },
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "GITHUB_WEBHOOK"),
            ):
                configured.__class__(**{**app_production, **changes}).validate_evolution()
        configured.__class__(
            **{**app_production, "github_webhook_secret": "w" * 32}
        ).validate_evolution()

    def test_github_installation_oauth_requires_complete_secure_configuration(self):
        configured = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        complete = {
            "github_app_id": "123",
            "github_app_slug": "evoagent",
            "github_private_key_path": "/run/secrets/github.pem",
            "github_client_id": "Iv1.client",
            "github_client_secret": "secret",
            "github_oauth_callback_url": "https://review.example/github/oauth/callback",
            "auth_required": True,
            "auth_secret": "s" * 32,
        }
        configured.__class__(**{**configured.__dict__, **complete}).validate_evolution()

        invalid = (
            {"github_app_slug": "evoagent"},
            {**complete, "auth_required": False},
            {**complete, "github_oauth_callback_url": "http://review.example/callback"},
            {**complete, "github_oauth_callback_url": "https://user@review.example/callback"},
        )
        for changes in invalid:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "GitHub|GITHUB"),
            ):
                configured.__class__(**{**configured.__dict__, **changes}).validate_evolution()


class AuthenticationStateTests(unittest.TestCase):
    def test_auth_manager_rejects_weak_signing_keys_before_store_access(self):
        for secret, previous in (("short", ""), ("", "p" * 32), ("c" * 32, "short")):
            store = mock.Mock()
            with self.subTest(secret=secret, previous=previous), self.assertRaises(ValueError):
                AuthManager(store, secret, previous_secret=previous)
            self.assertEqual([], store.mock_calls)

    def test_signed_tokens_require_a_fixed_header_and_unambiguous_payload(self):
        store = mock.Mock()
        store.get_user.return_value = {
            "id": "user-1",
            "username": "alice",
            "active": True,
            "memberships": [{"tenant_id": "tenant-b", "role": "admin"}],
        }
        secret = b"s" * 32
        auth = AuthManager(store, secret.decode())
        payload = (
            b'{"sub":"user-1","username":"alice","tenant":"tenant-b",'
            b'"role":"admin","credential_version":0,"exp":4102444800}'
        )

        def signed(raw_header, raw_payload):
            header, body = _b64(raw_header), _b64(raw_payload)
            signing_input = (header + "." + body).encode("ascii")
            return (
                signing_input.decode()
                + "."
                + _b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
            )

        header = b'{"alg":"HS256","typ":"JWT"}'
        cases = (
            (b'{"alg":"none","typ":"JWT"}', payload),
            (
                header,
                payload.replace(b'"tenant":"tenant-b"', b'"tenant":"tenant-a","tenant":"tenant-b"'),
            ),
            (
                header,
                payload.replace(b'"exp":4102444800', b'"exp":"4102444800"'),
            ),
            (
                header,
                payload.replace(b'"exp":4102444800', b'"exp":true'),
            ),
        )
        for raw_header, raw_payload in cases:
            with (
                self.subTest(raw_header=raw_header, raw_payload=raw_payload),
                self.assertRaisesRegex(AccessDeniedError, "invalid token"),
            ):
                auth.authenticate("Bearer " + signed(raw_header, raw_payload))

        expires_now = payload.replace(b'"exp":4102444800', b'"exp":100')
        with (
            mock.patch("evoagent.auth.time.time", return_value=100),
            self.assertRaisesRegex(AccessDeniedError, "expired"),
        ):
            auth.authenticate("Bearer " + signed(header, expires_now))

    def test_non_loopback_listener_requires_durable_bounded_admission(self):
        settings = Settings(
            host="0.0.0.0",
            port=8080,
            max_diff_bytes=1000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )

        with self.assertRaisesRegex(ValueError, "AUTH_REQUIRED"):
            settings.validate_evolution()
        settings.__class__(**{**settings.__dict__, "host": "127.0.0.2"}).validate_evolution()

        external = {
            **settings.__dict__,
            "auth_required": True,
            "auth_secret": "s" * 32,
        }
        with self.assertRaisesRegex(ValueError, "REDIS_URL"):
            settings.__class__(**external).validate_evolution()
        external["redis_url"] = "redis://queue:6379/0"
        limits = {
            "rate_limit_rps": "RATE_LIMIT_RPS",
            "max_inflight_heavy": "MAX_INFLIGHT_HEAVY",
            "max_http_connections": "MAX_HTTP_CONNECTIONS",
            "tenant_max_active_reviews": "TENANT_MAX_ACTIVE_REVIEWS",
        }
        for field, error in limits.items():
            configured = {**external, **dict.fromkeys(limits, 1), field: 0}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                settings.__class__(**configured).validate_evolution()
        production = {**external, **dict.fromkeys(limits, 1)}
        with self.assertRaisesRegex(ValueError, "SKILL_REQUIRE_CONTAINER"):
            settings.__class__(**production).validate_evolution()
        settings.__class__(**{**production, "skill_require_container": True}).validate_evolution()

    def test_unknown_user_still_runs_password_verification(self):
        store = mock.Mock()
        store.get_user.return_value = None
        auth = AuthManager(store, "s" * 32)

        with (
            mock.patch("evoagent.auth.hashlib.pbkdf2_hmac", return_value=b"x" * 32) as derive,
            self.assertRaisesRegex(AccessDeniedError, "invalid username or password"),
        ):
            auth.login("missing", "wrong-password")

        derive.assert_called_once()
        self.assertEqual(310_000, derive.call_args.args[3])

    def test_password_hash_rejects_unbounded_work_before_derivation(self):
        encoded = hash_password("correct-horse")
        for invalid in (
            encoded.replace("$310000$", "$999999999$"),
            encoded.replace("$310000$", "$1$"),
            "x" * 257,
        ):
            with (
                self.subTest(invalid=invalid[:40]),
                mock.patch("evoagent.auth.hashlib.pbkdf2_hmac") as derive,
            ):
                self.assertFalse(verify_password("correct-horse", invalid))
                derive.assert_not_called()

    def test_password_change_revokes_tokens_and_signed_states(self):
        user = {
            "id": "user-1",
            "username": "alice",
            "password_hash": hash_password("correct-horse"),
            "credential_version": 0,
            "active": True,
            "memberships": [{"tenant_id": "tenant-a", "role": "admin"}],
        }

        class Store:
            @staticmethod
            def get_user(_username):
                return user

            @staticmethod
            def change_user_password(user_id, expected_hash, password_hash, actor, tenant_id):
                if user_id != user["id"] or expected_hash != user["password_hash"]:
                    return False
                assert (actor, tenant_id) == ("alice", "tenant-a")
                user["password_hash"] = password_hash
                user["credential_version"] += 1
                return True

        auth = AuthManager(Store(), "s" * 32)
        token = str(auth.login("alice", "correct-horse")["access_token"])
        principal = auth.authenticate("Bearer " + token)
        state = auth.issue_state(principal, "github-install")

        auth.change_password(principal, "correct-horse", "even-better-password")

        with self.assertRaisesRegex(AccessDeniedError, "credentials are no longer current"):
            auth.authenticate("Bearer " + token)
        with self.assertRaisesRegex(AccessDeniedError, "credentials are no longer current"):
            auth.authenticate_state(state, "github-install")
        with self.assertRaisesRegex(AccessDeniedError, "invalid username or password"):
            auth.login("alice", "correct-horse")
        self.assertEqual(
            1,
            auth.authenticate(
                "Bearer " + str(auth.login("alice", "even-better-password")["access_token"])
            ).credential_version,
        )

    def test_tenant_admin_can_provision_members_but_not_platform_admins(self):
        store = mock.Mock()
        store.create_user.return_value = True
        auth = AuthManager(store, "s" * 32)
        principal = Principal("admin-1", "alice", "tenant-a", "admin")

        created = auth.provision_user(principal, "bob@example.com", "correct-horse", "maintainer")

        self.assertEqual("tenant-a", created["tenant_id"])
        self.assertEqual("maintainer", created["role"])
        args = store.create_user.call_args.args
        self.assertEqual(
            ("bob@example.com", "tenant-a", "maintainer", "alice"), args[1:2] + args[3:]
        )
        self.assertTrue(verify_password("correct-horse", args[2]))
        with self.assertRaisesRegex(AccessDeniedError, "platform administrator"):
            auth.provision_user(principal, "root", "another-password", "platform_admin")
        self.assertEqual(1, store.create_user.call_count)
        store.create_user.return_value = False
        with self.assertRaisesRegex(ClientInputError, "username already exists"):
            auth.provision_user(principal, "taken", "another-password", "auditor")

    def test_only_platform_admin_can_change_global_user_status(self):
        user = {
            "id": "user-2",
            "username": "bob",
            "password_hash": hash_password("correct-horse"),
            "credential_version": 0,
            "active": True,
            "memberships": [{"tenant_id": "tenant-a", "role": "maintainer"}],
        }

        class Store:
            @staticmethod
            def get_user(_username):
                return user

            @staticmethod
            def set_user_active(user_id, active, _actor, _audit_tenant_id):
                if user_id != user["id"]:
                    return False
                user["active"] = active
                user["credential_version"] += 1
                return True

        auth = AuthManager(Store(), "s" * 32)
        old_token = "Bearer " + str(auth.login("bob", "correct-horse")["access_token"])
        tenant_admin = Principal("admin-1", "alice", "tenant-a", "admin")
        platform_admin = Principal("platform-1", "root", "tenant-a", "platform_admin")

        with self.assertRaisesRegex(AccessDeniedError, "permission denied"):
            auth.set_user_active(tenant_admin, "bob", False)
        with self.assertRaisesRegex(ClientInputError, "own account"):
            auth.set_user_active(
                Principal("user-2", "bob", "tenant-a", "platform_admin"), "bob", False
            )
        self.assertFalse(auth.set_user_active(platform_admin, "bob", False)["active"])
        with self.assertRaisesRegex(AccessDeniedError, "no longer active"):
            auth.authenticate(old_token)
        self.assertTrue(auth.set_user_active(platform_admin, "bob", True)["active"])
        with self.assertRaisesRegex(AccessDeniedError, "credentials are no longer current"):
            auth.authenticate(old_token)
        self.assertEqual(
            "bob",
            auth.authenticate(
                "Bearer " + str(auth.login("bob", "correct-horse")["access_token"])
            ).username,
        )

    def test_existing_token_uses_current_user_and_membership_state(self):
        user = {
            "id": "user-1",
            "username": "alice",
            "password_hash": hash_password("correct-horse"),
            "active": True,
            "memberships": [{"tenant_id": "tenant-a", "role": "admin"}],
        }

        class Store:
            @staticmethod
            def get_user(_username):
                return user

        auth = AuthManager(Store(), "a" * 32)
        token = "Bearer " + auth.login("alice", "correct-horse")["access_token"]

        self.assertFalse(auth.authenticate(token).can("platform"))
        user["memberships"][0]["role"] = "auditor"
        self.assertFalse(auth.authenticate(token).can("manage"))
        user["active"] = False
        with self.assertRaisesRegex(AccessDeniedError, "no longer active"):
            auth.authenticate(token)

    def test_auth_rotation_accepts_old_tokens_but_signs_new_tokens_with_current_secret(self):
        user = {
            "id": "user-1",
            "username": "alice",
            "password_hash": hash_password("correct-horse"),
            "active": True,
            "memberships": [{"tenant_id": "tenant-a", "role": "admin"}],
        }

        class Store:
            @staticmethod
            def get_user(_username):
                return user

        old = AuthManager(Store(), "o" * 32)
        old_token = old.login("alice", "correct-horse")["access_token"]
        rotated = AuthManager(Store(), "n" * 32, previous_secret="o" * 32)

        captured = Metrics()
        with mock.patch("evoagent.auth.metrics", captured):
            self.assertEqual("alice", rotated.authenticate("Bearer " + str(old_token)).username)
        self.assertIn(
            "evoagent_auth_previous_secret_verifications_total 1.0",
            captured.prometheus(),
        )
        new_token = rotated.login("alice", "correct-horse")["access_token"]
        with self.assertRaisesRegex(AccessDeniedError, "invalid token"):
            old.authenticate("Bearer " + str(new_token))


class RolloutConfigurationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.store = mock.Mock()
        self.versions = {
            1: {"version": 1, "qualification": "legacy"},
            2: {"version": 2, "qualification": "approved"},
        }
        self.store.get_skill_version.side_effect = lambda _skill_name, version: self.versions.get(
            version
        )
        self.store.save_deployment.return_value = {"status": "running", "candidate_version": 2}
        self.store.get_deployment.return_value = {"status": "running", "candidate_version": 2}
        self.revision = "a" * 64
        self.store.get_skill_evaluation_revision.return_value = self.revision
        self.release = ReleaseManager(self.store, self.revision)

    def test_configuration_is_normalized_before_persistence(self):
        self.release.configure(
            "tenant",
            "skill",
            {"stable_version": 1, "candidate_version": 2, "shadow_percent": 25},
        )

        self.store.save_deployment.assert_called_once_with(
            "tenant",
            "skill",
            {
                "stable_version": 1,
                "candidate_version": 2,
                "canary_percent": 0,
                "shadow_percent": 25,
                "max_error_rate": 0.1,
                "min_samples": 20,
                "max_disagreement_rate": 0.2,
                "auto_promote": False,
                "status": "running",
            },
            "system",
        )
        self.assertEqual(
            [mock.call("skill", 2), mock.call("skill", 1)],
            self.store.get_skill_version.call_args_list,
        )
        self.store.get_deployment.assert_not_called()

    def test_candidate_qualification_must_match_the_current_execution_revision(self):
        for qualified_revision in ("", "b" * 64):
            self.store.get_skill_evaluation_revision.return_value = qualified_revision
            with (
                self.subTest(qualified_revision=qualified_revision),
                self.assertRaisesRegex(ClientInputError, "re-evaluated"),
            ):
                self.release.configure("tenant", "skill", {"candidate_version": 2})

        self.store.save_deployment.assert_not_called()

    def test_running_stale_candidate_is_removed_from_new_assignments(self):
        self.store.get_skill_evaluation_revision.return_value = "b" * 64
        self.store.get_deployment.return_value = {
            "status": "running",
            "stable_version": 1,
            "candidate_version": 2,
            "generation": 7,
        }
        captured = Metrics()

        with mock.patch("evoagent.rollout.metrics", captured):
            assignment = self.release.assignment("tenant", "skill", "task")

        self.assertEqual("stable", assignment["lane"])
        self.assertFalse(assignment["shadow"])
        self.assertEqual(1, assignment["deployment"]["stable_version"])
        self.assertIsNone(assignment["deployment"]["candidate_version"])
        self.assertIsNone(assignment["deployment"]["generation"])
        self.assertIn("evoagent_release_revision_mismatch_total 1.0", captured.prometheus())

    def test_invalid_configuration_never_reaches_the_store(self):
        invalid = (
            {"candidate_version": 2, "secret": "must-not-enter-audit"},
            {"candidate_version": 99},
            {"candidate_version": 2, "max_error_rate": "0.1"},
            {"candidate_version": 2, "min_samples": True},
            {"candidate_version": 2, "auto_promote": True},
            {"stable_version": 2, "candidate_version": 2},
        )
        for config in invalid:
            self.store.save_deployment.reset_mock()
            with self.subTest(config=config), self.assertRaises(ClientInputError):
                self.release.configure("tenant", "skill", config)
            self.store.save_deployment.assert_not_called()

    def test_boolean_release_identity_never_reaches_observation_storage(self):
        self.assertIsNone(
            self.release.observe(
                "tenant",
                "skill",
                "task",
                True,
                "canary",
                candidate_version=True,
                generation=1,
            )
        )
        self.assertIsNone(
            self.release.observe_shadow(
                "tenant",
                "skill",
                "task",
                "canary",
                {},
                {},
                candidate_version=True,
                generation=1,
            )
        )

        self.store.record_deployment_result.assert_not_called()
        self.store.record_shadow_observation.assert_not_called()

    def test_alert_failure_does_not_reclassify_a_durable_shadow_promotion(self):
        self.store.record_shadow_observation.return_value = {"status": "promoted"}
        self.store.create_alert.side_effect = RuntimeError("alert unavailable")
        captured = Metrics()

        with mock.patch("evoagent.rollout.metrics", captured):
            result = self.release.observe_shadow(
                "tenant",
                "skill",
                "task",
                "stable",
                {},
                {},
                candidate_version=2,
                generation=3,
                audit_event=("shadow.completed", {}),
            )

        self.assertEqual("promoted", result["status"])
        self.assertIn("evoagent_release_alert_failures_total 1.0", captured.prometheus())
        self.assertEqual(
            ("shadow.completed", {}),
            self.store.record_shadow_observation.call_args.kwargs["audit_event"],
        )

    def test_unapproved_candidate_never_reaches_the_store(self):
        self.versions[2]["qualification"] = "rejected"

        with self.assertRaisesRegex(ClientInputError, "not approved"):
            self.release.configure("tenant", "skill", {"candidate_version": 2})

        self.store.save_deployment.assert_not_called()

    def test_rejected_version_cannot_be_used_as_the_stable_lane(self):
        self.versions[1]["qualification"] = "rejected"

        with self.assertRaisesRegex(ClientInputError, "stable_version is not eligible"):
            self.release.configure("tenant", "skill", {"stable_version": 1, "candidate_version": 2})

        self.store.save_deployment.assert_not_called()


class EvolutionQualificationBoundaryTests(unittest.TestCase):
    def test_engine_rejects_inconsistent_limits_before_store_access(self):
        invalid = (
            {"min_cases": 0},
            {"max_cases": 501},
            {"min_cases": 3, "max_cases": 2},
            {"max_cases": 2, "min_holdout_cases": 3},
            {"min_improvement": float("nan")},
            {"max_metric_regression": -0.1},
            {"seed_defaults": "yes"},
            {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")},
            {"execution_revision": "not-a-digest"},
        )
        for options in invalid:
            store = mock.Mock()
            with self.subTest(options=options), self.assertRaises(ValueError):
                EvolutionEngine(store, **options)
            self.assertEqual([], store.mock_calls)

    def test_stale_duplicate_prompt_can_be_requalified_as_a_new_version(self):
        store = mock.Mock()
        prompt = "Review the diff as JSON with severity, fix and test guidance."
        store.get_active_skill_version.return_value = None
        store.get_skill_version_by_prompt.return_value = {
            "skill_name": "llm-review",
            "version": 1,
            "score": 0.8,
            "active": False,
            "qualification": "approved",
        }
        store.get_skill_evaluation_revision.return_value = "b" * 64
        store.list_evaluation_cases.side_effect = [[{"name": "case"}], []]
        store.save_skill_version.return_value = {
            "skill_name": "llm-review",
            "version": 2,
            "score": 1.0,
            "active": False,
            "qualification": "approved",
        }
        engine = EvolutionEngine(
            store,
            reviewer_factory=mock.Mock(),
            min_cases=1,
            seed_defaults=False,
            execution_revision="a" * 64,
        )
        baseline = engine._empty_metrics(1)
        candidate = {**baseline, "score": 1.0, "successful_cases": 1, "success_rate": 1.0}

        with mock.patch(
            "evoagent.evolution.RegressionEvaluator.run", side_effect=[baseline, candidate]
        ):
            result = engine.propose("llm-review", prompt)

        self.assertEqual("approved", result["decision"])
        self.assertEqual(2, result["version"]["version"])

    def test_oversized_prompt_is_rejected_before_store_access(self):
        store = mock.Mock()
        engine = EvolutionEngine(store, seed_defaults=False)

        with self.assertRaisesRegex(ClientInputError, "at most 12000 characters"):
            engine.propose("llm-review", "x" * 12_001)

        self.assertEqual([], store.mock_calls)

    def test_replay_stops_calling_reviewers_after_the_shared_deadline(self):
        reviewer = mock.Mock()
        reviewer.name = "bounded-reviewer"
        reviewer.review.return_value = []
        cases = [
            {
                "name": name,
                "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n",
                "expected": [],
            }
            for name in ("within-budget", "after-deadline")
        ]

        with mock.patch("evoagent.evolution.time.monotonic", side_effect=(9.0, 10.0)):
            result = RegressionEvaluator(lambda _prompt: reviewer).run(
                "prompt", cases, deadline=10.0
            )

        reviewer.review.assert_called_once()
        self.assertEqual(1, result["successful_cases"])
        self.assertEqual(1, len(result["errors"]))

    def test_evaluation_labels_reject_ambiguous_or_unknown_fields(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        invalid = (
            {"path": "a.py", "line": True},
            {"path": "a.py", "line": 1.0},
            {"path": 1, "line": 1},
            {"path": "a.py", "line": 1, "min_severity": 1},
            {"path": "a.py", "line": 1, "rule_id": " SEC-EVAL"},
            {"path": "a.py", "line": 1, "note": "unscored metadata"},
        )
        for finding in invalid:
            with self.subTest(finding=finding), self.assertRaises(ClientInputError):
                EvolutionEngine.validate_case("case", diff, [finding])

    def test_passing_evaluation_saves_an_inactive_approved_candidate(self):
        store = mock.Mock()
        store.get_active_skill_version.return_value = None
        store.get_skill_version_by_prompt.return_value = None
        store.list_evaluation_cases.side_effect = [[{"name": "case"}], []]
        store.save_skill_version.return_value = {
            "skill_name": "llm-review",
            "version": 1,
            "score": 1.0,
            "active": False,
            "qualification": "approved",
        }
        engine = EvolutionEngine(
            store,
            reviewer_factory=mock.Mock(),
            min_cases=1,
            seed_defaults=False,
            execution_revision="a" * 64,
        )
        baseline = engine._empty_metrics(1)
        candidate = {**baseline, "score": 1.0, "successful_cases": 1, "success_rate": 1.0}
        prompt = "Review the diff as JSON with severity, fix and test guidance."

        with (
            mock.patch(
                "evoagent.evolution.RegressionEvaluator.run", side_effect=[baseline, candidate]
            ) as evaluate,
            mock.patch("evoagent.evolution.time.monotonic", return_value=100.0),
        ):
            result = engine.propose("llm-review", prompt)

        self.assertEqual("approved", result["decision"])
        self.assertFalse(result["version"]["active"])
        store.save_skill_version.assert_called_once_with(
            "llm-review", prompt, 1.0, qualification="approved"
        )
        store.get_skill_version_by_prompt.assert_called_once_with("llm-review", prompt)
        self.assertEqual(
            {220.0},
            {call.kwargs["deadline"] for call in evaluate.call_args_list},
        )
        reproducibility = store.save_evolution_run.call_args.args[0]["metrics"]["reproducibility"]
        self.assertEqual(3, reproducibility["evaluation_schema_version"])
        self.assertEqual("a" * 64, reproducibility["execution_revision"])


class ProductionFeatureTests(unittest.TestCase):
    def setUp(self):
        self.store = postgres_store(self)
        self.database_url = postgres_url(self)

    def test_login_rbac_and_tenant_task_isolation(self):
        auth = AuthManager(
            self.store,
            "a" * 32,
            bootstrap_username="alice",
            bootstrap_password="correct-horse",
            default_tenant_id="tenant-a",
        )
        token = auth.login("alice", "correct-horse")["access_token"]
        principal = auth.authenticate("Bearer " + token)
        self.assertTrue(principal.can("manage"))
        self.assertTrue(principal.can("platform"))
        self.store.create("a", "org/a", 1, {}, "tenant-a")
        self.store.create("b", "org/b", 2, {}, "tenant-b")
        self.assertIsNotNone(self.store.get("a", principal.tenant_id))
        self.assertIsNone(self.store.get("b", principal.tenant_id))
        self.assertEqual(["a"], [item["id"] for item in self.store.list_tasks(10, "tenant-a")])

    def test_webhook_delivery_is_idempotent_and_payload_bound(self):
        self.assertTrue(self.store.claim_webhook("delivery-1", "t", "pull_request", "aaa"))
        self.assertFalse(self.store.claim_webhook("delivery-1", "t", "pull_request", "aaa"))
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.store.claim_webhook("delivery-1", "t", "pull_request", "bbb")

    def test_failure_cases_are_filtered_by_tenant(self):
        self.store.create("a", "org/a", 1, {}, "tenant-a")
        self.store.create("b", "org/b", 2, {}, "tenant-b")
        self.store.record_failure_case("a", "false_positive", {"note": "a"})
        self.store.record_failure_case("b", "missed_issue", {"note": "b"})

        cases = self.store.list_failure_cases(tenant_id="tenant-a")

        self.assertEqual(["a"], [item["task_id"] for item in cases])

    def test_failed_graph_resumes_after_last_completed_checkpoint(self):
        secret = "provider-token=temporary-secret"

        class BrokenReviewer:
            name = "broken"

            def review(self, _diff, _parsed):
                raise RuntimeError(secret)

        self.store.create("task", "org/repo", 1, {})
        with self.assertRaises(RuntimeError):
            ReviewHarness(self.store, BrokenReviewer(), node_retries=0).run(
                "task", "org/repo", 1, DIFF
            )
        checkpoints = self.store.load_checkpoints("task")
        self.assertEqual("completed", checkpoints["planning"]["status"])
        self.assertEqual("failed", checkpoints["executing"]["status"])
        failed_task = self.store.get("task")
        failure_cases = self.store.list_failure_cases()
        persisted = str({"task": failed_task, "checkpoints": checkpoints, "cases": failure_cases})
        self.assertNotIn(secret, persisted)
        self.assertRegex(
            failed_task["error"],
            r"^review execution failed \[type=builtins\.RuntimeError; ref=[0-9a-f]{16}\]$",
        )
        self.assertRegex(
            checkpoints["executing"]["error"],
            r"^review node failed \[type=builtins\.RuntimeError; ref=[0-9a-f]{16}\]$",
        )

        report = ReviewHarness(self.store, LocalRuleReviewer(), node_retries=0).run(
            "task", "org/repo", 1, DIFF
        )
        self.assertEqual("high", report.risk)
        planning_events = [
            item for item in self.store.get("task")["trace"] if item["state"] == "PLANNING"
        ]
        self.assertEqual(1, len(planning_events))

    def test_queue_moves_terminal_failure_to_dlq(self):
        secret = "queue-password=terminal-secret"

        def broken(_payload):
            raise RuntimeError(secret)

        queue = TaskQueue(broken, workers=1, max_attempts=1)
        queue.submit({"task_id": "dead"})
        for _ in range(100):
            if queue.dead_letters():
                break
            time.sleep(0.01)
        letters = queue.dead_letters()
        queue.close()
        self.assertEqual("dead", letters[0]["message_id"])
        self.assertRegex(
            letters[0]["error"],
            r"^task delivery failed \[type=builtins\.RuntimeError; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn(secret, str(letters))

    def test_dead_letter_marks_pending_task_failed(self):
        self.store.create_review_task("dead", "org/repo", 1, {}, "tenant", "diff")
        service = ReviewService(
            Settings(
                host="127.0.0.1",
                port=8080,
                max_diff_bytes=10_000,
                max_steps=8,
                timeout_seconds=10,
                llm_base_url="",
                llm_api_key="",
                llm_model="",
                github_webhook_secret="",
                github_token="",
                auto_post_review=False,
                database_url=self.database_url,
            )
        )
        self.addCleanup(service.close)

        secret = "redis-url=terminal-secret"
        service._on_dead_letter(
            {"task_id": "dead", "tenant_id": "tenant", "admission_generation": 1}, secret
        )

        task = self.store.get("dead", "tenant")
        self.assertEqual("FAILED", task["state"])
        self.assertRegex(
            task["error"],
            r"^task delivery failed \[type=unknown; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn(secret, str(task))

    def test_canary_assignment_and_error_budget_rollback(self):
        release = ReleaseManager(self.store)
        self.store.save_skill_version("skill", "stable", 0.1, qualification="legacy")
        self.store.save_skill_version("skill", "candidate", 0.2, qualification="approved")
        release.configure(
            "tenant",
            "skill",
            {
                "stable_version": 1,
                "candidate_version": 2,
                "canary_percent": 100,
                "shadow_percent": 100,
                "min_samples": 2,
                "max_error_rate": 0.25,
            },
        )
        assignment = release.assignment("tenant", "skill", "task")
        self.assertEqual("canary", assignment["lane"])
        generation = assignment["deployment"]["generation"]
        self.store.create("canary-failed", "acme/widgets", 17, {}, "tenant")
        self.store.create("canary-passed", "acme/widgets", 18, {}, "tenant")
        release.observe(
            "tenant",
            "skill",
            "canary-failed",
            True,
            candidate_version=2,
            generation=generation,
        )
        result = release.observe(
            "tenant",
            "skill",
            "canary-passed",
            False,
            candidate_version=2,
            generation=generation,
        )
        self.assertEqual("rolled_back", result["status"])
        self.assertEqual(0, result["canary_percent"])
        self.assertTrue(self.store.list_alerts("tenant"))
        audit = next(
            item
            for item in self.store.list_audit("tenant", 100)
            if item["action"] == "deployment.auto-rollback"
        )
        self.assertEqual(generation, audit["detail"]["generation"])

    def test_qualification_revision_is_read_from_the_approved_evolution_run(self):
        version = self.store.save_skill_version(
            "qualified-skill", "candidate", 0.2, qualification="approved"
        )
        revision = "a" * 64
        self.store.save_evolution_run(
            {
                "id": "qualification-revision-run",
                "skill_name": "qualified-skill",
                "candidate_version": version["version"],
                "baseline_version": None,
                "decision": "approved",
                "candidate_score": 0.2,
                "baseline_score": 0.1,
                "metrics": {"reproducibility": {"execution_revision": revision}},
                "created_at": utc_now(),
            }
        )

        self.assertEqual(
            revision,
            self.store.get_skill_evaluation_revision("qualified-skill", version["version"]),
        )
        self.assertEqual("", self.store.get_skill_evaluation_revision("missing", 1))

    def test_rollout_rejects_invalid_percentage_types(self):
        release = ReleaseManager(self.store)
        for value in (True, "10", "not-a-number", [], {}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "canary_percent must be an integer"),
            ):
                release.configure(
                    "tenant",
                    "skill",
                    {
                        "candidate_version": 2,
                        "canary_percent": value,
                        "shadow_percent": 0,
                    },
                )

    def test_repair_verifier_blocks_invalid_python(self):
        result = RepairVerifier().verify_contents({"app.py": "def broken(:\n"})
        self.assertFalse(result["passed"])
        self.assertEqual("compile:app.py", result["checks"][0]["name"])


if __name__ == "__main__":
    unittest.main()
