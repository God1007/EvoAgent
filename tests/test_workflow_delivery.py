"""Full service handoffs over disposable PostgreSQL and Redis instances."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from evoagent.agents import FINDINGS_TYPE, review_workflow
from evoagent.config import Settings
from evoagent.recovery import RECOVERY_MARKER
from evoagent.service import ReviewService
from evoagent.task_queue import TaskQueue
from evoagent.workflow import AgentSpec, Step, Workflow
from tests.db_support import postgres_store

POSTGRES_URL = os.getenv("EVOAGENT_TEST_POSTGRES_URL", "")
REDIS_URL = os.getenv("EVOAGENT_TEST_REDIS_URL", "")
REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DIFF = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+eval(value)\n"


def _settings(skills_dir):
    return Settings(
        host="127.0.0.1",
        port=8080,
        max_diff_bytes=10000,
        max_steps=8,
        timeout_seconds=20,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        github_webhook_secret="",
        github_token="",
        auto_post_review=False,
        database_url=POSTGRES_URL,
        redis_url=REDIS_URL,
        skills_dir=skills_dir,
        async_workers=1,
        queue_lease_seconds=1,
        queue_shutdown_timeout_seconds=5,
        outbox_lease_seconds=1,
        pg_pool_min=0,
        pg_pool_max=3,
    )


def _paused_workflow(catalog):
    default = review_workflow(catalog)

    def pause_first_attempt(handoff):
        if handoff.attempt == 1:
            # The parent kills this worker after the running checkpoint commits.
            threading.Event().wait(30)
            raise AssertionError("fault-injection worker was not terminated")
        return {"findings": handoff.inputs["findings"]}

    receiver = AgentSpec(
        "crash-receiver",
        REVISION,
        {"findings": FINDINGS_TYPE},
        {"findings": FINDINGS_TYPE},
        pause_first_attempt,
    )
    return Workflow(
        "crash-recovery",
        default.inputs,
        (*default.steps, Step("receiver", receiver, {"findings": default.outputs["verified"]})),
        {"verified": "receiver.findings"},
    )


def _crash_worker():
    service = ReviewService(
        _settings(os.environ["EVOAGENT_TEST_SKILLS_DIR"]), workflow_factory=_paused_workflow
    )
    try:
        service.enqueue_review("demo/repo", DIFF, 7)
        threading.Event().wait(30)
    finally:
        service.close()


def _wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(POSTGRES_URL and REDIS_URL, "disposable PostgreSQL and Redis are required")
class WorkflowDeliveryTests(unittest.TestCase):
    def setUp(self):
        import redis

        self.store = postgres_store(self)
        self.redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        self.addCleanup(self.redis.close)
        self.addCleanup(self._clear_queue)
        self._clear_queue()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = _settings(directory.name)
        self.settings.validate_evolution()

    def _clear_queue(self):
        keys = [TaskQueue.STREAM, TaskQueue.DLQ, *self.redis.scan_iter(TaskQueue.DEDUP + "*")]
        self.redis.delete(*keys)

    @staticmethod
    def _stop_worker(worker):
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)

    def test_new_worker_reclaims_crashed_agent_without_repeating_completed_handoffs(self):
        self._resume_crashed_worker(rebuild=False)

    def test_lost_queue_is_rebuilt_by_cli_without_repeating_completed_handoffs(self):
        self._resume_crashed_worker(rebuild=True)

    def _resume_crashed_worker(self, rebuild):
        environment = {
            **{key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ},
            "EVOAGENT_TEST_POSTGRES_URL": POSTGRES_URL,
            "EVOAGENT_TEST_REDIS_URL": REDIS_URL,
            "EVOAGENT_TEST_SKILLS_DIR": self.settings.skills_dir,
        }
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tests.test_workflow_delivery import _crash_worker; _crash_worker()",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop_worker, worker)

        def paused():
            self.assertIsNone(worker.poll(), "worker exited before reaching the handoff")
            tasks = self.store.list_tasks()
            if not tasks:
                return False
            snapshot = self.store.workflow_status(tasks[0]["id"], "default")
            return snapshot["steps"] and snapshot["steps"][-1]["status"] == "running"

        self.assertTrue(_wait(paused), self.store.list_tasks())
        self.assertIsNone(worker.poll())
        task_id = self.store.list_tasks()[0]["id"]
        before = self.store.load_checkpoints(task_id)
        self.assertEqual(
            7,
            sum(
                node.startswith("workflow:") and checkpoint["status"] == "completed"
                for node, checkpoint in before.items()
            ),
        )
        self.assertNotIn("outputs", before["workflow:receiver"]["state"])
        messages = self.store.get(task_id)["collaboration"]
        self.assertEqual(1, self.redis.xpending(TaskQueue.STREAM, TaskQueue.GROUP)["pending"])
        self.assertTrue(_wait(lambda: len(self.store.list_outbox("published")) == 1))
        self._stop_worker(worker)
        self.assertNotEqual(0, worker.returncode)

        if rebuild:
            recovery_id = str(uuid.uuid4())

            def recover(*arguments, code=0):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "evoagent.recovery",
                        "--recovery-id",
                        recovery_id,
                        "--confirm-database",
                        self.store.connected_database_name(),
                        *arguments,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    env={
                        **environment,
                        "EVOAGENT_DATABASE_URL": POSTGRES_URL,
                        "EVOAGENT_REDIS_URL": REDIS_URL,
                    },
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(code, result.returncode, result.stderr)
                return json.loads(result.stdout if code == 0 else result.stderr)

            history = self.store.list_outbox("published")
            self.assertEqual(1, len(history))
            self.assertIn("not empty", recover(code=2)["error"])
            # Delete only this fixture's queue keys, never FLUSHDB or unrelated state.
            self._clear_queue()
            self.assertEqual(0, self.redis.dbsize(), "recovery requires a dedicated empty Redis")
            self.addCleanup(self.redis.delete, RECOVERY_MARKER)
            planned = recover()
            self.assertEqual("planned", planned["status"])
            self.assertEqual(1, planned["plan"]["recoverable"])
            self.assertEqual(0, planned["plan"]["unrecoverable"])
            self.assertIn(
                "does not match",
                recover("--apply", "--expect-plan-sha256", "0" * 64, code=2)["error"],
            )
            self.assertEqual(0, self.redis.dbsize())
            self.assertEqual(history, self.store.list_outbox("published"))
            self.assertEqual([], self.store.list_outbox("pending"))
            arguments = ("--apply", "--expect-plan-sha256", planned["plan"]["plan_sha256"])
            applied = recover(*arguments)
            self.assertEqual(1, applied["staging"]["staged"])
            self.assertEqual(1, applied["staging"]["preserved_outbox_history"])
            pending = self.store.list_outbox("pending")
            self.assertEqual(1, len(pending))
            self.assertTrue(recover(*arguments)["already_applied"])
            self.assertEqual(pending, self.store.list_outbox("pending"))
            self.assertEqual(history, self.store.list_outbox("published"))
            self.assertEqual(before, self.store.load_checkpoints(task_id))

        recovered = ReviewService(self.settings, workflow_factory=_paused_workflow)
        self.addCleanup(recovered.close)

        def delivered():
            task = self.store.get(task_id)
            outbox = self.store.outbox_stats()
            return (
                task["input"].get("_delivery_complete") is True
                and recovered.queue.depth() == 0
                and outbox["pending"] == outbox["publishing"] == 0
            )

        self.assertTrue(_wait(delivered), self.store.workflow_status(task_id, "default"))
        after = self.store.load_checkpoints(task_id)
        for node, checkpoint in before.items():
            if node != "workflow:receiver":
                self.assertEqual(checkpoint, after[node], node)
        old_receiver, new_receiver = before["workflow:receiver"], after["workflow:receiver"]
        self.assertEqual((1, 2), (old_receiver["attempt"], new_receiver["attempt"]))
        self.assertEqual("completed", new_receiver["status"])
        self.assertEqual(
            old_receiver["state"]["idempotency_key"], new_receiver["state"]["idempotency_key"]
        )
        self.assertEqual(old_receiver["state"]["handoff"], new_receiver["state"]["handoff"])
        self.assertEqual(messages, self.store.get(task_id)["collaboration"])
        self.assertEqual("SEC-EVAL", self.store.get(task_id)["report"]["findings"][0]["rule_id"])
        self.assertEqual(0, self.store.tenant_review_admission_stats()["active"])
        self.assertEqual([], recovered.queue.dead_letters())
        self.assertEqual(0, self.redis.xpending(TaskQueue.STREAM, TaskQueue.GROUP)["pending"])
        self.assertEqual(2 if rebuild else 1, len(self.store.list_outbox("published")))

    def test_invalid_agent_output_goes_directly_to_dlq_without_publishing_report(self):
        invalid = mock.Mock(return_value={"critiques": "private-invalid-payload"})

        def workflow(catalog):
            return review_workflow(
                {**catalog, "critic": replace(catalog["critic"], run=invalid, revision=REVISION)}
            )

        service = ReviewService(self.settings, workflow_factory=workflow)
        self.addCleanup(service.close)
        task_id = service.enqueue_review("demo/repo", DIFF, 7)["task_id"]
        self.assertTrue(
            _wait(lambda: service.queue.dead_letter_depth() == 1 and service.queue.depth() == 0)
        )
        invalid.assert_called_once()
        self.assertEqual(1, service.queue.dead_letters()[0]["attempt"])
        task = self.store.get(task_id)
        self.assertEqual("FAILED", task["state"])
        self.assertIsNone(task["report"])
        self.assertEqual(0, self.store.tenant_review_admission_stats()["active"])
        snapshot = self.store.workflow_status(task_id, "default")
        self.assertEqual(
            "failed", next(step for step in snapshot["steps"] if step["id"] == "critic")["status"]
        )
        self.assertNotIn("private-invalid-payload", str(snapshot))
