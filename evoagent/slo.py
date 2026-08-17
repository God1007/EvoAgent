"""Machine-readable SLO catalog and Prometheus evaluation CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Protocol

_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_WINDOW = re.compile(r"^[1-9][0-9]*[smhdwy]$")
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
SOURCE_CATALOG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ops", "slo.toml"))
INSTALLED_CATALOG = os.path.join(sys.prefix, "share", "evoagent", "ops", "slo.toml")
DEFAULT_CATALOG = SOURCE_CATALOG if os.path.isfile(SOURCE_CATALOG) else INSTALLED_CATALOG


class SLOError(RuntimeError):
    pass


class PrometheusQueryPort(Protocol):
    def query(self, expression: str) -> float: ...


@dataclass(frozen=True)
class SLOObjective:
    objective_id: str
    description: str
    target: float
    window: str
    indicator_query: str
    sample_query: str
    min_samples: int


@dataclass(frozen=True)
class SLOCatalog:
    version: int
    objectives: tuple[SLOObjective, ...]


@dataclass(frozen=True)
class SLOResult:
    objective_id: str
    status: str
    target: float
    achieved: float | None
    samples: int
    window: str
    error_budget_remaining: float | None
    description: str


def load_slo_catalog(path: str) -> SLOCatalog:
    with open(path, "rb") as handle:
        document = tomllib.load(handle)
    if document.get("version") != 1:
        raise ValueError("unsupported SLO catalog version")
    raw_objectives = document.get("objectives")
    if not isinstance(raw_objectives, list) or not raw_objectives:
        raise ValueError("SLO catalog must define at least one objective")
    objectives = []
    seen = set()
    for raw in raw_objectives:
        if not isinstance(raw, dict):
            raise ValueError("SLO objective must be a table")
        objective_id = str(raw.get("id", ""))
        if not _ID.fullmatch(objective_id) or objective_id in seen:
            raise ValueError("SLO objective ids must be unique lowercase slugs")
        seen.add(objective_id)
        target = float(raw.get("target", 0))
        if not 0 < target < 1:
            raise ValueError("SLO target must be greater than 0 and less than 1")
        window = str(raw.get("window", ""))
        if not _WINDOW.fullmatch(window):
            raise ValueError("SLO window must use Prometheus duration syntax")
        indicator_query = str(raw.get("indicator_query", "")).strip()
        sample_query = str(raw.get("sample_query", "")).strip()
        if not indicator_query or not sample_query:
            raise ValueError("SLO indicator and sample queries are required")
        min_samples = int(raw.get("min_samples", 1))
        if min_samples <= 0:
            raise ValueError("SLO min_samples must be positive")
        objectives.append(
            SLOObjective(
                objective_id,
                str(raw.get("description", "")).strip(),
                target,
                window,
                indicator_query,
                sample_query,
                min_samples,
            )
        )
    return SLOCatalog(1, tuple(objectives))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrometheusClient:
    def __init__(
        self,
        base_url: str,
        allowed_hosts: tuple[str, ...] = (),
        bearer_token: str = "",
        timeout_seconds: int = 10,
        max_response_bytes: int = 1024 * 1024,
        opener: Any | None = None,
    ):
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        host = (parsed.hostname or "").lower().rstrip(".")
        allowlist = {item.strip().lower().rstrip(".") for item in allowed_hosts if item.strip()}
        if not host or (host not in _LOOPBACK and host not in allowlist):
            raise ValueError("Prometheus host must be loopback or explicitly allowlisted")
        if parsed.scheme == "http" and host not in _LOOPBACK:
            raise ValueError("Prometheus requires HTTPS outside loopback")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Prometheus URL must use HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Prometheus URL must not contain credentials, query, or fragment")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("Prometheus client limits must be positive")
        self.base_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def query(self, expression: str) -> float:
        url = self.base_url + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_response_bytes:
                    raise SLOError("Prometheus response exceeds the byte limit")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise SLOError("Prometheus query failed with HTTP %d" % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise SLOError("Prometheus query transport failed") from exc
        if len(body) > self.max_response_bytes:
            raise SLOError("Prometheus response exceeds the byte limit")
        try:
            document = json.loads(body)
            if not isinstance(document, dict):
                raise SLOError("Prometheus returned an invalid SLO response")
            if document.get("status") != "success":
                raise SLOError("Prometheus rejected the SLO query")
            data = document["data"]
            result_type = data["resultType"]
            result = data["result"]
            if result_type == "scalar":
                raw_value = result[1]
            elif result_type == "vector" and len(result) == 1:
                raw_value = result[0]["value"][1]
            else:
                raise SLOError("Prometheus SLO query must return exactly one value")
            value = float(raw_value)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SLOError("Prometheus returned an invalid SLO response") from exc
        if value != value or value in {float("inf"), float("-inf")}:
            raise SLOError("Prometheus returned a non-finite SLO value")
        return value


def evaluate_slos(
    catalog: SLOCatalog,
    client: PrometheusQueryPort,
    window_override: str = "",
) -> list[SLOResult]:
    if window_override and not _WINDOW.fullmatch(window_override):
        raise ValueError("SLO window override must use Prometheus duration syntax")
    results = []
    for objective in catalog.objectives:
        window = window_override or objective.window
        indicator_query = objective.indicator_query.replace("$window", window)
        sample_query = objective.sample_query.replace("$window", window)
        samples = max(0, int(client.query(sample_query)))
        if samples < objective.min_samples:
            results.append(
                SLOResult(
                    objective.objective_id,
                    "no-data",
                    objective.target,
                    None,
                    samples,
                    window,
                    None,
                    objective.description,
                )
            )
            continue
        achieved = min(1.0, max(0.0, client.query(indicator_query)))
        budget = 1.0 - objective.target
        remaining = 1.0 - ((1.0 - achieved) / budget)
        results.append(
            SLOResult(
                objective.objective_id,
                "pass" if achieved >= objective.target else "fail",
                objective.target,
                round(achieved, 9),
                samples,
                window,
                round(remaining, 6),
                objective.description,
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate EvoAgent SLOs against Prometheus")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument(
        "--prometheus-url",
        default=os.getenv("EVOAGENT_PROMETHEUS_URL", "http://127.0.0.1:9090"),
    )
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--window", default="")
    parser.add_argument("--allow-no-data", action="store_true")
    args = parser.parse_args(argv)
    catalog = load_slo_catalog(args.catalog)
    client = PrometheusClient(
        args.prometheus_url,
        tuple(args.allowed_host),
        os.getenv("EVOAGENT_PROMETHEUS_BEARER_TOKEN", ""),
    )
    results = evaluate_slos(catalog, client, args.window)
    payload = {
        "catalog_version": catalog.version,
        "status": (
            "fail"
            if any(result.status == "fail" for result in results)
            else "no-data"
            if any(result.status == "no-data" for result in results)
            else "pass"
        ),
        "objectives": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if any(result.status == "fail" for result in results):
        return 1
    if not args.allow_no_data and any(result.status == "no-data" for result in results):
        return 2
    return 0


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
