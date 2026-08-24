# Database migrations

EvoAgent supports PostgreSQL only. Its forward-only migration catalog lives in
`evoagent/migrations.py`; applied versions, names and SHA-256 checksums live in
`schema_migrations`.

The dedicated `evoagent-migrate` process:

1. acquires a PostgreSQL transaction advisory lock;
2. verifies that recorded versions are contiguous and match the catalog;
3. refuses a schema created by a newer application;
4. applies pending migrations transactionally.

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
