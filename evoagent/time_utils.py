from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def utc_after(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
