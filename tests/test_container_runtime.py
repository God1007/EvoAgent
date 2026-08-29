import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from evoagent.container_runtime import reconcile_sandboxes, remove_sandbox, sandbox_command

NAME = "evoagent-verify-012345abcdef"


class SandboxLifecycleTests(unittest.TestCase):
    def test_deadline_and_isolation_do_not_depend_on_the_owner_or_image_defaults(self):
        with patch("evoagent.container_runtime.time.time", return_value=100):
            command = sandbox_command("sha256:" + "a" * 64, NAME, 7, ["--memory", "256m"])
        for flag in ("--rm", "--init=false", "--no-healthcheck", "--read-only"):
            self.assertIn(flag, command)
        self.assertFalse(any(flag.startswith("--restart") for flag in command))
        self.assertEqual("/bin/sleep", command[command.index("--entrypoint") + 1])
        self.assertEqual("22", command[-1])
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertEqual("65533:65533", command[command.index("--user") + 1])
        self.assertEqual("/", command[command.index("--workdir") + 1])
        self.assertEqual(
            "com.evoagent.sandbox.expires-at=122", command[command.index("--label") + 1]
        )
        for value in (0, -1, True, 1.5):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                sandbox_command("image", NAME, value, [])
        for name in ("other-container", "evoagent-verify-", NAME + "-extra"):
            with self.subTest(name=name), patch("evoagent.container_runtime.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    sandbox_command("image", name, 1, [])
                with self.assertRaises(ValueError):
                    remove_sandbox(name)
                run.assert_not_called()

    def test_cleanup_accepts_only_removal_or_confirmed_absence_on_the_same_daemon(self):
        env = {"DOCKER_HOST": "unix:///owned/docker.sock"}
        for rc, output, expected in (
            (0, "", True),
            (0, "still-present", False),
            (1, "", False),
            (0, None, False),
        ):
            with (
                self.subTest(rc=rc, output=output),
                patch(
                    "evoagent.container_runtime.subprocess.run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=rc, stdout=output),
                    ],
                ) as run,
            ):
                self.assertEqual(expected, remove_sandbox(NAME, env))
                self.assertEqual(["docker", "rm", "-f", NAME], run.call_args_list[0].args[0])
                self.assertEqual(
                    ["docker", "ps", "-a", "--filter", "name=" + NAME, "--format", "{{.ID}}"],
                    run.call_args_list[1].args[0],
                )
                for call in run.call_args_list:
                    self.assertIs(env, call.kwargs["env"])
                    self.assertGreater(call.kwargs["timeout"], 0)
                    self.assertLessEqual(call.kwargs["timeout"], 10)
        with patch(
            "evoagent.container_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertTrue(remove_sandbox(NAME))
            run.assert_called_once()

    def test_cleanup_deadline_and_daemon_errors_are_not_absence(self):
        with (
            patch("evoagent.container_runtime.time.monotonic", side_effect=[0, 11]),
            patch(
                "evoagent.container_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ) as run,
        ):
            self.assertFalse(remove_sandbox(NAME))
            run.assert_called_once()
        for error in (OSError("daemon unavailable"), subprocess.TimeoutExpired("docker", 10)):
            with (
                self.subTest(error=type(error)),
                patch("evoagent.container_runtime.subprocess.run", side_effect=error),
                self.assertRaises(type(error)),
            ):
                remove_sandbox(NAME)

    def test_reconciliation_removes_only_expired_labelled_sandboxes(self):
        env = {"DOCKER_HOST": "unix:///owned/docker.sock"}
        inventory = SimpleNamespace(
            returncode=0,
            stdout=(NAME + "\t99\n" + "evoagent-skill-fedcba987654\t101\n"),
        )
        with (
            patch("evoagent.container_runtime.subprocess.run", return_value=inventory) as run,
            patch("evoagent.container_runtime.remove_sandbox", return_value=True) as remove,
        ):
            self.assertEqual(1, reconcile_sandboxes(env, now=100))
        self.assertIn("label=com.evoagent.sandbox.expires-at", run.call_args.args[0])
        remove.assert_called_once_with(NAME, env, mock.ANY)

    def test_reconciliation_fails_closed_on_untrusted_or_unbounded_inventory(self):
        invalid_outputs = (
            "other-container\t1\n",
            NAME + "\tnot-a-time\n",
            "\n".join("evoagent-verify-%012x\t1" % index for index in range(65)),
        )
        for output in invalid_outputs:
            with (
                self.subTest(output=output[:20]),
                patch(
                    "evoagent.container_runtime.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=output),
                ),
                patch("evoagent.container_runtime.remove_sandbox") as remove,
                self.assertRaises(RuntimeError),
            ):
                reconcile_sandboxes(now=100)
            remove.assert_not_called()
        with (
            patch(
                "evoagent.container_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=""),
            ),
            self.assertRaisesRegex(RuntimeError, "inventory failed"),
        ):
            reconcile_sandboxes(now=100)
        for timeout in (0, True, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                reconcile_sandboxes(now=100, timeout_seconds=timeout)
