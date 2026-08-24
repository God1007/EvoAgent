"""Dedicated schema migration entry point for deployment jobs/init containers."""

import json
import sys

from .config import Settings
from .migrations import SchemaMigrationError
from .postgres_store import create_store


def run() -> None:
    store = None
    try:
        settings = Settings.from_env()
        store = create_store(
            settings.database_url,
            settings.pg_pool_min,
            settings.pg_pool_max,
            settings.pg_pool_timeout,
            settings.pg_statement_timeout_seconds,
            auto_migrate=True,
        )
        print(
            json.dumps(
                {
                    "status": "migrated",
                    "backend": "postgresql",
                    "schema_version": store.schema_version(),
                },
                sort_keys=True,
            )
        )
    except (ValueError, SchemaMigrationError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "schema migration failed (%s)" % type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    run()
