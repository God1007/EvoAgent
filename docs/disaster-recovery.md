# Disaster recovery

PostgreSQL is durable state; Redis Streams are reconstructable delivery state.
The recovery tools never restore over the source database.

## PostgreSQL drill

`evoagent-dr` takes a consistent dump, creates a disposable database, restores
it, verifies every migration checksum, compares schema and row fingerprints,
exercises the store, and emits a JSON RPO/RTO report. It requires PostgreSQL
client tools (`pg_dump`, `createdb`, `pg_restore`, `dropdb`).

```bash
evoagent-dr \
  --database-url "$EVOAGENT_DATABASE_URL" \
  --output-dir ./dr-evidence \
  --max-rpo-seconds 300 \
  --max-rto-seconds 900
```

Use credentials that may create and drop only disposable drill databases. The
tool accepts only generated `evoagent_drill_<uuid>` targets and cleans them up.
Retain the dump and manifest according to the deployment's backup policy.

## Redis reconstruction

When Redis is lost, stop workers first. `evoagent-recover-queue` reads incomplete
tasks and outbox state from PostgreSQL, creates a content-hashed plan, and only
publishes after an operator repeats that hash with `--apply`. The target Redis
database must be empty and must use TLS outside loopback.

Run a dry plan, review unrecoverable task IDs, then apply exactly that plan.
Start one worker, verify terminal state and external-effect idempotency, then
restore normal capacity.

## Minimum evidence

- snapshot time and source database identity;
- migration and content fingerprints;
- observed RPO/RTO and objective result;
- recovery plan hash and recovered task count;
- one verified review result after restore.
