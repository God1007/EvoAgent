"""Dedicated schema migration entry point for deployment jobs/init containers."""

import json
import sys

from .config import Settings
from .migrations import SchemaMigrationError
from .postgres_store import create_store


def run() -> None:
    settings = Settings.from_env()
    store = None
    try:
        store = create_store(
            settings.database_url,
            settings.db_path,
            settings.pg_pool_min,
            settings.pg_pool_max,
            settings.pg_pool_timeout,
        )
        print(
            json.dumps(
                {
                    "status": "migrated",
                    "backend": "postgresql" if settings.database_url else "sqlite",
                    "schema_version": store.schema_version(),
                },
                sort_keys=True,
            )
        )
    except SchemaMigrationError as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    run()
