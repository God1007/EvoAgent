import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager

from evoagent.github import (
    GitHubAppAuthenticator,
    GitHubClient,
    _RestrictedRedirectHandler,
    _validate_github_url,
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


class GitHubApiMethodTests(unittest.TestCase):
    def _client(self, bodies):
        client = GitHubClient("tok")
        opener = _RecordingOpener(bodies)
        client._opener = opener  # type: ignore[assignment]
        return client, opener

    def test_get_pull_request_targets_pulls_endpoint(self):
        client, opener = self._client([{"number": 5}])
        self.assertEqual({"number": 5}, client.get_pull_request("o/r", 5))
        self.assertTrue(opener.requests[0].full_url.endswith("/repos/o/r/pulls/5"))

    def test_get_file_decodes_content_and_pins_ref(self):
        encoded = base64.b64encode(b"hello world").decode()
        client, opener = self._client([{"content": encoded, "sha": "s"}])
        result = client.get_file("o/r", "src/a.py", "main")
        self.assertEqual("hello world", result["decoded_content"])
        self.assertIn("/contents/src/a.py", opener.requests[0].full_url)
        self.assertIn("ref=main", opener.requests[0].full_url)

    def test_ensure_repository_access_rejects_mismatch(self):
        client, _ = self._client([{"full_name": "other/repo"}])
        with self.assertRaises(PermissionError):
            client.ensure_repository_access("o/r")

    def test_ensure_repository_access_accepts_case_insensitive_match(self):
        client, _ = self._client([{"full_name": "O/R"}])
        client.ensure_repository_access("o/r")  # does not raise

    def test_create_atomic_commit_creates_tree_commit_and_branch(self):
        client, opener = self._client(
            [
                {"tree": {"sha": "basetree"}},
                {"sha": "newtree"},
                {"sha": "commitsha"},
                {"ref": "refs/heads/b"},
            ]
        )
        commit = client.create_atomic_commit("o/r", "b", "parent", {"a.py": "x=1"}, "msg")
        self.assertEqual("commitsha", commit["sha"])
        self.assertEqual(4, len(opener.requests))
        # Tree must be based on the parent's tree and carry the file blob.
        tree_body = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual("basetree", tree_body["base_tree"])
        self.assertEqual(
            [{"path": "a.py", "mode": "100644", "type": "blob", "content": "x=1"}],
            tree_body["tree"],
        )
        # Commit must point at the new tree and the given parent.
        commit_body = json.loads(opener.requests[2].data.decode("utf-8"))
        self.assertEqual("newtree", commit_body["tree"])
        self.assertEqual(["parent"], commit_body["parents"])
        # Branch ref must be created for the fresh commit sha.
        ref_body = json.loads(opener.requests[3].data.decode("utf-8"))
        self.assertEqual("refs/heads/b", ref_body["ref"])
        self.assertEqual("commitsha", ref_body["sha"])

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
        client.upsert_comment("https://api.github.com/repos/o/r/issues/1", "new", "<!-- m -->")
        self.assertEqual("PATCH", opener.requests[1].method)
        # Must patch the existing comment's own url and carry marker + new body.
        self.assertEqual("https://api.github.com/comments/1", opener.requests[1].full_url)
        patched = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual("<!-- m -->\nnew", patched["body"])

    def test_download_archive_targets_zipball_and_caps_size(self):
        client = GitHubClient("tok", max_archive_bytes=8)

        @contextmanager
        def _fake_open(request, timeout=None):
            self._last = request  # type: ignore[attr-defined]
            yield _FakeResponse(b"x" * 64)

        client._opener.open = _fake_open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            client.download_archive("o/r", "main")
        self.assertIn("/repos/o/r/zipball/main", self._last.full_url)

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

        def _open(request, timeout=None):
            raise self._http_error(request.full_url, 404)

        client._opener.open = _open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError) as ctx:
            client.get_repository("o/r")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertEqual([], self.sleeps)


class GitHubAppAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        # The token cache is a process-wide ClassVar; keep tests isolated.
        self.addCleanup(GitHubAppAuthenticator._cache.clear)

    def test_installation_token_caps_response_and_uses_restricted_opener(self):
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem", max_response_bytes=16)
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]

        @contextmanager
        def _fake_open(_request, timeout=None):
            yield _FakeResponse(b"x" * 128)

        auth._opener.open = _fake_open  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            auth.installation_token(42)

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

    def test_installation_token_tolerates_unparseable_expiry(self):
        auth = GitHubAppAuthenticator("app-id", "/nonexistent.pem")
        auth.app_jwt = lambda: "signed-jwt"  # type: ignore[method-assign]

        def _open(_request, timeout=None):
            return _FakeResponse(json.dumps({"token": "t", "expires_at": "not-a-date"}).encode())

        auth._opener.open = _open  # type: ignore[assignment]
        self.assertEqual("t", auth.installation_token(77))
        # A far-future default keeps the token cached rather than crashing.
        self.assertIn(("app-id", 77), GitHubAppAuthenticator._cache)

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
