import base64
import hashlib
import hmac
import http.client
import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar
from weakref import WeakValueDictionary

from . import __version__
from .errors import AccessDeniedError
from .json_boundary import strict_json_loads
from .repository import canonical_repository


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# Only these GitHub-owned hosts may ever receive a request carrying the token.
# `github.com` serves `.diff`/`.patch` links, `codeload.github.com` serves the
# archive redirect, and `api.github.com` serves the REST API.
DEFAULT_GITHUB_HOSTS = frozenset({"api.github.com", "github.com", "codeload.github.com"})
_GITHUB_SERVER_ERRORS = frozenset({500, 502, 503, 504})
GITHUB_INSTALLATION_ID_MAX = 2**63 - 1
_GITHUB_TOKEN = re.compile(r"[\x21-\x7e]{1,4096}")


def _valid_github_token(value: str, *, allow_empty: bool = False) -> bool:
    return (allow_empty and not value) or _GITHUB_TOKEN.fullmatch(value) is not None


def _github_repository_url(repository: str, suffix: str = "") -> str:
    repository = canonical_repository(repository)
    return "https://api.github.com/repos/" + repository + suffix


def _github_file_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("GitHub file path must be repository-relative")
    return urllib.parse.quote(path, safe="/")


def _validate_github_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Reject any URL that is not an https request to an allowed GitHub host.

    A pull_request webhook payload supplies `diff_url`/`issue_url` verbatim, so
    an attacker with a valid signature could otherwise point the client (and its
    Authorization header) at an arbitrary host. Enforcing scheme, host and the
    absence of embedded credentials closes that SSRF / token-exfiltration path.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("refusing non-https GitHub URL: %s" % url)
    if parts.username or parts.password:
        raise ValueError("refusing GitHub URL with embedded credentials")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError("refusing GitHub URL to unexpected host: %s" % (host or "<none>"))
    if parts.port not in (None, 443):
        raise ValueError("refusing GitHub URL to unexpected port: %s" % parts.port)
    return host


def validate_pull_request_urls(repository: str, number: int, diff_url: str, issue_url: str) -> None:
    repository = canonical_repository(repository)
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError("GitHub pull request number must be positive")
    expected = (
        (diff_url, "github.com", "/%s/pull/%d.diff" % (repository, number)),
        (issue_url, "api.github.com", "/repos/%s/issues/%d" % (repository, number)),
    )
    for url, host, path in expected:
        parts = urllib.parse.urlsplit(url)
        if (
            _validate_github_url(url, DEFAULT_GITHUB_HOSTS) != host
            or parts.path.casefold() != path.casefold()
            or parts.query
            or parts.fragment
        ):
            raise ValueError("GitHub pull request URL does not match repository and number")


def _read_capped(response, limit: int) -> bytes:
    """Read at most `limit` bytes, raising if the response is larger."""
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError("GitHub response exceeded the %d byte limit" % limit)
    return body


def _retry_delay(headers, attempt: int) -> float:
    fallback = min(2 ** (attempt - 1) + random.random(), 10)
    try:
        if headers.get("Retry-After") is not None:
            delay = float(headers["Retry-After"])
        elif headers.get("X-RateLimit-Reset") is not None:
            delay = float(headers["X-RateLimit-Reset"]) - time.time()
        else:
            return fallback
    except (TypeError, ValueError):
        return fallback
    return min(delay, 30) if math.isfinite(delay) and delay >= 0 else fallback


def _read_with_breaker(
    opener,
    request: urllib.request.Request,
    timeout: int,
    breaker,
    limit: int,
) -> bytes:
    if breaker is not None:
        breaker.allow()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = _read_capped(response, limit)
    except urllib.error.HTTPError as exc:
        if breaker is not None:
            if exc.code in _GITHUB_SERVER_ERRORS:
                breaker.record_failure()
            else:
                breaker.record_success()
        raise
    except Exception:
        if breaker is not None:
            breaker.record_failure()
        raise
    if breaker is not None:
        breaker.record_success()
    return body


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate redirect targets and drop the token on cross-host redirects."""

    def __init__(self, allowed_hosts: frozenset[str]):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_github_url(newurl, self.allowed_hosts)
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None:
            origin = urllib.parse.urlsplit(req.full_url).hostname
            target = urllib.parse.urlsplit(newurl).hostname
            if origin != target:
                new_request.remove_header("Authorization")
        return new_request


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github_opener(allowed_hosts: frozenset[str], redirects: bool = True):
    redirect_handler = (
        _RestrictedRedirectHandler(allowed_hosts) if redirects else _NoRedirectHandler()
    )
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect_handler)


class GitHubClient:
    def __init__(
        self,
        token: str,
        timeout: int = 30,
        max_attempts: int = 4,
        allowed_hosts: frozenset[str] | None = None,
        max_response_bytes: int = 25 * 1024 * 1024,
        max_archive_bytes: int = 1024 * 1024 * 1024,
        breaker=None,
        on_unauthorized: Callable[[str], object] | None = None,
        comment_author_login: str = "",
    ):
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (timeout, max_attempts, max_response_bytes, max_archive_bytes)
        ):
            raise ValueError("GitHub client transport limits must be positive integers")
        if not isinstance(token, str) or not _valid_github_token(token, allow_empty=True):
            raise ValueError("GitHub token must be a valid HTTP header value")
        if (
            not isinstance(comment_author_login, str)
            or len(comment_author_login) > 256
            or comment_author_login != comment_author_login.strip()
            or (comment_author_login and not comment_author_login.isprintable())
        ):
            raise ValueError("GitHub comment author must be a bounded printable login")
        self.token = token
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.allowed_hosts = allowed_hosts or DEFAULT_GITHUB_HOSTS
        self.max_response_bytes = max_response_bytes
        self.max_archive_bytes = max_archive_bytes
        self._opener = _github_opener(self.allowed_hosts)
        # Optional circuit breaker: after this client's internal retries are
        # exhausted repeatedly, the breaker trips and further calls fail fast.
        self._breaker = breaker
        self._on_unauthorized = on_unauthorized
        self.comment_author_login = comment_author_login.casefold()

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "EvoAgent/%s" % __version__,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def fetch_diff(self, url: str, max_bytes: int | None = None) -> str:
        body = self._request(
            "GET", url, accept="application/vnd.github.v3.diff", raw=True, max_bytes=max_bytes
        )
        return body.decode("utf-8", errors="replace")

    def upsert_comment(
        self,
        api_url: str,
        markdown: str,
        marker: str,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Update this service's existing review comment instead of creating duplicates."""
        comments_url = api_url.rstrip("/") + "/comments"
        body = marker + "\n" + markdown
        # ponytail: scan at most 1000 comments; persist the provider comment id if
        # repositories measurably exceed this instead of adding a search subsystem.
        for page in range(1, 11):
            comments = self._json("GET", comments_url + "?per_page=100&page=%d" % page)
            if not isinstance(comments, list):
                raise RuntimeError("GitHub returned an invalid comment list")
            for comment in comments:
                if not isinstance(comment, dict):
                    raise RuntimeError("GitHub returned an invalid comment")
                if str(comment.get("body", "")).startswith(marker + "\n"):
                    user = comment.get("user")
                    if self.comment_author_login and (
                        not isinstance(user, dict)
                        or str(user.get("login", "")).casefold() != self.comment_author_login
                    ):
                        continue
                    self._json("PATCH", comment["url"], {"body": body}, before_write)
                    return
            if len(comments) < 100:
                break
        else:
            raise RuntimeError("GitHub comment marker lookup exceeded its bounded window")
        self._json("POST", comments_url, {"body": body}, before_write)

    def _json(self, method: str, url: str, payload=None, before_write=None):
        return self._request(method, url, payload, before_write=before_write)

    def _request(
        self,
        method: str,
        url: str,
        payload=None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
        max_bytes: int | None = None,
        before_write: Callable[[], None] | None = None,
    ):
        _validate_github_url(url, self.allowed_hosts)
        limit = self.max_response_bytes if max_bytes is None else max_bytes
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        safe_to_retry = method in {"GET", "HEAD"}
        for attempt in range(1, self.max_attempts + 1):
            if data is not None and before_write is not None:
                before_write()
            request = urllib.request.Request(
                url,
                data=data,
                headers=dict(self._headers(accept), **{"Content-Type": "application/json"}),
                method=method,
            )
            # Keep open + bounded response read in one retry/breaker scope.
            try:
                body = _read_with_breaker(self._opener, request, self.timeout, self._breaker, limit)
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code == 401 and self._on_unauthorized is not None:
                        self._on_unauthorized(self.token)
                    retryable = safe_to_retry and (
                        exc.code == 429 or exc.code in _GITHUB_SERVER_ERRORS
                    )
                    if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                        retryable = safe_to_retry
                    if not retryable or attempt >= self.max_attempts:
                        detail = exc.read(1000).decode("utf-8", errors="replace")
                        raise RuntimeError(
                            "GitHub API %s %s returned HTTP %d: %s"
                            % (method, url, exc.code, detail)
                        ) from exc
                    time.sleep(_retry_delay(exc.headers, attempt))
                    continue
                finally:
                    exc.close()
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as exc:
                if not safe_to_retry or attempt >= self.max_attempts:
                    raise RuntimeError("GitHub API request failed: %s" % exc) from exc
                time.sleep(min(2 ** (attempt - 1) + random.random(), 10))
                continue
            if raw:
                return body
            if not body:
                return {}
            try:
                return strict_json_loads(body)
            except (UnicodeError, ValueError, RecursionError):
                raise RuntimeError("GitHub returned invalid JSON") from None

    def get_pull_request(self, repository: str, number: int) -> dict:
        return self._json("GET", _github_repository_url(repository, "/pulls/%d" % number))

    def get_file(self, repository: str, path: str, ref: str) -> dict:
        quoted = _github_file_path(path)
        result = self._json(
            "GET",
            _github_repository_url(
                repository,
                "/contents/%s?ref=%s" % (quoted, urllib.parse.quote(ref, safe="")),
            ),
        )
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, str):
            raise RuntimeError("GitHub returned an invalid file response")
        try:
            result["decoded_content"] = base64.b64decode(
                content.replace("\r", "").replace("\n", ""), validate=True
            ).decode("utf-8")
        except (UnicodeError, ValueError):
            raise RuntimeError("GitHub returned invalid file content") from None
        return result

    def get_repository(self, repository: str) -> dict:
        return self._json("GET", _github_repository_url(repository))

    def get_branch(self, repository: str, branch: str) -> dict | None:
        try:
            return self._json(
                "GET",
                _github_repository_url(
                    repository,
                    "/git/ref/heads/%s" % urllib.parse.quote(branch, safe=""),
                ),
            )
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def find_pull_request_by_head(self, repository: str, branch: str, base: str) -> dict | None:
        repository = canonical_repository(repository)
        owner = repository.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {"state": "open", "head": "%s:%s" % (owner, branch), "base": base}
        )
        values = self._json(
            "GET",
            _github_repository_url(repository, "/pulls?" + query),
        )
        if (
            not isinstance(values, list)
            or len(values) > 1
            or (values and not isinstance(values[0], dict))
        ):
            raise RuntimeError("GitHub returned an invalid repair pull request lookup")
        return values[0] if values else None

    def ensure_repository_access(self, repository: str) -> None:
        result = self.get_repository(repository)
        if str(result.get("full_name", "")).lower() != repository.lower():
            raise AccessDeniedError("GitHub installation is not authorized for this repository")

    def create_branch(
        self,
        repository: str,
        branch: str,
        sha: str,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        self._json(
            "POST",
            _github_repository_url(repository, "/git/refs"),
            {"ref": "refs/heads/" + branch, "sha": sha},
            before_write,
        )

    def create_atomic_commit(
        self,
        repository: str,
        branch: str,
        parent_sha: str,
        files: dict[str, str],
        message: str,
        existing_sha: str = "",
        before_write: Callable[[], None] | None = None,
    ) -> dict:
        parent = self._json(
            "GET",
            _github_repository_url(
                repository, "/git/commits/%s" % urllib.parse.quote(parent_sha, safe="")
            ),
        )
        parent_tree = parent.get("tree") if isinstance(parent, dict) else None
        tree_sha = parent_tree.get("sha") if isinstance(parent_tree, dict) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            raise RuntimeError("GitHub returned an invalid parent commit")
        base_tree = self._json(
            "GET",
            _github_repository_url(
                repository,
                "/git/trees/%s?recursive=1" % urllib.parse.quote(tree_sha, safe=""),
            ),
        )
        entries = base_tree.get("tree") if isinstance(base_tree, dict) else None
        if not isinstance(entries, list) or base_tree.get("truncated"):
            raise RuntimeError("GitHub returned an incomplete base tree")
        modes: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str) and path in files:
                mode = entry.get("mode")
                if (
                    entry.get("type") != "blob"
                    or not isinstance(mode, str)
                    or mode not in {"100644", "100755"}
                ):
                    raise RuntimeError("automatic repair target is not a regular file")
                modes[path] = mode
        if modes.keys() != files.keys():
            raise RuntimeError("automatic repair target is missing from the parent tree")
        tree = self._json(
            "POST",
            _github_repository_url(repository, "/git/trees"),
            {
                "base_tree": tree_sha,
                "tree": [
                    {"path": path, "mode": modes[path], "type": "blob", "content": content}
                    for path, content in sorted(files.items())
                ],
            },
            before_write,
        )
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            raise RuntimeError("GitHub returned an invalid repair tree")
        if existing_sha:
            existing = self._json(
                "GET",
                _github_repository_url(
                    repository,
                    "/git/commits/%s" % urllib.parse.quote(existing_sha, safe=""),
                ),
            )
            existing_tree = existing.get("tree") if isinstance(existing, dict) else None
            parents = existing.get("parents") if isinstance(existing, dict) else None
            if (
                not isinstance(existing, dict)
                or existing.get("sha") != existing_sha
                or not isinstance(existing_tree, dict)
                or existing_tree.get("sha") != tree_sha
                or not isinstance(parents, list)
                or len(parents) != 1
                or not isinstance(parents[0], dict)
                or parents[0].get("sha") != parent_sha
            ):
                raise RuntimeError("existing repair branch does not match the verified repair")
            current = self.get_branch(repository, branch)
            current_object = current.get("object") if isinstance(current, dict) else None
            if not isinstance(current_object, dict) or current_object.get("sha") != existing_sha:
                raise RuntimeError("existing repair branch changed during validation")
            return existing
        commit = self._json(
            "POST",
            _github_repository_url(repository, "/git/commits"),
            {"message": message, "tree": tree_sha, "parents": [parent_sha]},
            before_write,
        )
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(commit_sha, str) or not commit_sha:
            raise RuntimeError("GitHub returned an invalid repair commit")
        self.create_branch(repository, branch, commit_sha, before_write)
        return commit

    def create_draft_pull_request(
        self,
        repository: str,
        title: str,
        head: str,
        base: str,
        body: str,
        before_write: Callable[[], None] | None = None,
    ) -> dict:
        return self._json(
            "POST",
            _github_repository_url(repository, "/pulls"),
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
            before_write,
        )

    def download_archive(self, repository: str, ref: str) -> bytes:
        return self._request(
            "GET",
            _github_repository_url(repository, "/zipball/%s" % urllib.parse.quote(ref, safe="")),
            accept="application/vnd.github+json",
            raw=True,
            max_bytes=self.max_archive_bytes,
        )


class GitHubInstallationOAuthClient:
    _CODE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

    @classmethod
    def valid_authorization_code(cls, value: object) -> bool:
        return isinstance(value, str) and cls._CODE.fullmatch(value) is not None

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        callback_url: str,
        timeout: int = 30,
        max_response_bytes: int = 4 * 1024 * 1024,
    ):
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (timeout, max_response_bytes)
        ):
            raise ValueError("GitHub OAuth transport limits must be positive integers")
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.allowed_hosts = frozenset({"github.com", "api.github.com"})
        self._opener = _github_opener(self.allowed_hosts, redirects=False)

    @staticmethod
    def installation_url(app_slug: str, state: str) -> str:
        return "https://github.com/apps/%s/installations/new?%s" % (
            urllib.parse.quote(app_slug, safe=""),
            urllib.parse.urlencode({"state": state}),
        )

    def authorization_url(self, state: str, code_verifier: str) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        return "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.callback_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

    def verify_installation(self, code: str, code_verifier: str, installation_id: int) -> str:
        if not self.valid_authorization_code(code):
            raise AccessDeniedError("invalid GitHub authorization code")
        token = self._exchange_code(code, code_verifier)
        for page in range(1, 101):
            result = self._request_json(
                urllib.request.Request(
                    "https://api.github.com/user/installations?"
                    + urllib.parse.urlencode({"per_page": 100, "page": page}),
                    headers=self._headers(token),
                )
            )
            installations = result.get("installations")
            if not isinstance(installations, list):
                raise RuntimeError("GitHub returned an invalid installation list")
            for installation in installations:
                if not isinstance(installation, dict):
                    raise RuntimeError("GitHub returned an invalid installation")
                candidate_id = installation.get("id")
                if (
                    not isinstance(candidate_id, int)
                    or isinstance(candidate_id, bool)
                    or not 1 <= candidate_id <= GITHUB_INSTALLATION_ID_MAX
                ):
                    raise RuntimeError("GitHub returned an invalid installation id")
                if candidate_id == installation_id:
                    account = installation.get("account")
                    if not isinstance(account, dict):
                        raise RuntimeError("GitHub returned an invalid installation account")
                    login = account.get("login")
                    if not isinstance(login, str) or not login or len(login) > 256:
                        raise RuntimeError("GitHub returned an invalid installation account login")
                    return login
            if len(installations) < 100:
                break
        raise AccessDeniedError("GitHub user cannot access the requested installation")

    def _exchange_code(self, code: str, code_verifier: str) -> str:
        request = urllib.request.Request(
            "https://github.com/login/oauth/access_token",
            data=urllib.parse.urlencode(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": self.callback_url,
                    "code_verifier": code_verifier,
                }
            ).encode("ascii"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "EvoAgent/%s" % __version__,
            },
        )
        result = self._request_json(request)
        token = result.get("access_token")
        if not isinstance(token, str) or not _valid_github_token(token):
            raise AccessDeniedError("GitHub authorization did not return an access token")
        return token

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "EvoAgent/%s" % __version__,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        _validate_github_url(request.full_url, self.allowed_hosts)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = _read_capped(response, self.max_response_bytes)
        except urllib.error.HTTPError as exc:
            try:
                exc.read(1024)
            finally:
                exc.close()
            if exc.code in {400, 401, 403, 404}:
                raise AccessDeniedError("GitHub authorization was rejected") from exc
            raise RuntimeError("GitHub OAuth request failed") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("GitHub OAuth is unavailable") from exc
        try:
            result = strict_json_loads(body)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise RuntimeError("GitHub returned invalid OAuth JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("GitHub returned an invalid OAuth response")
        return result


class GitHubAppAuthenticator:
    MAX_PRIVATE_KEY_BYTES = 64 * 1024
    # ponytail: 4096 live installations per process; eviction only forces a safe refresh.
    _cache_limit = 4096
    _cache: ClassVar[OrderedDict[tuple[str, int], dict[str, Any]]] = OrderedDict()
    _refresh_locks: ClassVar[WeakValueDictionary[tuple[str, int], threading.Lock]] = (
        WeakValueDictionary()
    )
    _lock = threading.Lock()

    def __init__(
        self,
        app_id: str,
        private_key_path: str,
        allowed_hosts: frozenset[str] | None = None,
        max_response_bytes: int = 1024 * 1024,
        breaker=None,
    ):
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("GitHub App response limit must be a positive integer")
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.allowed_hosts = allowed_hosts or DEFAULT_GITHUB_HOSTS
        self.max_response_bytes = max_response_bytes
        self._breaker = breaker
        self._opener = _github_opener(self.allowed_hosts, redirects=False)

    def app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("GitHub App mode requires: pip install PyJWT[crypto]") from exc
        with open(self.private_key_path, "rb") as handle:
            key = handle.read(self.MAX_PRIVATE_KEY_BYTES + 1)
        if len(key) > self.MAX_PRIVATE_KEY_BYTES:
            raise RuntimeError("GitHub private key exceeds the 64 KiB limit")
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id}, key, algorithm="RS256"
        )

    @staticmethod
    def _installation_id(value: object) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= GITHUB_INSTALLATION_ID_MAX
        ):
            raise ValueError("GitHub installation id must be a positive 64-bit integer")
        return value

    def installation_token(self, installation_id: int) -> str:
        installation_id = self._installation_id(installation_id)
        cache_key = (self.app_id, installation_id)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached["expires_at"] > time.time() + 120:
                self._cache.move_to_end(cache_key)
                return cached["token"]
            refresh_lock = self._refresh_locks.setdefault(cache_key, threading.Lock())
        with refresh_lock:
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached and cached["expires_at"] > time.time() + 120:
                    self._cache.move_to_end(cache_key)
                    return cached["token"]
            url = "https://api.github.com/app/installations/%d/access_tokens" % installation_id
            _validate_github_url(url, self.allowed_hosts)
            request = urllib.request.Request(
                url,
                data=b"{}",
                method="POST",
                headers={
                    "Authorization": "Bearer " + self.app_jwt(),
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "EvoAgent/%s" % __version__,
                    "Content-Type": "application/json",
                },
            )
            try:
                raw = _read_with_breaker(
                    self._opener,
                    request,
                    30,
                    self._breaker,
                    self.max_response_bytes,
                )
            except urllib.error.HTTPError as exc:
                exc.close()
                raise
            try:
                result = strict_json_loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                token = result.get("token")
                expires = result.get("expires_at")
                if not isinstance(token, str) or not _valid_github_token(token):
                    raise ValueError
                if not isinstance(expires, str):
                    raise ValueError
                expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    raise ValueError
                expires_at = expiry.timestamp()
                if expires_at <= time.time() + 120:
                    raise ValueError
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise RuntimeError("GitHub returned an invalid installation token") from exc
            with self._lock:
                self._cache[cache_key] = {"token": token, "expires_at": expires_at}
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_limit:
                    self._cache.popitem(last=False)
            return token

    def invalidate(self, installation_id: int, token: str) -> bool:
        installation_id = self._installation_id(installation_id)
        cache_key = (self.app_id, installation_id)
        with self._lock:
            cached = self._cache.get(cache_key)
            if not cached or not hmac.compare_digest(str(cached.get("token", "")), token):
                return False
            del self._cache[cache_key]
            return True
