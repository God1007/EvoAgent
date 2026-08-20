"""PostgreSQL backup/restore drills with machine-readable recovery evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS
from .postgres_store import PostgresTaskStore

REPORT_SCHEMA_VERSION = 1
_DRILL_DATABASE = re.compile(r"^evoagent_drill_[a-f0-9]{32}$")
_CHUNK_SIZE = 1000


class DisasterRecoveryError(RuntimeError):
    """A drill failed without ever making the source database a restore target."""


@dataclass(frozen=True)
class TableFingerprint:
    row_count: int
    content_sha256: str


@dataclass(frozen=True)
class DatabaseFingerprint:
    schema_sha256: str
    schema_version: int
    tables: dict[str, TableFingerprint]

    @property
    def total_rows(self) -> int:
        return sum(item.row_count for item in self.tables.values())


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": bytes(value).hex()}
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return str(value)


def _row_digest(columns: tuple[str, ...], row: Any) -> bytes:
    if hasattr(row, "keys"):
        payload = {column: _canonical(row[column]) for column in columns}
    else:
        payload = {column: _canonical(row[index]) for index, column in enumerate(columns)}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _fingerprint_cursor(cursor: Any) -> TableFingerprint:
    columns = tuple(
        str(item.name if hasattr(item, "name") else item[0]) for item in cursor.description
    )
    count = 0
    modulus = 1 << 256
    xor = 0
    total = 0
    square_total = 0
    while rows := cursor.fetchmany(_CHUNK_SIZE):
        count += len(rows)
        for row in rows:
            numeric = int.from_bytes(_row_digest(columns, row), "big")
            xor ^= numeric
            total = (total + numeric) % modulus
            square_total = (square_total + numeric * numeric) % modulus
    # Three commutative accumulators plus the count make the result independent
    # of physical/query order without retaining every row digest in memory.
    digest = hashlib.sha256()
    digest.update(count.to_bytes(16, "big"))
    digest.update(xor.to_bytes(32, "big"))
    digest.update(total.to_bytes(32, "big"))
    digest.update(square_total.to_bytes(32, "big"))
    return TableFingerprint(count, digest.hexdigest())


def _validate_migration_history(rows: list[dict[str, Any]]) -> int:
    expected = {migration.version: migration for migration in MIGRATIONS}
    versions = [int(row["version"]) for row in rows]
    if versions != list(range(1, CURRENT_SCHEMA_VERSION + 1)):
        raise DisasterRecoveryError(
            "restored schema history is incomplete or newer than this release"
        )
    for row in rows:
        migration = expected[int(row["version"])]
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise DisasterRecoveryError(
                "restored schema migration %d failed checksum validation" % migration.version
            )
    return versions[-1]


def _compare_fingerprints(source: DatabaseFingerprint, restored: DatabaseFingerprint) -> None:
    if source != restored:
        raise DisasterRecoveryError("restored database content or schema differs from the snapshot")


def _secure_output_directory(path: str) -> str:
    output = os.path.abspath(path)
    if os.path.exists(output) and not os.path.isdir(output):
        raise ValueError("DR output path must be a directory")
    if not os.path.exists(output):
        os.makedirs(output, mode=0o700)
    return output


def _write_manifest(path: str, report: dict[str, Any]) -> None:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.write(b"\n")


def _objective_report(
    recovery_point_age: float,
    recovery_time: float,
    max_rpo_seconds: float,
    max_rto_seconds: float,
) -> dict[str, Any]:
    return {
        "rpo": {
            "objective_seconds": max_rpo_seconds,
            "observed_snapshot_age_seconds": round(recovery_point_age, 6),
            "met": recovery_point_age <= max_rpo_seconds,
        },
        "rto": {
            "objective_seconds": max_rto_seconds,
            "observed_restore_and_validation_seconds": round(recovery_time, 6),
            "met": recovery_time <= max_rto_seconds,
        },
    }


def _postgres_parts(url: str) -> tuple[dict[str, str], str, dict[str, str]]:
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise DisasterRecoveryError("PostgreSQL DR requires psycopg") from exc
    try:
        parts = {key: str(value) for key, value in conninfo_to_dict(url).items()}
    except Exception as exc:
        raise ValueError("invalid PostgreSQL connection string") from exc
    database = parts.get("dbname", "")
    if not database:
        raise ValueError("PostgreSQL connection string must name a source database")
    command_parts = {key: value for key, value in parts.items() if key != "password"}
    environment = dict(os.environ)
    environment.pop("EVOAGENT_DATABASE_URL", None)
    if "password" in parts:
        environment["PGPASSWORD"] = parts["password"]
    return parts, make_conninfo(**command_parts), environment


def _postgres_target_url(parts: dict[str, str], database: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(**{**parts, "dbname": database})


def _run_pg_tool(
    executable: str,
    arguments: list[str],
    environment: dict[str, str],
    timeout_seconds: float,
) -> None:
    resolved = shutil.which(executable) if not os.path.isabs(executable) else executable
    if not resolved or not os.path.isfile(resolved):
        raise DisasterRecoveryError("required PostgreSQL tool is unavailable: %s" % executable)
    try:
        completed = subprocess.run(
            [resolved, *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DisasterRecoveryError("PostgreSQL backup/restore command timed out") from exc
    if completed.returncode:
        detail = completed.stderr.strip().replace("\n", " ")[:1000]
        raise DisasterRecoveryError(
            "PostgreSQL backup/restore command failed: %s" % (detail or "unknown error")
        )


def _run_pg_dump_bounded(
    executable: str,
    arguments: list[str],
    artifact: str,
    environment: dict[str, str],
    timeout_seconds: float,
    max_backup_bytes: int,
) -> None:
    """Stream pg_dump to a private file while enforcing time and size limits."""
    resolved = shutil.which(executable) if not os.path.isabs(executable) else executable
    if not resolved or not os.path.isfile(resolved):
        raise DisasterRecoveryError("required PostgreSQL tool is unavailable: %s" % executable)
    deadline = time.monotonic() + timeout_seconds
    failure = ""
    with open(artifact, "xb") as output, tempfile.TemporaryFile() as errors:
        os.chmod(artifact, 0o600)
        process = subprocess.Popen(
            [resolved, *arguments],
            env=environment,
            stdout=output,
            stderr=errors,
        )
        while process.poll() is None:
            if time.monotonic() >= deadline:
                failure = "PostgreSQL backup command timed out"
                process.terminate()
                break
            output.flush()
            if os.path.getsize(artifact) > max_backup_bytes:
                failure = "PostgreSQL backup artifact exceeds the configured byte limit"
                process.terminate()
                break
            time.sleep(0.05)
        if failure:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        else:
            process.wait()
        output.flush()
        errors.seek(0)
        detail = errors.read(1001).decode("utf-8", "replace").strip().replace("\n", " ")
        if len(detail) > 1000:
            detail = detail[:1000]
        if not failure and process.returncode:
            failure = "PostgreSQL backup command failed: %s" % (detail or "unknown error")
    if not failure and os.path.getsize(artifact) > max_backup_bytes:
        failure = "PostgreSQL backup artifact exceeds the configured byte limit"
    if failure:
        if os.path.exists(artifact):
            os.unlink(artifact)
        raise DisasterRecoveryError(failure)


def _postgres_fingerprint(connection: Any) -> DatabaseFingerprint:
    from psycopg import sql

    tables = [
        str(row["tablename"])
        for row in connection.execute(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
    ]
    schema_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT c.relname AS table_name,a.attnum,a.attname AS column_name,"
            "pg_catalog.format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull,"
            "COALESCE(pg_catalog.pg_get_expr(d.adbin,d.adrelid),'') AS default_expression "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
            "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p') "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"
        ).fetchall()
    ]
    schema_rows.extend(
        {
            "object_type": "constraint",
            **dict(row),
        }
        for row in connection.execute(
            "SELECT c.relname AS table_name,k.conname AS object_name,"
            "pg_catalog.pg_get_constraintdef(k.oid,TRUE) AS definition "
            "FROM pg_catalog.pg_constraint k "
            "JOIN pg_catalog.pg_class c ON c.oid=k.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' ORDER BY c.relname,k.conname"
        ).fetchall()
    )
    schema_rows.extend(
        {
            "object_type": "index",
            **dict(row),
        }
        for row in connection.execute(
            "SELECT tablename AS table_name,indexname AS object_name,indexdef AS definition "
            "FROM pg_catalog.pg_indexes WHERE schemaname='public' "
            "ORDER BY tablename,indexname"
        ).fetchall()
    )
    schema_rows.extend(
        {
            "object_type": "extension",
            **dict(row),
        }
        for row in connection.execute(
            "SELECT extname AS object_name,extversion AS version "
            "FROM pg_catalog.pg_extension WHERE extname<>'plpgsql' ORDER BY extname"
        ).fetchall()
    )
    schema_digest = hashlib.sha256(
        json.dumps(
            _canonical(schema_rows),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    fingerprints: dict[str, TableFingerprint] = {}
    for table in tables:
        cursor = connection.cursor()
        try:
            cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
            fingerprints[table] = _fingerprint_cursor(cursor)
        finally:
            cursor.close()
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    schema_version = _validate_migration_history(rows)
    return DatabaseFingerprint(schema_digest, schema_version, fingerprints)


def _create_drill_database(admin_connection: Any, database: str) -> None:
    if not _DRILL_DATABASE.fullmatch(database):
        raise DisasterRecoveryError("refusing to create a non-generated DR database")
    from psycopg import sql

    admin_connection.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
    )


def _drop_drill_database(admin_connection: Any, database: str) -> None:
    if not _DRILL_DATABASE.fullmatch(database):
        raise DisasterRecoveryError("refusing to drop a non-generated DR database")
    from psycopg import sql

    admin_connection.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname=%s AND pid<>pg_backend_pid()",
        (database,),
    )
    admin_connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def run_postgres_drill(
    database_url: str,
    output_dir: str,
    max_rpo_seconds: float = 3600.0,
    max_rto_seconds: float = 900.0,
    command_timeout_seconds: float = 900.0,
    max_backup_bytes: int = 20 * 1024 * 1024 * 1024,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
) -> dict[str, Any]:
    """Dump one MVCC snapshot and restore it into a generated database."""
    if min(max_rpo_seconds, max_rto_seconds, command_timeout_seconds, max_backup_bytes) <= 0:
        raise ValueError("DR limits and objectives must be positive")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise DisasterRecoveryError("PostgreSQL DR requires psycopg") from exc

    parts, command_conninfo, environment = _postgres_parts(database_url)
    source_database = parts["dbname"]
    target_database = "evoagent_drill_" + uuid.uuid4().hex
    if target_database == source_database or not _DRILL_DATABASE.fullmatch(target_database):
        raise DisasterRecoveryError("generated restore target failed the safety policy")
    admin_url = _postgres_target_url(parts, "postgres")
    target_url = _postgres_target_url(parts, target_database)
    target_command_parts, target_command_conninfo, target_environment = _postgres_parts(target_url)
    del target_command_parts
    output = _secure_output_directory(output_dir)
    drill_id = uuid.uuid4().hex
    artifact = os.path.join(output, "evoagent-%s.dump" % drill_id)
    manifest = os.path.join(output, "evoagent-%s.manifest.json" % drill_id)
    started_at = _now()
    backup_started = time.monotonic()

    with psycopg.connect(database_url, row_factory=dict_row) as source_connection:
        source_connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot_row = source_connection.execute(
            "SELECT pg_export_snapshot() AS snapshot_id"
        ).fetchone()
        if snapshot_row is None:  # pragma: no cover - PostgreSQL contract
            raise DisasterRecoveryError("PostgreSQL did not export a recovery snapshot")
        snapshot_id = str(snapshot_row["snapshot_id"])
        snapshot_captured_at = _now()
        source_fingerprint = _postgres_fingerprint(source_connection)
        _run_pg_dump_bounded(
            pg_dump,
            [
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--snapshot=" + snapshot_id,
                "--dbname=" + command_conninfo,
            ],
            artifact,
            environment,
            command_timeout_seconds,
            max_backup_bytes,
        )
    artifact_size = os.path.getsize(artifact)
    artifact_sha256 = _sha256_file(artifact)
    backup_seconds = time.monotonic() - backup_started

    restore_started_at = _now()
    restore_started = time.monotonic()
    target_created = False
    target_removed = False
    validation_error: Exception | None = None
    restored_fingerprint: DatabaseFingerprint | None = None
    try:
        with psycopg.connect(admin_url, autocommit=True) as admin_connection:
            _create_drill_database(admin_connection, target_database)
            target_created = True
        _run_pg_tool(
            pg_restore,
            [
                "--exit-on-error",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--dbname=" + target_command_conninfo,
                artifact,
            ],
            target_environment,
            command_timeout_seconds,
        )
        with psycopg.connect(target_url, row_factory=dict_row) as target_connection:
            restored_fingerprint = _postgres_fingerprint(target_connection)
        _compare_fingerprints(source_fingerprint, restored_fingerprint)
        # Avoid background pool threads in the short-lived privileged admin job;
        # pool behavior has its own adapter contract suite.
        restored_store = PostgresTaskStore(
            target_url,
            pool_min=0,
            pool_max=0,
            pool_timeout=5,
            auto_migrate=False,
        )
        try:
            restored_store.ping()
            restored_store.dashboard_stats()
            restored_store.audit(
                "drill",
                "evoagent-dr",
                "recovery.smoke",
                drill_id,
                {"backend": "postgresql"},
            )
        finally:
            restored_store.close()
    except Exception as exc:
        validation_error = exc
    finally:
        if target_created:
            try:
                with psycopg.connect(admin_url, autocommit=True) as admin_connection:
                    _drop_drill_database(admin_connection, target_database)
                target_removed = True
            except Exception as cleanup_error:
                if validation_error is None:
                    validation_error = DisasterRecoveryError(
                        "restored database cleanup failed: %s" % cleanup_error
                    )
    if validation_error is not None:
        if isinstance(validation_error, DisasterRecoveryError):
            raise validation_error
        raise DisasterRecoveryError("PostgreSQL recovery validation failed: %s" % validation_error)
    if restored_fingerprint is None:  # pragma: no cover - defensive invariant
        raise DisasterRecoveryError("PostgreSQL recovery produced no fingerprint")

    validated_at = _now()
    recovery_seconds = time.monotonic() - restore_started
    snapshot_age_seconds = (validated_at - snapshot_captured_at).total_seconds()
    objectives = _objective_report(
        snapshot_age_seconds, recovery_seconds, max_rpo_seconds, max_rto_seconds
    )
    status = "pass" if all(item["met"] for item in objectives.values()) else "fail"
    source_identity = {
        "host": parts.get("host", "local-socket"),
        "port": parts.get("port", "5432"),
        "database": source_database,
        "user": parts.get("user", ""),
    }
    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "backend": "postgresql",
        "drill_id": drill_id,
        "source": source_identity,
        "started_at": _timestamp(started_at),
        "snapshot_captured_at": _timestamp(snapshot_captured_at),
        "restore_started_at": _timestamp(restore_started_at),
        "validated_at": _timestamp(validated_at),
        "backup_duration_seconds": round(backup_seconds, 6),
        "artifact": {
            "path": artifact,
            "bytes": artifact_size,
            "sha256": artifact_sha256,
            "format": "pg_dump-custom",
        },
        "manifest_path": manifest,
        "integrity": {
            "schema_version": source_fingerprint.schema_version,
            "schema_sha256": source_fingerprint.schema_sha256,
            "table_count": len(source_fingerprint.tables),
            "total_rows": source_fingerprint.total_rows,
            "source_fingerprint": asdict(source_fingerprint),
            "restored_fingerprint": asdict(restored_fingerprint),
            "application_smoke": "pass",
        },
        "objectives": objectives,
        "cleanup": {
            "generated_database": target_database,
            "restored_database_removed": target_removed,
        },
    }
    _write_manifest(manifest, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an EvoAgent backup/restore integrity drill")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-rpo-seconds", type=float, default=3600.0)
    parser.add_argument("--max-rto-seconds", type=float, default=900.0)
    parser.add_argument("--command-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-backup-bytes", type=int, default=20 * 1024 * 1024 * 1024)
    parser.add_argument("--pg-dump", default="pg_dump")
    parser.add_argument("--pg-restore", default="pg_restore")
    args = parser.parse_args(argv)
    database_url = os.getenv("EVOAGENT_DATABASE_URL", "")
    try:
        if not database_url:
            raise ValueError("EVOAGENT_DATABASE_URL is required for PostgreSQL DR")
        report = run_postgres_drill(
            database_url,
            args.output_dir,
            args.max_rpo_seconds,
            args.max_rto_seconds,
            args.command_timeout_seconds,
            args.max_backup_bytes,
            args.pg_dump,
            args.pg_restore,
        )
    except (ValueError, OSError, DisasterRecoveryError) as exc:
        print(
            json.dumps(
                {
                    "report_schema_version": REPORT_SCHEMA_VERSION,
                    "status": "error",
                    "backend": "postgresql",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # pragma: no cover - external driver boundary
        print(
            json.dumps(
                {
                    "report_schema_version": REPORT_SCHEMA_VERSION,
                    "status": "error",
                    "backend": "postgresql",
                    "error": "recovery drill failed (%s)" % type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
