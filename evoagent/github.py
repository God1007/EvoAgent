import base64
import hashlib
import hmac
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, ClassVar


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# Only these GitHub-owned hosts may ever receive a request carrying the token.
# `github.com` serves `.diff`/`.patch` links, `codeload.github.com` serves the
# archive redirect, and `api.github.com` serves the REST API.
DEFAULT_GITHUB_HOSTS = frozenset({"api.github.com", "github.com", "codeload.github.com"})


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


def _read_capped(response, limit: int) -> bytes:
    """Read at most `limit` bytes, raising if the response is larger."""
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError("GitHub response exceeded the %d byte limit" % limit)
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
    ):
        self.token = token
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.allowed_hosts = allowed_hosts or DEFAULT_GITHUB_HOSTS
        self.max_response_bytes = max_response_bytes
        self.max_archive_bytes = max_archive_bytes
        self._opener = urllib.request.build_opener(_RestrictedRedirectHandler(self.allowed_hosts))
        # Optional circuit breaker: after this client's internal retries are
        # exhausted repeatedly, the breaker trips and further calls fail fast.
        self._breaker = breaker

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "EvoAgent/0.1",
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

    def post_comment(self, api_url: str, markdown: str) -> None:
        self._json("POST", api_url.rstrip("/") + "/comments", {"body": markdown})

    def upsert_comment(self, api_url: str, markdown: str, marker: str) -> None:
        """Update this service's existing review comment instead of creating duplicates."""
        comments_url = api_url.rstrip("/") + "/comments"
        comments = self._json("GET", comments_url + "?per_page=100")
        body = marker + "\n" + markdown
        for comment in comments:
            if marker in str(comment.get("body", "")):
                self._json("PATCH", comment["url"], {"body": body})
                return
        self._json("POST", comments_url, {"body": body})

    def _json(self, method: str, url: str, payload=None):
        return self._request(method, url, payload)

    def _request(
        self,
        method: str,
        url: str,
        payload=None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
        max_bytes: int | None = None,
    ):
        _validate_github_url(url, self.allowed_hosts)
        limit = self.max_response_bytes if max_bytes is None else max_bytes
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                url,
                data=data,
                headers=dict(self._headers(accept), **{"Content-Type": "application/json"}),
                method=method,
            )
            # Route the transport through the breaker so hard connectivity/timeout
            # failures (the ones that pin a worker for the full timeout) trip it
            # and fail fast. HTTP error *responses* mean the dependency is alive,
            # so they are handled below and are NOT counted as breaker failures.
            try:
                response = self._open(request)
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {429, 500, 502, 503, 504}
                if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                    retryable = True
                if not retryable or attempt >= self.max_attempts:
                    detail = exc.read(1000).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        "GitHub API %s %s returned HTTP %d: %s" % (method, url, exc.code, detail)
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                reset = exc.headers.get("X-RateLimit-Reset")
                if retry_after:
                    delay = float(retry_after)
                elif reset:
                    delay = max(0.0, float(reset) - time.time())
                else:
                    delay = min(2 ** (attempt - 1) + random.random(), 10)
                time.sleep(min(delay, 30))
                continue
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self.max_attempts:
                    raise RuntimeError("GitHub API request failed: %s" % exc) from exc
                time.sleep(min(2 ** (attempt - 1) + random.random(), 10))
                continue
            with response as resp:
                body = _read_capped(resp, limit)
            if raw:
                return body
            return json.loads(body.decode("utf-8")) if body else {}

    def _open(self, request: "urllib.request.Request"):
        """Open a request, routing transport (connect/timeout) failures through
        the circuit breaker so a dead endpoint fails fast. An HTTPError *response*
        means the dependency answered, so it counts as a breaker success and is
        re-raised for the caller's retry/raise logic (no double request)."""
        if self._breaker is None:
            return self._opener.open(request, timeout=self.timeout)
        self._breaker.allow()
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError:
            self._breaker.record_success()
            raise
        except Exception:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return response

    def get_pull_request(self, repository: str, number: int) -> dict:
        return self._json("GET", "https://api.github.com/repos/%s/pulls/%d" % (repository, number))

    def get_file(self, repository: str, path: str, ref: str) -> dict:
        quoted = urllib.parse.quote(path, safe="/")
        result = self._json(
            "GET",
            "https://api.github.com/repos/%s/contents/%s?ref=%s"
            % (repository, quoted, urllib.parse.quote(ref, safe="")),
        )
        result["decoded_content"] = base64.b64decode(result["content"]).decode("utf-8")
        return result

    def get_repository(self, repository: str) -> dict:
        return self._json("GET", "https://api.github.com/repos/%s" % repository)

    def get_branch(self, repository: str, branch: str) -> dict | None:
        try:
            return self._json(
                "GET",
                "https://api.github.com/repos/%s/git/ref/heads/%s"
                % (repository, urllib.parse.quote(branch, safe="")),
            )
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def find_pull_request_by_head(self, repository: str, branch: str) -> dict | None:
        owner = repository.split("/", 1)[0]
        values = self._json(
            "GET",
            "https://api.github.com/repos/%s/pulls?state=all&head=%s"
            % (repository, urllib.parse.quote("%s:%s" % (owner, branch), safe="")),
        )
        return values[0] if values else None

    def ensure_repository_access(self, repository: str) -> None:
        result = self.get_repository(repository)
        if str(result.get("full_name", "")).lower() != repository.lower():
            raise PermissionError("GitHub installation is not authorized for this repository")

    def create_branch(self, repository: str, branch: str, sha: str) -> None:
        self._json(
            "POST",
            "https://api.github.com/repos/%s/git/refs" % repository,
            {"ref": "refs/heads/" + branch, "sha": sha},
        )

    def commit_file(
        self, repository: str, path: str, branch: str, content: str, sha: str, message: str
    ) -> dict:
        quoted = urllib.parse.quote(path, safe="/")
        return self._json(
            "PUT",
            "https://api.github.com/repos/%s/contents/%s" % (repository, quoted),
            {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "sha": sha,
                "branch": branch,
            },
        )

    def create_atomic_commit(
        self,
        repository: str,
        branch: str,
        parent_sha: str,
        files: dict[str, str],
        message: str,
    ) -> dict:
        parent = self._json(
            "GET", "https://api.github.com/repos/%s/git/commits/%s" % (repository, parent_sha)
        )
        tree = self._json(
            "POST",
            "https://api.github.com/repos/%s/git/trees" % repository,
            {
                "base_tree": parent["tree"]["sha"],
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": content}
                    for path, content in sorted(files.items())
                ],
            },
        )
        commit = self._json(
            "POST",
            "https://api.github.com/repos/%s/git/commits" % repository,
            {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )
        self.create_branch(repository, branch, commit["sha"])
        return commit

    def create_draft_pull_request(
        self,
        repository: str,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> dict:
        return self._json(
            "POST",
            "https://api.github.com/repos/%s/pulls" % repository,
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )

    def download_archive(self, repository: str, ref: str) -> bytes:
        return self._request(
            "GET",
            "https://api.github.com/repos/%s/zipball/%s"
            % (repository, urllib.parse.quote(ref, safe="")),
            accept="application/vnd.github+json",
            raw=True,
            max_bytes=self.max_archive_bytes,
        )


class GitHubAppAuthenticator:
    _cache: ClassVar[dict[tuple[str, int], dict[str, Any]]] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        app_id: str,
        private_key_path: str,
        allowed_hosts: frozenset[str] | None = None,
        max_response_bytes: int = 1024 * 1024,
    ):
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.allowed_hosts = allowed_hosts or DEFAULT_GITHUB_HOSTS
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_RestrictedRedirectHandler(self.allowed_hosts))

    def app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("GitHub App mode requires: pip install PyJWT[crypto]") from exc
        with open(self.private_key_path, "rb") as handle:
            key = handle.read()
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id}, key, algorithm="RS256"
        )

    def installation_token(self, installation_id: int) -> str:
        cache_key = (self.app_id, int(installation_id))
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached["expires_at"] > time.time() + 120:
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
                "User-Agent": "EvoAgent/0.3",
                "Content-Type": "application/json",
            },
        )
        with self._opener.open(request, timeout=30) as response:
            result = json.loads(_read_capped(response, self.max_response_bytes).decode("utf-8"))
        expires = result.get("expires_at", "")
        try:
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except ValueError:
            expires_at = time.time() + 3000
        with self._lock:
            self._cache[cache_key] = {"token": result["token"], "expires_at": expires_at}
        return result["token"]
