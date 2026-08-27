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
