# Performance testing

The `perf/` and `scripts/loadgen.py` tools measure HTTP throughput, intake
latency, overload recovery and hot-path regressions. Results depend on hardware
and deployment configuration; the repository does not publish a universal
baseline.

For the full-stack rig use `docker-compose.perf.yml`. For the host runner, set
`EVOAGENT_DATABASE_URL` to a disposable PostgreSQL database before running
`scripts/perf_baseline.sh`.

The host runner refuses an occupied loopback port instead of terminating its
listener. Select another port with `EVOAGENT_PERF_PORT` or stop the other service
yourself. Teardown only signals the child PID started by this run; it never
searches for or kills other processes by port.

`--quick` checks the benchmark toolchain, not production capacity. Read-mix
scenarios exercise health/readiness; async intake measures acceptance, not Agent
completion throughput. Report those separately from end-to-end workflow latency
and sustained completion rates under a representative deployment workload.

## Local lifecycle audit — 2026-08-28

The occupied-port regression runs the actual baseline script while intercepting
signals for safety. Before the fix it timed out instead of refusing the port;
afterward it exits before starting a server and attempts no signal against the
existing listener. At that stage, the load-generator/microbenchmark unit group
passed 10 tests.

A loopback-only `single --quick` run with a fresh PostgreSQL database, empty Skill
directory and no provider/GitHub credentials completed the runner lifecycle.
Its 750 accepted synthetic review tasks were all `SUCCESS` when the run ended;
the child PID exited and its port could be rebound. This is not a measured
end-to-end completion-rate or latency baseline.

The audit also reproduced a generator issue: rate-changing scenarios derived the
next send time from the cumulative index times the *current* interval. The old
6-second spike sent 9,000 requests instead of its intended 7,380. Those old
rate-changing results are invalid and must not be compared as a capacity baseline.

## Measurement semantics

The generator now derives each planned send time from cumulative arrivals:
constant rate for ordinary scenarios, a linear zero-to-peak ramp, or a spike with
the first 20% of the window at 10% of peak. A late scheduler wakeup does not change
these timestamps. HTTP latency includes scheduler/worker-queue delay, transport
and server time; it is not isolated server processing time.

Enqueueing never waits for worker capacity. A full client queue records a dropped
request against the error budget rather than reducing the offered rate. Drops
have no fabricated HTTP latency sample; percentile data covers only requests that
a worker attempted. A drop can reflect client saturation, not necessarily server
capacity. The queue is bounded to 100 waiting jobs per worker.

| Report field | Meaning |
| --- | --- |
| `sent` | Planned/offered requests, including client-side drops; retained for report compatibility |
| `completed` | Finished HTTP attempts, including non-2xx responses and transport failures; not Agent task completions |
| `dropped` | Requests never handed to an HTTP worker |
| `errors` / `error_rate` | Non-2xx responses, transport failures and drops; divided by all offered requests |
| `status_counts["0"]` | No HTTP status: either a transport failure or a drop; `dropped` distinguishes them |
| `elapsed_seconds` | Measured time through the end of the arrival window and worker drain |
| `throughput_rps` | Completed HTTP attempts divided by actual elapsed time, not the requested arrival duration |

`sent = completed + dropped` after a finished run. Worker drain may extend beyond
`--duration`; all connections and worker threads are closed before returning the
report. JSON retains numerical precision for gates; only terminal text is rounded.
Redirects and authentication failures are errors, not successful workload. Invalid
limits/origins fail before warmup; a run with no completed HTTP attempts cannot
pass even when error/latency thresholds are disabled.

Deterministic regressions inject a 2.5-second scheduler delay, rate changes and a
fully saturated client queue. The load-generator/microbenchmark group now passes
13 tests and 21 subtests. Real loopback HTTP fixture runs also verified steady,
spike, ramp, 401 rejection, timeout and backlog behavior. In the backlog fixture,
1,000 requests produced 108 HTTP completions and 892 drops: error rate 89.2%, with
actual drain time about 1.34 seconds. This validates measurement/accounting, not
EvoAgent production capacity or deployment SLOs.

A corrected `single --quick` run against another fresh PostgreSQL database and
the actual application completed 7,380/7,380 spike requests, 7,500/7,500 soak
requests and 1,000/1,000 intake-mix requests, with no HTTP errors or client drops.
The intake mix returned 250 health responses and 750 accepted reviews; SQL showed
all 750 synthetic reviews at `SUCCESS` afterward. The owned server exited and its
port was released. This repeat validates the corrected runner against EvoAgent;
it still does not measure representative Agent completion throughput or qualify
the deployment's capacity.

## Workflow artifact retention — 2026-08-28

The serial runner now drops its reference to an artifact after the last consumer,
while retaining inputs needed by later branches or final outputs. Persisted
checkpoints are unchanged. In an isolated CPython 3.12/macOS `tracemalloc` run,
passing a 512 KiB text through 4 and 32 nodes previously peaked at 5.65 and
19.69 MiB; with dependency-based retention it peaked at 4.15 and 4.19 MiB.
These are Python allocation peaks for a synthetic in-process chain, not service
RSS, database storage, model memory or a production capacity claim.

`python -m pytest -q tests/test_workflow.py -k 'chain_memory or fanout_join'`
checks bounded chain growth and preserves late branch/final-input references.
Wide joins still retain their necessary inputs, and serialization/validation
creates temporary copies; the 8 MiB handoff limit is not a process-memory quota.

Oversized handoffs are also rejected before constructing their complete encoded
representation. A conservative preflight bounds traversal, followed by stdlib
incremental JSON encoding with an exact UTF-8 byte budget. In isolated allocation
measurements (input objects created before tracing), rejecting 32 text ports of
512 KiB each fell from 32.01 MiB to 0.008 MiB peak. With a 256 KiB test limit,
16 ports of 10 KiB control characters expand beyond that limit when escaped;
their rejection fell from 1.88 MiB to 0.387 MiB. These synthetic results do not
include Agent allocations or promise an RSS cap. The encoder can still allocate
an individual chunk before measuring its bytes.

`python -m pytest -q tests/test_workflow.py -k json_` verifies byte-for-byte
compatibility, exact limits, Unicode/escape expansion, bounded shared-subtree
preflight, and rejection without publishing partial outputs to the receiver.

## Studio completion and replica baseline — 2026-08-28

A controlled workload now measures completed custom workflows, not just HTTP
acceptance. The fixture uses two published rule Agents (`SEC-EVAL` and
`REL-DEBUG-PRINT`) feeding a published merge. Each fresh task receives the same
10,940-byte Diff: four files, 400 added lines, eight expected findings. Every
accepted task was checked for the pinned workflow digest, eight correct findings
and exactly three completed node checkpoints at attempt 1. There is no reuse of
another task's result, model call, GitHub request or container execution.

Environment: local Apple M5 Pro, 18 logical CPUs, 48 GiB RAM, CPython 3.12.13,
PostgreSQL 16.15/schema 28 and Redis 7.4.11. API, database, Redis and generator share the
host. Each process has a pool maximum of 10; the shared tenant admission limit is
100. Authentication is disabled only on these isolated loopback fixture APIs.
The application execution revision was
`2d00cdca809286c61e6a56dcf54b1f46562c35f362e32dcea86915e27ade46b3`.

Each row offers 750 reviews at 50/s over 15 seconds, with 16 HTTP client workers.
Read-only warmup does not create review tasks. All review submissions go to the first
API; a second replica, when present, consumes the same Redis stream. This tests
task-level worker scaling, not ingress load balancing or parallel DAG branches.

| Processes × workers/process | Accepted | HTTP 429 | Observed successful tasks/s | Creation → success event P99 | Redis depth after HTTP drain |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 × 2 | 636 | 114 | 36.66 | 2,774 ms | 98 |
| 1 × 4 | 718 | 32 | 42.03 | 2,401 ms | 100 |
| 2 × 2, first run | 750 | 0 | 49.64 | 67 ms | 0 |
| 2 × 2, repeat | 750 | 0 | 49.40 | 89 ms | 1 |

All accepted tasks eventually reached `SUCCESS`; no transport errors, client
drops or DLQ entries occurred. Each two-process run recorded 374 and 376 successful
reviews in the replicas' respective metrics, totaling its 750 persisted reports.
The admission gate rejected excess work rather than accepting an unbounded queue.
The local comparison supports using existing replicas before adding another
scheduler; it does not isolate a CPU/SQL bottleneck or establish a universal
worker count. Two replicas also use two pools and roughly twice the API RSS.

The completion-rate denominator starts before generating requests and ends after
observing terminal tasks, released admission, drained Outbox and empty Redis.
It includes HTTP/client drain and observer overhead. The P99 column instead uses
server wall-clock timestamps on task creation and the `SUCCESS` trace event;
it includes queueing but excludes time before task creation and the final commit
after that event timestamp. It is **not** browser-observed or scheduled-to-durable
latency. The two-process HTTP acceptance P99s were 13.85 and 15.99 ms, a different
metric. These short synthetic runs do not qualify real-PR, provider, authenticated
deployment, soak or production SLO performance.

### Reproduce the workload without a new load framework

Use a fresh disposable PostgreSQL database and Redis instance, with loopback APIs
running matching application revisions. Keep all model/GitHub credentials absent. Publish the three-Agent
fixture above and bind a new repository name for **each** sample; never reuse a
production repository or aggregate earlier runs. Configure worker count through
`EVOAGENT_ASYNC_WORKERS`, pool size through `EVOAGENT_PG_POOL_MAX`, and the common
limit through `EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS=100`. Give host replicas distinct
ports (8199/8200 here); both must share the database, Redis and Skill configuration.

From the checkout, reuse the existing generator's configurable scenario data:

```python
import json
from scripts import loadgen

repository = "perf/studio-sample-001"  # Bind this exact, unused fixture repository first.
lines = ["def changed(value, user_input):", "    result = eval(user_input)", "    print(value)"] + [
    f"    value_{i} = value + {i}" for i in range(97)
]
diff = "".join(
    f"--- a/module_{i}.py\n+++ b/module_{i}.py\n@@ -0,0 +1,100 @@\n"
    + "".join("+" + line + "\n" for line in lines)
    for i in range(4)
)
loadgen.SCENARIOS["studio"] = {
    "ramp": False,
    "mix": [("POST", "/v1/reviews?async=true", 1, {"repository": repository, "diff": diff})],
}
loadgen.generate("http://127.0.0.1:8199", "steady", 1, 5, 4, {}, 10)
result = loadgen.generate("http://127.0.0.1:8199", "studio", 15, 50, 16, {}, 10)
print(json.dumps(loadgen.summarize(result, 15), indent=2))  # HTTP intake only.
```

After bounded drain, reconcile its HTTP 202 count with the exact repository's
persisted task count, successful reports, three completed checkpoints per task,
zero active admission, zero pending/publishing Outbox and empty queue/DLQ. Do not
omit failed/cancelled/missing tasks from the completion result. For event timing:

```sql
WITH timing AS (
  SELECT t.id, t.state,
    1000 * EXTRACT(EPOCH FROM
      MIN(e.created_at) FILTER (WHERE e.state = 'SUCCESS') - t.created_at) AS ms
  FROM tasks t LEFT JOIN trace_events e ON e.task_id = t.id
  WHERE t.tenant_id = 'default' AND t.repository = 'perf/studio-sample-001'
  GROUP BY t.id
)
SELECT state, COUNT(*) AS tasks, COUNT(ms) AS with_success_event,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY ms) AS event_p99_ms
FROM timing GROUP BY state;
```

Retain each run's offered/accepted/rejected counts, event timings, observed drain
duration, configuration and per-replica counter deltas. A timeout, count mismatch
or missing output is a failed qualification, not an empty/zero-latency sample.

The exact Python and SQL recipe above was also executed against a fresh fixture
repository: 750 successful reports and 2,250 completed Agent checkpoints. The
existing load-generator/microbenchmark checks passed 13 tests and 21 subtests.
No production implementation or default concurrency setting was changed.
