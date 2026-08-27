# Disaster recovery

PostgreSQL is durable state; Redis Streams are reconstructable delivery state.
The recovery tools never restore over the source database.

## PostgreSQL drill

`evoagent-dr` takes a consistent dump, creates a disposable database, restores
it, verifies every migration checksum, compares schema and row fingerprints,
exercises the store, and emits a JSON RPO/RTO report. It reads the source connection
from `EVOAGENT_DATABASE_URL` and requires PostgreSQL client tools `pg_dump` and
`pg_restore`. Database creation and cleanup use the existing Python driver.

```bash
evoagent-dr \
  --output-dir ./dr-evidence \
  --max-rpo-seconds 300 \
  --max-rto-seconds 900
```

Use a dedicated drill role that can read the source, connect to `postgres`, create
databases and drop its own generated databases. The tool accepts only generated
`evoagent_drill_<uuid>` targets and cleans them up; this name guard is an application
safety check, not a database permission boundary.
Database connections and statements use the command timeout; connection setup is
capped at 30 seconds.
Timestamp fingerprints normalize timezone-aware values to UTC, so equivalent
instants compare equally across source and restore database timezone settings;
naive timestamps and dates retain their original values.
Retain the dump and manifest according to the deployment's backup policy.

## Redis reconstruction

When Redis is lost, pause incoming traffic/webhooks and stop every service replica,
including its workers and Outbox dispatcher. `evoagent-recover-queue` reads incomplete
tasks and outbox state from PostgreSQL and creates a content-hashed plan. After an
operator repeats that hash with `--apply`, it reserves the Redis target and stages
Outbox intents in PostgreSQL; it does not publish queue messages or run Agents.
The target Redis database must be empty and must use TLS outside loopback.
Connection query parameters are rejected because redis-py lets them override
the tool's TLS and timeout policy; select the logical database with the URL path
(for example, `rediss://cache.example/3`).

Set `EVOAGENT_DATABASE_URL` to the restored PostgreSQL connection and
`EVOAGENT_REDIS_URL` to the empty recovery target. Replace `restored_evoagent` below
with the database name you have independently checked; the CLI verifies it against
both the configured URL and the actual connection.

```bash
recovery_database=restored_evoagent
recovery_id="$(python -c 'import uuid; print(uuid.uuid4())')"
evoagent-recover-queue \
  --recovery-id "$recovery_id" \
  --confirm-database "$recovery_database" > recovery-plan.json
```

Review the plan before proceeding: candidate count, recoverable/unrecoverable
counts and affected task IDs. Do not bypass missing payloads with
`--allow-unrecoverable` unless the incident owner explicitly accepts those omissions.

```bash
plan_sha256="$(python -c 'import json; print(json.load(open("recovery-plan.json"))["plan"]["plan_sha256"])')"
evoagent-recover-queue \
  --recovery-id "$recovery_id" \
  --confirm-database "$recovery_database" \
  --expect-plan-sha256 "$plan_sha256" --apply
```

Before restarting services, retrying the same ID/hash against its reserved target
does not create duplicate intents. A mismatched plan or a nonempty target is
rejected. If Redis is lost again, use a new recovery ID and review a fresh plan.
Start one service replica with the original pinned workflow configuration; verify
task completion, unchanged completed handoffs and external-effect idempotency,
then restore normal capacity and traffic. Never edit checkpoints or stream records
to force a task through.

## Minimum evidence

- snapshot time and source database identity;
- migration and content fingerprints;
- observed RPO/RTO and objective result;
- recovery plan hash and recovered task count;
- one verified review result after restore.
