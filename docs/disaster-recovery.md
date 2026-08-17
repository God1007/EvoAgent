# Database backup, restore, and disaster recovery

EvoAgent treats a backup as usable only after an isolated restore has passed
schema, content, integrity, and application smoke checks. `evoagent-dr` produces
a versioned JSON evidence manifest and exits non-zero when the declared RPO or
RTO is missed.

This is a database recovery baseline, not a claim of regional high
availability. A same-cluster restore drill cannot prove DNS, network, secret,
object-store, or regional control-plane failover.

## Recovery contract

The repository ships these engineering acceptance defaults:

| Measure | PostgreSQL drill | SQLite drill | Meaning |
| --- | ---: | ---: | --- |
| RPO | 1 hour | 1 hour | Age of the newly captured recovery point when validation finishes |
| RTO | 15 minutes | 15 minutes | Restore start through integrity and application validation |

Pass explicit objectives on every scheduled run. The defaults are not a
business-approved production contract. A scheduled backup system must also
prove that its most recent durable, cross-region object remains inside the RPO;
an immediate drill only measures the fresh snapshot it just created.

## PostgreSQL drill

Prerequisites:

- `EVOAGENT_DATABASE_URL` names the source database. Never pass credentials on
  the command line.
- `pg_dump` and `pg_restore` are the same major version as the server or newer.
- the drill role can read all application objects and create/drop a database on
  the drill cluster;
- the destination directory is encrypted storage with enough capacity for the
  custom-format dump.

```bash
export EVOAGENT_DATABASE_URL='postgresql://...'
install -d -m 0700 /var/lib/evoagent/recovery
evoagent-dr --backend postgresql \
  --output-dir /var/lib/evoagent/recovery \
  --max-rpo-seconds 3600 \
  --max-rto-seconds 900
```

The implementation exports a PostgreSQL MVCC snapshot, fingerprints every
`public` table from that exact snapshot, and gives the snapshot ID to
`pg_dump --format=custom`. It then:

1. generates a database name matching only
   `evoagent_drill_<32 lowercase hex characters>`;
2. creates that database from `template0`;
3. runs `pg_restore --exit-on-error` into the generated database;
4. validates migration checksums, columns/defaults, constraints, indexes,
   extensions, per-table row counts, and order-independent content hashes;
5. opens the restored database through `PostgresTaskStore`, performs readiness
   reads, and writes a disposable audit event to exercise a restored sequence;
6. terminates only sessions connected to the generated database and drops it;
7. writes a mode-`0600` manifest beside the mode-`0600` dump.

The source URL is never an eligible restore target. Passwords are removed from
the `pg_dump`/`pg_restore` argument vector and passed through `PGPASSWORD`; the
manifest records only host, port, database, and user. Environment access still
requires a locked-down job runtime because process environments are privileged
data.

## SQLite drill

SQLite is a development/single-node topology, but its recovery path is fully
exercised:

```bash
evoagent-dr --backend sqlite \
  --sqlite-path ./evoagent.db \
  --output-dir ./recovery \
  --max-rpo-seconds 3600 \
  --max-rto-seconds 300
```

The source is copied with SQLite's online backup API, not a filesystem copy.
The artifact is then restored through the backup API into a second database.
`integrity_check`, `foreign_key_check`, migration checksums, schema SQL,
per-table counts/content hashes, Store readiness, and an audit write must pass.
The temporary restored copy is deleted; the snapshot and manifest remain.

## Evidence and retention

Every successful manifest contains:

- `report_schema_version`, drill ID, timestamps, backend, and sanitized source;
- artifact byte length and SHA-256;
- source/restored schema and content fingerprints;
- migration version, table count, total rows, and application smoke result;
- observed RPO/RTO values and pass/fail decisions;
- proof that the disposable restore target was removed.

The table fingerprints contain no row values, diffs, model prompts, tokens, or
credentials. The backup artifact contains the complete database and is
sensitive. Production automation must upload it to a private object store with
TLS, KMS-backed encryption, object lock/WORM retention, access logging, and a
tested lifecycle policy. Upload only the manifest to ordinary CI artifacts;
the repository CI intentionally does not upload its database dump.

Recommended cadence:

- continuous managed-database backups or WAL archiving/PITR;
- daily verification that the newest durable recovery point is inside RPO;
- weekly isolated restore drill using `evoagent-dr`;
- quarterly failover into a separate account/project and region;
- annual credential-loss and operator handoff exercise.

## Production recovery runbook

1. Declare the incident, freeze writers, record the failure time, and choose the
   newest clean recovery point before the corruption event.
2. Provision a new PostgreSQL cluster/database. Never restore over the damaged
   source; preserve it for forensics.
3. Restore the vendor snapshot/PITR or encrypted `pg_dump` artifact.
4. Run migration checksum, schema, content sampling, tenant isolation, and
   `PostgresTaskStore` smoke checks before attaching application credentials.
5. Provision a clean Redis Streams deployment. Redis is a delivery mechanism,
   not the only copy of accepted task intent; reconcile incomplete tasks from
   PostgreSQL/Outbox before enabling workers.
6. Start one canary application pod with outbound GitHub/model publication
   disabled. Verify `/ready`, task/session reads, audit writes, and SLO metrics.
7. Enable workers, then external effects, then traffic in that order. Watch
   Outbox age, queue age, DLQ, 5xx burn rate, and duplicate-effect receipts.
8. Record actual data-loss interval and recovery time. Keep the old database
   isolated until the incident owner approves disposal.

Queue reconciliation after a regional Redis loss and automated cross-region
failover are deliberately separate follow-ups. Until both are exercised, this
milestone proves database recoverability rather than complete service DR.

## Failure handling

- A checksum/content mismatch is a failed restore. Do not waive it or start the
  service against the target.
- If cleanup fails, the command fails and reports the generated database name.
  Remove only a name that matches the generated prefix after verifying active
  sessions.
- A missed RPO/RTO exits `1`; tool/input/restore errors exit `2`.
- Keep failed manifests and tool logs, but never paste connection strings or
  database artifacts into tickets.
