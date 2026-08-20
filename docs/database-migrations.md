# Database migrations

EvoAgent supports PostgreSQL only. Its forward-only migration catalog lives in
`evoagent/migrations.py`; applied versions, names and SHA-256 checksums live in
`schema_migrations`.

At startup the store:

1. acquires a PostgreSQL transaction advisory lock;
2. verifies that recorded versions are contiguous and match the catalog;
3. refuses a schema created by a newer application;
4. applies pending migrations transactionally;
5. exposes the active version through `/ready`.

Never edit an existing released migration. Add a new version and keep old names
and checksums stable. Version 15 removes the retired model usage, shadow-route
and route-capacity tables.

## Commands

```bash
export EVOAGENT_DATABASE_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent'
evoagent-migrate
```

Before deployment, back up PostgreSQL and run the new image against a disposable
copy. Multiple instances may start together; the advisory lock serializes the
schema transition. There is no automatic downgrade. Roll back application code
only when it understands the current schema.
