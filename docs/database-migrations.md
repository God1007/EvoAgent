# Database migrations

EvoAgent supports PostgreSQL only. Its forward-only migration catalog lives in
`evoagent/migrations.py`; applied versions, names and SHA-256 checksums live in
`schema_migrations`.

The dedicated `evoagent-migrate` process:

1. acquires a PostgreSQL transaction advisory lock;
2. verifies that recorded versions are contiguous and match the catalog;
3. refuses a schema created by a newer application;
4. applies pending migrations transactionally.

Run it without arguments to apply migrations. `--help` prints usage without
reading configuration or connecting to PostgreSQL. Other arguments, including
`--dry-run`, are unsupported and rejected with exit code 2 before any database
access; they must never be silently treated as an apply request.

The application process never applies DDL. It validates the complete migration
history during startup and refuses to serve with a missing, pending, changed or
newer schema. It exposes the active version through `/ready`.

Each readiness probe revalidates the live PostgreSQL history rather than
returning a startup cache. An older replica therefore becomes unready after a
new release commits a schema version or checksum it cannot understand.

Never edit an existing released migration. Add a new version and keep old names
and checksums stable. Version 15 removes the retired model usage, shadow-route
and route-capacity tables. Version 17 adds rollout generations plus
database-enforced deployment bounds and state invariants. It intentionally
refuses to migrate if a legacy deployment row is invalid; audit and correct that
row through a controlled operation before retrying the migration.
Version 18 records whether each Prompt version is approved, rejected, deferred,
or legacy so rollout admission cannot mistake evaluation history for production
authorization. The active pre-v18 baseline becomes `legacy`; other versions
inherit their immutable evolution-run decision, while unclassified inactive
versions become `rejected`. Omitted qualifications default to `rejected`, and
PostgreSQL rejects every new global activation during a mixed-version rollout.
Version 19 adds the monotonic credential version used to revoke every existing
JWT and signed OAuth state immediately after a password change.
Version 20 records the last GitHub PR event time on each review session so a
late delivery cannot reverse a newer closed, draft or reopened state.
Version 23 indexes the latest evaluation revision lookup used to keep Prompt
rollouts on the application replica's execution revision.
Version 24 adds the partial completion-time index used for bounded effect-receipt
retention; it does not delete rows until operators explicitly enable retention.
Version 25 indexes webhook receipt time for the same opt-in retention path.
Version 26 adds user-authored Studio drafts, immutable publications and repository bindings.
Version 27 pins every existing Studio binding to its currently effective published
version and adds a monotonic binding revision. A tenant-scoped composite foreign
key requires that the selected publication exists; missing or invalid legacy
targets fail the migration transaction instead of being silently unbound.
Unbinding keeps a row with both target fields null and an incremented revision,
so stale clients cannot overwrite a later configuration after an unbind/rebind.

For the v26-to-v27 transition, drain and stop all old API/worker replicas **before**
migrating: old code follows the latest publication, whereas new code follows the
pinned version. Readiness failure alone does not stop an already-running old
process from accepting requests or publishing. Stop ingress and writers, back up,
migrate once, then start only the new application. Rollback of a workflow version
is a normal versioned binding change, not a schema downgrade. Older queued tasks
retain their original embedded workflow snapshot.

Version 28 changes only `checkpoints.state_json` from JSONB to JSON. JSONB
normalizes numbers such as `-0.0` and `1e20`; reading them back can change their
JSON encoding and invalidate an otherwise correct handoff digest. JSON preserves
the serialized number representation without changing the handoff protocol or
hash rules. Existing field projections continue to work; other JSONB columns are
unchanged.

Before upgrading to v28, stop ingress and drain/stop every old API and worker,
then back up and migrate. Old writers explicitly cast checkpoints to JSONB and
would still normalize new values even after the column changes. Do not run a
mixed-version rollout. The column conversion can rewrite the checkpoint table
and takes an exclusive lock: rehearse on a restored copy, reserve maintenance
time and enough storage for the rewrite and its WAL, and verify `/ready` before
reopening ingress with only the new application.

The migration preserves existing checkpoints, attempts and digests; it cannot
reconstruct number representations already lost in JSONB. A historical digest
mismatch still fails closed. Do not repair it by recomputing the stored hash or
rerunning a completed node. Investigate the original task and any external
effects before explicitly submitting a new review. There is no automatic repair
or schema downgrade; application-revision checks still govern task resumption.

## Commands

```bash
export EVOAGENT_DATABASE_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent'
evoagent-migrate
```

Before deployment, back up PostgreSQL and run the new image against a disposable
copy. Then run one migration job before starting the new application replicas;
the advisory lock makes a retried job safe. Give the migration job a DDL-capable
database role and the application replicas a separate runtime role without DDL.
Docker Compose models this ordering with its one-shot `migrate` service. There
is no automatic downgrade. Roll back application code only when it understands
the current schema.
