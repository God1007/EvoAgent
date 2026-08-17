"""Wire-level GitHub client tests against a local deterministic HTTP fixture."""

import json
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evoagent.github import GitHubClient


class _FixtureHandler(BaseHTTPRequestHandler):
    requests = []
    comment_reads = 0

    def log_message(self, _format, *_args):
        return

    def _record(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        return body

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        if self.path == "/repos/o/r/issues/1/comments?per_page=100":
            type(self).comment_reads += 1
            if type(self).comment_reads == 1:
                self._json(503, {"message": "retry fixture"}, {"Retry-After": "0"})
                return
            self._json(
                200,
                [{"body": "<!-- task:abc -->\nold", "url": "https://api.github.com/comments/7"}],
            )
            return
        self._json(404, {"message": "not found"})

    def do_PATCH(self):
        body = self._record()
        if self.path == "/comments/7":
            self._json(200, {"id": 7, "body": json.loads(body)["body"]})
            return
        self._json(404, {"message": "not found"})


class _MappedHttpOpener:
    """Map GitHub HTTPS URLs to the local fixture without changing client policy."""

    def __init__(self, fixture_base: str):
        self.fixture_base = fixture_base

    def open(self, request, timeout=None):
        source = urllib.parse.urlsplit(request.full_url)
        target = self.fixture_base + urllib.parse.urlunsplit(
            ("", "", source.path, source.query, "")
        )
        mapped = urllib.request.Request(
            target,
            data=request.data,
            headers=dict(request.header_items()),
            method=request.method,
        )
        return urllib.request.urlopen(mapped, timeout=timeout)


class GitHubHttpFixtureTests(unittest.TestCase):
    def setUp(self):
        _FixtureHandler.requests = []
        _FixtureHandler.comment_reads = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_retry_headers_auth_and_idempotent_comment_update_over_http(self):
        host, port = self.server.server_address
        client = GitHubClient("fixture-token", max_attempts=2)
        client._opener = _MappedHttpOpener("http://%s:%d" % (host, port))  # type: ignore[assignment]

        client.upsert_comment(
            "https://api.github.com/repos/o/r/issues/1",
            "new review",
            "<!-- task:abc -->",
        )

        self.assertEqual(["GET", "GET", "PATCH"], [r["method"] for r in _FixtureHandler.requests])
        for request in _FixtureHandler.requests:
            self.assertEqual("Bearer fixture-token", request["headers"]["Authorization"])
            self.assertEqual("2022-11-28", request["headers"]["X-Github-Api-Version"])
        payload = json.loads(_FixtureHandler.requests[-1]["body"])
        self.assertEqual("<!-- task:abc -->\nnew review", payload["body"])


if __name__ == "__main__":
    unittest.main()
