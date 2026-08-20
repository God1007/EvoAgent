import os
import unittest

from evoagent.postgres_store import PostgresTaskStore


def postgres_url(testcase: unittest.TestCase) -> str:
    url = os.getenv("EVOAGENT_TEST_POSTGRES_URL", "")
    if not url:
        testcase.skipTest("EVOAGENT_TEST_POSTGRES_URL is not configured")
    return url


def reset_postgres(url: str) -> None:
    store = PostgresTaskStore(url, pool_min=0, pool_max=0)
    try:
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
                "AND table_name<>'schema_migrations'"
            ).fetchall()
            if rows:
                sql = store.psycopg.sql
                tables = sql.SQL(", ").join(sql.Identifier(row["table_name"]) for row in rows)
                conn.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(tables))
    finally:
        store.close()


def postgres_store(testcase: unittest.TestCase) -> PostgresTaskStore:
    url = postgres_url(testcase)
    reset_postgres(url)
    store = PostgresTaskStore(url, pool_min=0, pool_max=0)
    testcase.addCleanup(store.close)
    return store
