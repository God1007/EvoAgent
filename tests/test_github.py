import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from unittest import mock

from evoagent.circuit_breaker import CircuitBreaker, CircuitOpenError
from evoagent.github import (
    GITHUB_INSTALLATION_ID_MAX,
    GitHubAppAuthenticator,
    GitHubClient,
    GitHubInstallationOAuthClient,
    _RestrictedRedirectHandler,
    _validate_github_url,
    validate_commit_sha,
    validate_pull_request_urls,
    verify_signature,
)


class GitHubSignatureTests(unittest.TestCase):
    def test_signature_verification(self):
        body = b'{"ok":true}'
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("secret", body, signature))
        self.assertFalse(verify_signature("wrong", body, signature))


class GitHubUrlAllowlistTests(unittest.TestCase):
    def test_allows_expected_github_hosts(self):
        for url in (
            "https://api.github.com/repos/o/r/pulls/1",
            "https://github.com/o/r/pull/1.diff",
            "https://codeload.github.com/o/r/zip/main",
        ):
            self.assertTrue(_validate_github_url(url, GitHubClient("t").allowed_hosts))

    def test_rejects_non_https(self):
        with self.assertRaises(ValueError):
            _validate_github_url("http://api.github.com/x", GitHubClient("t").allowed_hosts)

    def test_rejects_unexpected_host(self):
        with self.assertRaises(ValueError):
            _validate_github_url("https://attacker.example/x", GitHubClient("t").allowed_hosts)

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError):
            _validate_github_url("https://u:p@api.github.com/x", GitHubClient("t").allowed_hosts)

    def test_rejects_unexpected_port(self):
        with self.assertRaises(ValueError):
            _validate_github_url("https://api.github.com:8443/x", GitHubClient("t").allowed_hosts)

    def test_request_refuses_ssrf_target_before_network(self):
        client = GitHubClient("secret-token")
        with self.assertRaises(ValueError):
            client.fetch_diff("https://attacker.example/steal")

    def test_upsert_comment_validates_external_api_url(self):
        client = GitHubClient("secret-token")
        with self.assertRaises(ValueError):
            client.upsert_comment("https://attacker.example/issues/1", "body", "<!-- m -->")

    def test_pull_request_urls_are_bound_to_repository_and_number(self):
        validate_pull_request_urls(
            "o/r",
            7,
            "https://github.com/O/R/pull/7.diff",
            "https://api.github.com/repos/o/r/issues/7",
        )
        for diff_url, issue_url in (
            (
                "https://github.com/o/other/pull/7.diff",
                "https://api.github.com/repos/o/r/issues/7",
            ),
            (
                "https://github.com/o/r/pull/8.diff",
                "https://api.github.com/repos/o/r/issues/7",
            ),
            (
                "https://github.com/o/r/pull/7.diff?token=x",
                "https://api.github.com/repos/o/r/issues/7",
            ),
            (
                "https://github.com/o/r/pull/7.diff",
                "https://api.github.com/repos/o/other/issues/7",
            ),
        ):
            with (
                self.subTest(diff_url=diff_url, issue_url=issue_url),
                self.assertRaises(ValueError),
            ):
                validate_pull_request_urls("o/r", 7, diff_url, issue_url)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GitHubResponseCapTests(unittest.TestCase):
    def test_transport_limits_cannot_be_disabled(self):
        for name, value in (
            ("timeout", 0),
            ("max_attempts", 0),
            ("max_response_bytes", 0),
            ("max_archive_bytes", 0),
            ("timeout", True),
            ("max_archive_bytes", float("nan")),
        ):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "limits"):
                GitHubClient("token", **{name: value})

    def test_token_must_be_safe_for_an_http_header(self):
        for token in ("bad token", "bad\ntoken", "tök", "x" * 4097, 7):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, "token"):
                GitHubClient(token)  # type: ignore[arg-type]

    def test_oversized_response_is_rejected(self):
        client = GitHubClient("t", max_response_bytes=16)

        @contextmanager
        def _fake_open(_request, timeout=None):
            yield _FakeResponse(b"x" * 64)

        client._opener.open = _fake_open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            client.fetch_diff("https://github.com/o/r/pull/1.diff")

    def test_within_limit_response_is_returned(self):
        client = GitHubClient("t", max_response_bytes=1024)

        @contextmanager
        def _fake_open(_request, timeout=None):
            yield _FakeResponse(b"diff-body")

        client._opener.open = _fake_open  # type: ignore[assignment]
        self.assertEqual("diff-body", client.fetch_diff("https://github.com/o/r/pull/1.diff"))


class GitHubRedirectHandlerTests(unittest.TestCase):
    def test_cross_host_redirect_drops_authorization(self):
        import urllib.request

        handler = _RestrictedRedirectHandler(GitHubClient("t").allowed_hosts)
        req = urllib.request.Request(
            "https://api.github.com/a", headers={"Authorization": "Bearer secret"}
        )
        new = handler.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/b")
        self.assertIsNotNone(new)
        self.assertIsNone(new.get_header("Authorization"))

    def test_redirect_to_unexpected_host_is_rejected(self):
        import urllib.request

        handler = _RestrictedRedirectHandler(GitHubClient("t").allowed_hosts)
        req = urllib.request.Request("https://api.github.com/a")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/b")

    def test_redirect_scheme_downgrade_is_rejected(self):
        import urllib.request

        handler = _RestrictedRedirectHandler(GitHubClient("t").allowed_hosts)
        req = urllib.request.Request("https://api.github.com/a")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://api.github.com/b")

    def test_same_host_redirect_retains_authorization(self):
        import urllib.request

        handler = _RestrictedRedirectHandler(GitHubClient("t").allowed_hosts)
        req = urllib.request.Request(
            "https://api.github.com/a", headers={"Authorization": "Bearer secret"}
        )
        new = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/b")
        self.assertEqual("Bearer secret", new.get_header("Authorization"))

    def test_github_transport_ignores_ambient_proxies(self):
        with mock.patch("urllib.request.getproxies", side_effect=AssertionError):
            GitHubClient("t")


class _RecordingOpener:
    """Return queued JSON bodies and record each dispatched request."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        body = self._bodies.pop(0)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        return _FakeResponse(payload)


class GitHubInstallationOAuthTests(unittest.TestCase):
    def test_transport_limits_cannot_be_disabled(self):
        for kwargs in (
            {"timeout": 0},
            {"max_response_bytes": 0},
            {"timeout": True},
            {"max_response_bytes": float("nan")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "limits"):
                GitHubInstallationOAuthClient("id", "secret", "https://app.example/cb", **kwargs)

    def _client(self, bodies):
        client = GitHubInstallationOAuthClient(
            "client-id",
            "client-secret",
            "https://review.example/github/oauth/callback",
        )
        opener = _RecordingOpener(bodies)
        client._opener = opener  # type: ignore[assignment]
        return client, opener

    def test_pkce_exchange_binds_only_an_installation_visible_to_the_user(self):
        client, opener = self._client(
            [
                {"access_token": "user-token"},
                {
                    "installations": [
                        {"id": 77, "account": {"login": "verified-org"}},
                    ]
                },
            ]
        )

        self.assertEqual("verified-org", client.verify_installation("oauth-code", "v" * 43, 77))

        exchange = urllib.parse.parse_qs(opener.requests[0].data.decode("ascii"))
        self.assertEqual(["client-secret"], exchange["client_secret"])
        self.assertEqual(["v" * 43], exchange["code_verifier"])
        self.assertEqual("Bearer user-token", opener.requests[1].get_header("Authorization"))

    def test_inaccessible_installation_is_rejected(self):
        client, _opener = self._client(
            [
                {"access_token": "user-token"},
                {"installations": [{"id": 78, "account": {"login": "other-org"}}]},
            ]
        )

        with self.assertRaisesRegex(PermissionError, "cannot access"):
            client.verify_installation("oauth-code", "v" * 43, 77)

    def test_unsafe_access_token_is_rejected_before_reuse(self):
        for token in ("bad token", "bad\ntoken", "tök"):
            client, opener = self._client([{"access_token": token}])
            with self.subTest(token=token), self.assertRaisesRegex(PermissionError, "access token"):
                client.verify_installation("oauth-code", "v" * 43, 77)
            self.assertEqual(1, len(opener.requests))

    def test_malformed_installation_identity_is_rejected(self):
        for installation, installation_id in (
            ({"id": True, "account": {"login": "wrong-org"}}, 1),
            ({"id": "77", "account": {"login": "wrong-org"}}, 77),
            ({"id": 77, "account": None}, 77),
            ({"id": 77, "account": {"login": []}}, 77),
            ({"id": 77, "account": {"login": ""}}, 77),
            ({"id": 77, "account": {"login": "x" * 257}}, 77),
        ):
            client, _opener = self._client(
                [{"access_token": "user-token"}, {"installations": [installation]}]
            )
            with (
                self.subTest(installation=installation),
                self.assertRaisesRegex(RuntimeError, "invalid installation"),
            ):
                client.verify_installation("oauth-code", "v" * 43, installation_id)


class GitHubApiMethodTests(unittest.TestCase):
    def _client(self, bodies, **kwargs):
        client = GitHubClient("tok", **kwargs)
        opener = _RecordingOpener(bodies)
        client._opener = opener  # type: ignore[assignment]
        return client, opener

    def test_get_pull_request_targets_pulls_endpoint(self):
        client, opener = self._client([{"number": 5}])
        self.assertEqual({"number": 5}, client.get_pull_request("Owner/Repo", 5))
        self.assertTrue(opener.requests[0].full_url.endswith("/repos/owner/repo/pulls/5"))

    def test_compare_diff_is_bound_to_two_commit_shas(self):
        base_sha, head_sha = "a" * 40, "b" * 40
        client, opener = self._client([b"fixed-diff"])

        self.assertEqual(
            "fixed-diff", client.fetch_compare_diff("Owner/Repo", base_sha, head_sha, 1024)
        )
        request = opener.requests[0]
        self.assertTrue(
            request.full_url.endswith("/repos/owner/repo/compare/%s...%s" % (base_sha, head_sha))
        )
        self.assertEqual("application/vnd.github.diff", request.get_header("Accept"))
        self.assertEqual("c" * 64, validate_commit_sha("c" * 64))

        for value in ("main", "A" * 40, "a" * 39, "a" * 65, "../" + "a" * 40):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_commit_sha(value)

    def test_json_responses_reject_ambiguous_or_nonstandard_values(self):
        bodies = (
            b'{"name":"first","name":"second"}',
            b'{"value":NaN}',
            b'{"value":"\\ud800"}',
            ('{"nested":' + "[" * 10_000 + "0" + "]" * 10_000 + "}").encode(),
        )

        for body in bodies:
            client, _opener = self._client([body])
            with self.subTest(body=body[:40]), self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                client.get_repository("o/r")

    def test_rejects_repository_and_file_path_injection_before_network(self):
        client, opener = self._client([])

        for repository in ("o/r?scope=other", "o/r/extra", "../r", "o/%2e%2e"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                client.get_pull_request(repository, 5)
        for path in ("../secret", "/absolute", "src//file.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.get_file("o/r", path, "main")

        self.assertEqual([], opener.requests)

    def test_get_file_decodes_content_and_pins_ref(self):
        encoded = base64.b64encode(b"hello world").decode()
        client, opener = self._client([{"content": encoded[:4] + "\n" + encoded[4:], "sha": "s"}])
        result = client.get_file("o/r", "src/a.py", "main")
        self.assertEqual("hello world", result["decoded_content"])
        self.assertIn("/contents/src/a.py", opener.requests[0].full_url)
        self.assertIn("ref=main", opener.requests[0].full_url)

    def test_get_file_rejects_malformed_provider_content(self):
        for response in (
            [],
            {},
            {"content": True},
            {"content": "not base64!"},
            {"content": base64.b64encode(b"\xff").decode()},
        ):
            client, _opener = self._client([response])
            with (
                self.subTest(response=response),
                self.assertRaisesRegex(RuntimeError, "invalid file"),
            ):
                client.get_file("o/r", "src/a.py", "main")

    def test_ensure_repository_access_rejects_mismatch(self):
        client, _ = self._client([{"full_name": "other/repo"}])
        with self.assertRaises(PermissionError):
            client.ensure_repository_access("o/r")

    def test_ensure_repository_access_accepts_case_insensitive_match(self):
        client, _ = self._client([{"full_name": "O/R"}])
        client.ensure_repository_access("o/r")  # does not raise

    def test_branch_and_pull_request_lookup_use_deterministic_head(self):
        client, opener = self._client(
            [{"object": {"sha": "abc"}}, [{"number": 7, "state": "open"}]]
        )
        branch = "evoagent/fix-pr-1-key"
        self.assertEqual("abc", client.get_branch("o/r", branch)["object"]["sha"])
        self.assertEqual(7, client.find_pull_request_by_head("o/r", branch, "main")["number"])
        self.assertIn("/git/ref/heads/evoagent%2Ffix-pr-1-key", opener.requests[0].full_url)
        self.assertIn("head=o%3Aevoagent%2Ffix-pr-1-key", opener.requests[1].full_url)
        self.assertIn("state=open", opener.requests[1].full_url)
        self.assertIn("base=main", opener.requests[1].full_url)

    def test_pull_request_lookup_rejects_ambiguous_provider_results(self):
        for result in ({"number": 7}, [{"number": 7}, {"number": 8}]):
            client, _ = self._client([result])
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "lookup"):
                client.find_pull_request_by_head("o/r", "branch", "main")

    def test_create_atomic_commit_creates_tree_commit_and_branch(self):
        client, opener = self._client(
            [
                {"tree": {"sha": "basetree"}},
                {"tree": [{"path": "a.py", "mode": "100755", "type": "blob", "sha": "old"}]},
                {"sha": "newtree"},
                {"sha": "commitsha"},
                {"ref": "refs/heads/b"},
            ]
        )
        before_write = mock.Mock()
        commit = client.create_atomic_commit(
            "o/r", "b", "parent", {"a.py": "x=1"}, "msg", before_write=before_write
        )
        self.assertEqual("commitsha", commit["sha"])
        self.assertEqual(3, before_write.call_count)
        self.assertEqual(5, len(opener.requests))
        # Tree must be based on the parent's tree and carry the file blob.
        tree_body = json.loads(opener.requests[2].data.decode("utf-8"))
        self.assertEqual("basetree", tree_body["base_tree"])
        self.assertEqual(
            [{"path": "a.py", "mode": "100755", "type": "blob", "content": "x=1"}],
            tree_body["tree"],
        )
        # Commit must point at the new tree and the given parent.
        commit_body = json.loads(opener.requests[3].data.decode("utf-8"))
        self.assertEqual("newtree", commit_body["tree"])
        self.assertEqual(["parent"], commit_body["parents"])
        # Branch ref must be created for the fresh commit sha.
        ref_body = json.loads(opener.requests[4].data.decode("utf-8"))
        self.assertEqual("refs/heads/b", ref_body["ref"])
        self.assertEqual("commitsha", ref_body["sha"])

    def test_create_atomic_commit_rejects_an_unsafe_or_incomplete_base_tree(self):
        for base_tree in (
            {"tree": [], "truncated": True},
            {"tree": []},
            {"tree": [{"path": "a.py", "mode": "120000", "type": "blob"}]},
        ):
            client, opener = self._client([{"tree": {"sha": "basetree"}}, base_tree])

            with (
                self.subTest(base_tree=base_tree),
                self.assertRaisesRegex(RuntimeError, "base tree|parent tree|regular file"),
            ):
                client.create_atomic_commit("o/r", "b", "parent", {"a.py": "x=1"}, "msg")

            self.assertEqual(2, len(opener.requests))

    def test_create_atomic_commit_reuses_only_the_verified_tree_and_parent(self):
        responses = [
            {"tree": {"sha": "basetree"}},
            {"tree": [{"path": "a.py", "mode": "100644", "type": "blob"}]},
            {"sha": "newtree"},
            {
                "sha": "commitsha",
                "tree": {"sha": "newtree"},
                "parents": [{"sha": "parent"}],
            },
            {"object": {"sha": "commitsha"}},
        ]
        client, opener = self._client(responses)

        commit = client.create_atomic_commit(
            "o/r", "b", "parent", {"a.py": "x=1"}, "msg", existing_sha="commitsha"
        )

        self.assertEqual("commitsha", commit["sha"])
        self.assertEqual(["GET", "GET", "POST", "GET", "GET"], [r.method for r in opener.requests])

        client, opener = self._client(
            responses[:3]
            + [{"sha": "commitsha", "tree": {"sha": "other"}, "parents": [{"sha": "parent"}]}]
        )
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            client.create_atomic_commit(
                "o/r", "b", "parent", {"a.py": "x=1"}, "msg", existing_sha="commitsha"
            )
        self.assertEqual(4, len(opener.requests))

    def test_create_draft_pull_request_sets_draft_flag(self):
        client, opener = self._client([{"number": 9}])
        client.create_draft_pull_request("o/r", "t", "head", "main", "body")
        sent = json.loads(opener.requests[0].data.decode("utf-8"))
        self.assertTrue(sent["draft"])

    def test_upsert_comment_patches_existing_marker(self):
        client, opener = self._client(
            [
                [{"body": "<!-- m -->\nold", "url": "https://api.github.com/comments/1"}],
                {"id": 1},
            ]
        )
        before_write = mock.Mock()
        client.upsert_comment(
            "https://api.github.com/repos/o/r/issues/1",
            "new",
            "<!-- m -->",
            before_write=before_write,
        )
        before_write.assert_called_once_with()
        self.assertEqual("PATCH", opener.requests[1].method)
        # Must patch the existing comment's own url and carry marker + new body.
        self.assertEqual("https://api.github.com/comments/1", opener.requests[1].full_url)
        patched = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual("<!-- m -->\nnew", patched["body"])

    def test_upsert_comment_finds_a_marker_after_the_first_page(self):
        client, opener = self._client(
            [
                [{"body": "unrelated"}] * 100,
                [{"body": "<!-- m -->\nold", "url": "https://api.github.com/comments/101"}],
                {"id": 101},
            ]
        )

        client.upsert_comment("https://api.github.com/repos/o/r/issues/1", "new", "<!-- m -->")

        self.assertIn("page=2", opener.requests[1].full_url)
        self.assertEqual("PATCH", opener.requests[2].method)

    def test_upsert_comment_ignores_a_marker_copied_by_another_author(self):
        client, opener = self._client(
            [
                [
                    {
                        "body": "<!-- m -->\nforged",
                        "url": "https://api.github.com/comments/1",
                        "user": {"login": "attacker"},
                    },
                    {
                        "body": "quoted <!-- m -->\nold",
                        "url": "https://api.github.com/comments/2",
                        "user": {"login": "EvoAgent[bot]"},
                    },
                    {
                        "body": "<!-- m -->\nold",
                        "url": "https://api.github.com/comments/3",
                        "user": {"login": "EvoAgent[bot]"},
                    },
                ],
                {"id": 3},
            ],
            comment_author_login="evoagent[bot]",
        )

        client.upsert_comment("https://api.github.com/repos/o/r/issues/1", "new", "<!-- m -->")

        self.assertEqual("https://api.github.com/comments/3", opener.requests[1].full_url)

    def test_upsert_comment_does_not_duplicate_beyond_its_lookup_bound(self):
        client, opener = self._client([[{"body": "unrelated"}] * 100] * 10)

        with self.assertRaisesRegex(RuntimeError, "bounded window"):
            client.upsert_comment("https://api.github.com/repos/o/r/issues/1", "new", "<!-- m -->")

        self.assertEqual(10, len(opener.requests))

    def test_download_archive_targets_zipball_and_caps_size(self):
        client = GitHubClient("tok", max_archive_bytes=8)

        @contextmanager
        def _fake_open(request, timeout=None):
            self._last = request  # type: ignore[attr-defined]
            yield _FakeResponse(b"x" * 64)

        client._opener.open = _fake_open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            client.download_archive("o/r", "main", max_bytes=4)
        self.assertIn("/repos/o/r/zipball/main", self._last.full_url)
        for limit in (0, True, 1.5):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "byte limit"):
                client.download_archive("o/r", "main", max_bytes=limit)  # type: ignore[arg-type]

    def test_upsert_comment_creates_when_no_marker(self):
        client, opener = self._client([[{"body": "unrelated"}], {"id": 2}])
        client.upsert_comment("https://api.github.com/repos/o/r/issues/1", "new", "<!-- m -->")
        self.assertEqual("POST", opener.requests[1].method)


class GitHubRetryTests(unittest.TestCase):
    def setUp(self):
        import evoagent.github as gh

        self.gh = gh
        self.sleeps = []
        original_sleep = gh.time.sleep
        gh.time.sleep = lambda s: self.sleeps.append(s)  # type: ignore[assignment]
        self.addCleanup(setattr, gh.time, "sleep", original_sleep)

    def _http_error(self, url, code, headers=None):
        return urllib.error.HTTPError(url, code, "err", headers or {}, io.BytesIO(b"boom"))

    def test_retries_on_500_then_succeeds(self):
        client = GitHubClient("tok", max_attempts=3)
        calls = {"n": 0}

        def _open(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._http_error(request.full_url, 500)
            return _FakeResponse(json.dumps({"ok": True}).encode())

        client._opener.open = _open  # type: ignore[assignment]
        self.assertEqual({"ok": True}, client.get_repository("o/r"))
        self.assertEqual(2, calls["n"])
        self.assertEqual(1, len(self.sleeps))

    def test_ambiguous_post_failure_is_left_to_durable_effect_replay(self):
        client = GitHubClient("tok", max_attempts=2)
        calls = 0
        before_write = mock.Mock()

        def _open(request, timeout=None):
            nonlocal calls
            calls += 1
            raise self._http_error(request.full_url, 500)

        client._opener.open = _open  # type: ignore[assignment]
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            client.create_draft_pull_request(
                "o/r", "title", "head", "main", "body", before_write=before_write
            )

        self.assertEqual(1, calls)
        before_write.assert_called_once_with()
        self.assertEqual([], self.sleeps)

    def test_secondary_rate_limit_403_is_retried(self):
        client = GitHubClient("tok", max_attempts=2)
        calls = {"n": 0}

        def _open(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._http_error(request.full_url, 403, {"X-RateLimit-Remaining": "0"})
            return _FakeResponse(json.dumps({"ok": True}).encode())

        client._opener.open = _open  # type: ignore[assignment]
        self.assertEqual({"ok": True}, client.get_repository("o/r"))
        self.assertEqual(2, calls["n"])

    def test_retry_after_header_drives_delay(self):
        client = GitHubClient("tok", max_attempts=2)
        calls = {"n": 0}

        def _open(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._http_error(request.full_url, 429, {"Retry-After": "7"})
            return _FakeResponse(json.dumps({"ok": True}).encode())

        client._opener.open = _open  # type: ignore[assignment]
        client.get_repository("o/r")
        self.assertEqual([7.0], self.sleeps)

    def test_rate_limit_reset_header_drives_delay(self):
        client = GitHubClient("tok", max_attempts=2)
        reset = str(int(self.gh.time.time()) + 5)
        calls = {"n": 0}

        def _open(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._http_error(request.full_url, 503, {"X-RateLimit-Reset": reset})
            return _FakeResponse(json.dumps({"ok": True}).encode())

        client._opener.open = _open  # type: ignore[assignment]
        client.get_repository("o/r")
        self.assertEqual(1, len(self.sleeps))
        self.assertGreater(self.sleeps[0], 0)

    def test_invalid_retry_headers_fall_back_to_a_bounded_delay(self):
        for header, value in (
            ("Retry-After", "not-a-number"),
            ("Retry-After", "nan"),
            ("X-RateLimit-Reset", "inf"),
            ("X-RateLimit-Reset", "-1"),
        ):
            calls = 0
            client = GitHubClient("tok", max_attempts=2)

            def _open(request, timeout=None, retry_header=header, retry_value=value):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise self._http_error(request.full_url, 429, {retry_header: retry_value})
                return _FakeResponse(json.dumps({"ok": True}).encode())

            client._opener.open = _open  # type: ignore[assignment]
            with self.subTest(header=header, value=value):
                self.sleeps.clear()
                self.assertEqual({"ok": True}, client.get_repository("o/r"))
                self.assertEqual(1, len(self.sleeps))
                self.assertGreaterEqual(self.sleeps[0], 0)
                self.assertLessEqual(self.sleeps[0], 10)

    def test_transport_error_is_retried_then_succeeds(self):
        client = GitHubClient("tok", max_attempts=3)
        calls = {"n": 0}

        def _open(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("temporary dns failure")
            return _FakeResponse(json.dumps({"ok": True}).encode())

        client._opener.open = _open  # type: ignore[assignment]
        self.assertEqual({"ok": True}, client.get_repository("o/r"))
        self.assertEqual(2, calls["n"])

    def test_response_read_timeout_is_retried_inside_the_breaker(self):
        breaker = CircuitBreaker("github-read", failure_threshold=2, reset_seconds=999)
        client = GitHubClient("tok", max_attempts=2, breaker=breaker)
        timed_out = mock.MagicMock()
        timed_out.__enter__.return_value = timed_out
        timed_out.read.side_effect = TimeoutError("stalled response")
        client._opener.open = mock.Mock(  # type: ignore[method-assign]
            side_effect=[timed_out, _FakeResponse(json.dumps({"ok": True}).encode())]
        )

        self.assertEqual({"ok": True}, client.get_repository("o/r"))

        self.assertEqual(CircuitBreaker.CLOSED, breaker.state)
        self.assertEqual(1, len(self.sleeps))
        timed_out.__exit__.assert_called_once()

    def test_transport_error_exhausts_attempts_and_raises(self):
        client = GitHubClient("tok", max_attempts=2)

        def _open(request, timeout=None):
            raise urllib.error.URLError("down")

        client._opener.open = _open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError) as ctx:
            client.get_repository("o/r")
        self.assertIn("request failed", str(ctx.exception))

    def test_non_retryable_error_raises_immediately(self):
        client = GitHubClient("tok", max_attempts=3)
        errors = []

        def _open(request, timeout=None):
            error = self._http_error(request.full_url, 404)
            errors.append(error)
            raise error

        client._opener.open = _open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError) as ctx:
            client.get_repository("o/r")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertEqual([], self.sleeps)
        self.assertTrue(errors[0].fp.closed)

    def test_repeated_server_errors_open_the_breaker_and_then_fail_fast(self):
        breaker = CircuitBreaker("github", failure_threshold=2, reset_seconds=999)
        client = GitHubClient("tok", max_attempts=2, breaker=breaker)
        calls = 0

        def _open(request, timeout=None):
            nonlocal calls
            calls += 1
            raise self._http_error(request.full_url, 503)

        client._opener.open = _open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            client.get_repository("o/r")
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)
        with self.assertRaises(CircuitOpenError):
            client.get_repository("o/r")
        self.assertEqual(2, calls)

        rate_limited = CircuitBreaker("github-rate", failure_threshold=1, reset_seconds=999)
        limited_client = GitHubClient("tok", max_attempts=1, breaker=rate_limited)

        def _rate_limited_open(request, timeout=None):
            raise self._http_error(request.full_url, 429)

        limited_client._opener.open = _rate_limited_open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            limited_client.get_repository("o/r")
        self.assertEqual(CircuitBreaker.CLOSED, rate_limited.state)


class GitHubAppAuthenticatorTests(unittest.TestCase):
    def test_response_limit_cannot_be_disabled(self):
        for value in (0, True, float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "limit"):
                GitHubAppAuthenticator("app", "key.pem", max_response_bytes=value)

    def setUp(self):
        # The token cache is a process-wide ClassVar; keep tests isolated.
        self.addCleanup(GitHubAppAuthenticator._cache.clear)
        self.addCleanup(GitHubAppAuthenticator._refresh_locks.clear)

    def test_installation_id_does_not_use_numeric_coercion(self):
        auth = GitHubAppAuthenticator("app", "key.pem")
        for installation_id in (
            True,
            1.5,
            "1",
            0,
            -1,
            GITHUB_INSTALLATION_ID_MAX + 1,
        ):
            with self.subTest(installation_id=installation_id):
                with self.assertRaisesRegex(ValueError, "installation id"):
                    auth.installation_token(installation_id)  # type: ignore[arg-type]
                with self.assertRaisesRegex(ValueError, "installation id"):
                    auth.invalidate(installation_id, "token")  # type: ignore[arg-type]

    def test_installation_token_caps_response_and_uses_restricted_opener(self):
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem", max_response_bytes=16)
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]

        @contextmanager
        def _fake_open(_request, timeout=None):
            yield _FakeResponse(b"x" * 128)

        auth._opener.open = _fake_open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            auth.installation_token(42)

    def test_installation_token_server_error_opens_shared_breaker(self):
        breaker = CircuitBreaker("github", failure_threshold=1, reset_seconds=999)
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem", breaker=breaker)
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]
        calls = 0

        def _open(request, timeout=None):
            nonlocal calls
            calls += 1
            raise urllib.error.HTTPError(request.full_url, 503, "err", {}, io.BytesIO())

        auth._opener.open = _open  # type: ignore[assignment]
        with self.assertRaises(urllib.error.HTTPError):
            auth.installation_token(42)
        with self.assertRaises(CircuitOpenError):
            auth.installation_token(42)
        self.assertEqual(1, calls)

    def test_installation_token_is_cached_until_near_expiry(self):
        auth = GitHubAppAuthenticator("cache-app", "/nonexistent.pem")
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]
        calls = {"n": 0}

        def _open(_request, timeout=None):
            calls["n"] += 1
            return _FakeResponse(
                json.dumps({"token": "t-123", "expires_at": "2999-01-01T00:00:00Z"}).encode()
            )

        auth._opener.open = _open  # type: ignore[assignment]
        first = auth.installation_token(101)
        second = auth.installation_token(101)
        self.assertEqual("t-123", first)
        self.assertEqual("t-123", second)
        self.assertEqual(1, calls["n"])

        with mock.patch.object(GitHubAppAuthenticator, "_cache_limit", 1):
            auth.installation_token(102)
        self.assertNotIn(("cache-app", 101), GitHubAppAuthenticator._cache)

    def test_concurrent_cache_miss_refreshes_one_installation_once(self):
        auth = GitHubAppAuthenticator("concurrent-app", "/nonexistent.pem")
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]
        first_call = threading.Event()
        second_call = threading.Event()
        release = threading.Event()
        calls = 0

        def _open(_request, timeout=None):
            nonlocal calls
            calls += 1
            (first_call if calls == 1 else second_call).set()
            release.wait(5)
            return _FakeResponse(
                json.dumps({"token": "shared", "expires_at": "2999-01-01T00:00:00Z"}).encode()
            )

        auth._opener.open = _open  # type: ignore[assignment]
        results = []
        workers = [
            threading.Thread(target=lambda: results.append(auth.installation_token(101)))
            for _ in range(2)
        ]
        workers[0].start()
        self.assertTrue(first_call.wait(1))
        workers[1].start()
        try:
            self.assertFalse(second_call.wait(0.2))
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

        self.assertEqual(["shared", "shared"], sorted(results))
        self.assertEqual(1, calls)
        self.assertNotIn(("concurrent-app", 101), GitHubAppAuthenticator._refresh_locks)

    def test_installation_token_uses_jwt_and_posts_to_access_tokens(self):
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem")
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]
        seen = {}

        def _open(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return _FakeResponse(
                json.dumps({"token": "fresh", "expires_at": "2999-01-01T00:00:00Z"}).encode()
            )

        auth._opener.open = _open  # type: ignore[assignment]
        self.assertEqual("fresh", auth.installation_token(55))
        self.assertTrue(seen["url"].endswith("/app/installations/55/access_tokens"))
        self.assertEqual("Bearer signed-jwt", seen["auth"])

    def test_installation_token_rejects_invalid_response_without_caching(self):
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem")
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]
        responses = (
            [],
            {"expires_at": "2999-01-01T00:00:00Z"},
            {"token": 7, "expires_at": "2999-01-01T00:00:00Z"},
            {"token": "bad\ntoken", "expires_at": "2999-01-01T00:00:00Z"},
            {"token": "tök", "expires_at": "2999-01-01T00:00:00Z"},
            {"token": "t", "expires_at": "not-a-date"},
            {"token": "t", "expires_at": "2999-01-01T00:00:00"},
            {"token": "t", "expires_at": "2000-01-01T00:00:00Z"},
        )
        for result in responses:
            auth._opener.open = lambda _request, timeout=None, value=result: _FakeResponse(  # type: ignore[assignment]
                json.dumps(value).encode()
            )
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "invalid"):
                auth.installation_token(77)
            self.assertNotIn(("app-id", 77), GitHubAppAuthenticator._cache)

    def test_unauthorized_client_invalidates_only_its_cached_token(self):
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem")
        key = ("app-id", 77)
        client = GitHubClient(
            "stale",
            max_attempts=1,
            on_unauthorized=lambda token: auth.invalidate(77, token),
        )
        client._opener.open = lambda request, timeout=None: (_ for _ in ()).throw(  # type: ignore[assignment]
            urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())
        )

        GitHubAppAuthenticator._cache[key] = {"token": "stale", "expires_at": 9e9}
        with self.assertRaises(RuntimeError):
            client.get_repository("o/r")
        self.assertNotIn(key, GitHubAppAuthenticator._cache)

        GitHubAppAuthenticator._cache[key] = {"token": "fresh", "expires_at": 9e9}
        with self.assertRaises(RuntimeError):
            client.get_repository("o/r")
        self.assertEqual("fresh", GitHubAppAuthenticator._cache[key]["token"])

    def test_app_jwt_signs_with_rs256_when_pyjwt_present(self):
        import sys
        import types

        captured = {}
        fake_jwt = types.ModuleType("jwt")

        def _encode(claims, key, algorithm):
            captured["claims"] = claims
            captured["key"] = key
            captured["algorithm"] = algorithm
            return "signed-token"

        fake_jwt.encode = _encode  # type: ignore[attr-defined]

        handle, key_path = tempfile.mkstemp(suffix=".pem")
        os.write(handle, b"PRIVATE-KEY-BYTES")
        os.close(handle)
        self.addCleanup(os.unlink, key_path)

        auth = GitHubAppAuthenticator("app-42", key_path)
        real_jwt = sys.modules.get("jwt")
        sys.modules["jwt"] = fake_jwt
        try:
            token = auth.app_jwt()
        finally:
            if real_jwt is None:
                sys.modules.pop("jwt", None)
            else:
                sys.modules["jwt"] = real_jwt

        self.assertEqual("signed-token", token)
        self.assertEqual("RS256", captured["algorithm"])
        self.assertEqual(b"PRIVATE-KEY-BYTES", captured["key"])
        self.assertEqual("app-42", captured["claims"]["iss"])

    def test_app_jwt_rejects_oversized_private_key(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"x" * (GitHubAppAuthenticator.MAX_PRIVATE_KEY_BYTES + 1))
            handle.flush()
            auth = GitHubAppAuthenticator("app", handle.name)
            with (
                mock.patch.dict("sys.modules", {"jwt": mock.Mock()}),
                self.assertRaisesRegex(RuntimeError, "private key exceeds"),
            ):
                auth.app_jwt()

    def test_app_jwt_requires_pyjwt(self):
        import builtins

        auth = GitHubAppAuthenticator("app", "/nonexistent.pem")
        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "jwt":
                raise ImportError("no jwt")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = _blocked  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError) as ctx:
                auth.app_jwt()
        finally:
            builtins.__import__ = real_import
        self.assertIn("PyJWT", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
