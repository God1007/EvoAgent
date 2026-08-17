"""CI probe for the real PostgreSQL -> recovery -> Outbox -> Redis path."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import redis

from evoagent.outbox import OutboxDispatcher
from evoagent.postgres_store import PostgresTaskStore
from evoagent.task_queue import TaskQueue


def _required_environment(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError("%s is required" % name)
    return value


def _store() -> PostgresTaskStore:
    return PostgresTaskStore(
        _required_environment("EVOAGENT_DATABASE_URL"),
        pool_min=0,
        pool_max=0,
        auto_migrate=False,
    )


def seed_published_task(task_id: str) -> dict[str, Any]:
    """Create durable work whose original Outbox record already says published."""
    store = _store()
    try:
        store.create_review_task(
            task_id,
            "ci/regional-recovery",
            91,
            {"source": "recovery-ci"},
            "default",
            "--- a/app.py\n+++ b/app.py\n+recovered = True\n",
            {
                "task_id": task_id,
                "repository": "ci/regional-recovery",
                "pull_request": 91,
                "tenant_id": "default",
            },
        )
        found = False
        for _ in range(20):
            messages = store.claim_outbox("ci-recovery-seed", 500, 30, 20)
            if not messages:
                break
            for message in messages:
                found = found or message["payload"].get("task_id") == task_id
                if not store.mark_outbox_published(message["id"], "ci-recovery-seed"):
                    raise RuntimeError("lost the Outbox lease while preparing recovery evidence")
            if found:
                break
        if not found:
            raise RuntimeError("recovery fixture Outbox row was not published")
        return {"status": "seeded", "task_id": task_id}
    finally:
        store.close()


def verify_publication(task_id: str, report_path: Path) -> dict[str, Any]:
    """Dispatch restaged intents and prove the selected task reached Redis Streams."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    redis_url = _required_environment("EVOAGENT_REDIS_URL")
    store = _store()
    queue = None
    drained = False
    try:
        queue = TaskQueue(lambda _payload: None, workers=1, redis_url=redis_url)
        dispatcher = OutboxDispatcher(store, queue, batch_size=500, autostart=False)
        published = 0
        for _ in range(100):
            count = dispatcher.dispatch_once()
            published += count
            if count == 0:
                break
        if published < int(report["staging"]["staged"]):
            raise RuntimeError("not every staged recovery intent was published")
    finally:
        try:
            if queue is not None:
                drained = queue.close(5)
        finally:
            store.close()
    if not drained:
        raise RuntimeError("queue workers did not drain during the recovery probe")

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        task_ids = {
            json.loads(fields["envelope"])["payload"]["task_id"]
            for _entry_id, fields in client.xrange(TaskQueue.STREAM)
        }
    finally:
        client.close()
    if task_id not in task_ids:
        raise RuntimeError("restaged recovery task did not reach Redis Streams")
    return {"status": "published", "task_id": task_id, "published": published}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exercise EvoAgent queue-recovery integration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed")
    seed.add_argument("--task-id", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--task-id", required=True)
    verify.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    result = (
        seed_published_task(args.task_id)
        if args.command == "seed"
        else verify_publication(args.task_id, args.report)
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
