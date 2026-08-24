# Performance testing

The `perf/` and `scripts/loadgen.py` tools measure HTTP throughput, intake
latency, overload recovery and hot-path regressions. Results depend on hardware
and deployment configuration; the repository does not publish a universal
baseline.

For the full-stack rig use `docker-compose.perf.yml`. For the host runner, set
`EVOAGENT_DATABASE_URL` to a disposable PostgreSQL database before running
`scripts/perf_baseline.sh`.
