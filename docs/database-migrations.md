# Database migration operations

EvoAgent uses one forward-only logical migration history for SQLite and
PostgreSQL. Dialect-specific statements live in `evoagent/migrations.py`; each
logical version has an immutable name and SHA-256 checksum recorded in
`schema_migrations`.

## Startup behavior

On Store activation EvoAgent:

1. acquires an SQLite immediate write lock or PostgreSQL transaction advisory
   lock;
2. creates the migration-history table if needed;
3. verifies that recorded versions are contiguous and their names/checksums
   match the application catalog;
4. refuses startup when the database contains a newer schema version;
5. applies all pending versions in a transaction and records each version;
6. exposes the active version in `/ready` as `checks.schema_version`.

An older database created before version tracking is not rebuilt. Idempotent
migrations inspect/add legacy columns, create missing objects, preserve rows,
and then adopt the database into the checksummed history.

Multiple instances may start concurrently: the migration lock serializes the
schema transition. A failed SQLite migration rolls back both its DDL and
history record; PostgreSQL migrations run in the connection transaction and
are likewise rolled back by the adapter on startup failure.

## Deployment command

Run migrations as a dedicated deployment job or init container before rolling
out API/worker instances:

```bash
EVOAGENT_DATABASE_URL=postgresql://... evoagent-migrate
# or
python -m evoagent.migrate
```

Successful output is machine-readable and contains no database credentials:

```json
{"backend": "postgresql", "schema_version": 3, "status": "migrated"}
```

Schema compatibility errors exit with status `2`. Normal application startup
also runs the same migration check, so bypassing the deployment job cannot
silently start against an incompatible schema.

## Pre-deployment backup

For PostgreSQL, capture and validate a logical backup before applying a new
schema version:

```bash
pg_dump --format=custom --file=evoagent-before-vNEXT.dump "$EVOAGENT_DATABASE_URL"
pg_restore --list evoagent-before-vNEXT.dump >/dev/null
```

For SQLite, stop writers (or use the SQLite online backup API) and copy the
database plus any `-wal`/`-shm` state as one consistent backup. Do not copy only
the main file while WAL writers are active.

Record the application image digest, current schema version, backup location,
backup checksum, and restore owner in the change ticket.

## Rollback and restore

Migrations are forward-only; application rollback and database restore are
separate decisions.

- If a migration is additive and the previous application supports the new
  schema, roll back the application image without changing the database.
- If the previous application reports `SchemaTooNewError`, do not delete rows
  from `schema_migrations` and do not edit checksums. Redeploy the compatible
  application or restore the complete pre-migration backup into a new database.
- Point the service at the restored database, run `evoagent-migrate`, execute
  smoke/consistency checks, and only then resume intake.

PostgreSQL restore example (to a new empty database):

```bash
createdb evoagent_restore
pg_restore --no-owner --dbname=evoagent_restore evoagent-before-vNEXT.dump
EVOAGENT_DATABASE_URL=postgresql://.../evoagent_restore evoagent-migrate
```

Never attempt rollback by manually changing the migration version: the
checksum guard intentionally treats this as history corruption.

## Adding a migration

1. Append one `Migration` with the next integer version; never edit a released
   migration.
2. Prefer additive/expand changes that are safe during rolling deployment.
3. Add both SQLite and PostgreSQL statements plus SQLite legacy-column metadata
   where required.
4. Test fresh install, upgrade from the previous version, legacy adoption,
   newer-version refusal, checksum refusal, concurrency, and injected failure
   rollback.
5. Add the corresponding contract/integration behavior before changing domain
   code to depend on the new schema.
6. Document backup, restore, and any later contract/column removal phase.

Destructive contract changes use expand/migrate/contract across separate
releases. The first release adds new structures, a background/backfill step
migrates data with observable progress, and only a later release removes the
old structure after all readers have moved.
