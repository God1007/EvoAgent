import unittest

from evoagent.models import Finding, Severity
from evoagent.session import (
    FindingStatus,
    classify_findings,
    continuity_summary,
    open_snapshot,
    snapshot_findings,
)
from tests.db_support import postgres_store

REPO = "org/app"


def _finding(**overrides):
    base = dict(
        rule_id="SEC-EVAL",
        severity=Severity.HIGH,
        title="Dangerous eval",
        explanation="e",
        path="app/service.py",
        line=42,
        evidence="eval(user_input)",
        fix="f",
        test="t",
    )
    base.update(overrides)
    return Finding(**base)


class ClassifyFindingsTests(unittest.TestCase):
    def test_all_new_when_no_previous_turn(self):
        result = classify_findings(REPO, [], [_finding()])
        self.assertEqual([FindingStatus.NEW], [c.status for c in result])

    def test_identical_finding_is_still_open_even_after_line_shift(self):
        previous = snapshot_findings(REPO, [_finding(line=42)])
        result = classify_findings(REPO, previous, [_finding(line=980)])
        self.assertEqual(1, len(result))
        self.assertEqual(FindingStatus.STILL_OPEN, result[0].status)

    def test_disappeared_finding_is_resolved(self):
        previous = snapshot_findings(REPO, [_finding()])
        result = classify_findings(REPO, previous, [])
        self.assertEqual([FindingStatus.RESOLVED], [c.status for c in result])
        self.assertIsNone(result[0].finding)

    def test_reindentation_keeps_finding_open(self):
        previous = snapshot_findings(REPO, [_finding(evidence="eval(user_input)")])
        moved = _finding(evidence="\n        eval(user_input)\n")
        result = classify_findings(REPO, previous, [moved])
        self.assertEqual([FindingStatus.STILL_OPEN], [c.status for c in result])

    def test_file_move_is_detected_as_moved_not_new_plus_resolved(self):
        previous = snapshot_findings(REPO, [_finding(path="old/a.py")])
        current = [_finding(path="new/a.py")]
        result = classify_findings(REPO, previous, current)
        self.assertEqual(1, len(result))
        self.assertEqual(FindingStatus.MOVED, result[0].status)
        self.assertEqual("old/a.py", result[0].previous_path)

    def test_new_and_resolved_coexist(self):
        previous = snapshot_findings(
            REPO, [_finding(rule_id="REL-DEBUG-PRINT", evidence="print(x)")]
        )
        current = [_finding(rule_id="SEC-EVAL", evidence="eval(y)")]
        statuses = sorted(c.status.value for c in classify_findings(REPO, previous, current))
        self.assertEqual(["new", "resolved"], statuses)

    def test_snapshot_fingerprints_are_repository_scoped(self):
        # A session is always single-repository, but the persisted fingerprint
        # must bind the repository so cross-repo storage never collides.
        a = snapshot_findings("org/a", [_finding()])[0]["fingerprint"]
        b = snapshot_findings("org/b", [_finding()])[0]["fingerprint"]
        self.assertNotEqual(a, b)

    def test_two_moves_sharing_a_key_are_both_moved(self):
        previous = snapshot_findings(
            REPO,
            [
                _finding(rule_id="X", title="dup", evidence="same", path="a/one.py"),
                _finding(rule_id="X", title="dup", evidence="same", path="a/two.py"),
            ],
        )
        current = [
            _finding(rule_id="X", title="dup", evidence="same", path="b/one.py"),
            _finding(rule_id="X", title="dup", evidence="same", path="b/two.py"),
        ]
        statuses = [c.status for c in classify_findings(REPO, previous, current)]
        self.assertEqual([FindingStatus.MOVED, FindingStatus.MOVED], statuses)

    def test_continuity_summary_counts(self):
        previous = snapshot_findings(
            REPO,
            [
                _finding(rule_id="A", evidence="a"),
                _finding(rule_id="B", evidence="b"),
                _finding(rule_id="C", evidence="c"),
            ],
        )
        current = [
            _finding(rule_id="A", evidence="a"),  # still open
            _finding(rule_id="B", evidence="b", path="moved/b.py"),  # moved
            _finding(rule_id="D", evidence="d"),  # new
        ]
        summary = continuity_summary(classify_findings(REPO, previous, current))
        self.assertEqual(1, summary["still_open"])
        self.assertEqual(1, summary["moved"])
        self.assertEqual(1, summary["new"])
        self.assertEqual(1, summary["resolved"])
        self.assertEqual(2, summary["carried"])
        self.assertEqual(3, summary["open"])


class StoreSessionTests(unittest.TestCase):
    def setUp(self):
        self.store = postgres_store(self)

    def _turn(self, head_sha, trigger, findings):
        turn = self.store.start_session_turn("default", REPO, 7, head_sha, trigger)
        classified = classify_findings(REPO, turn["previous_findings"], findings)
        self.store.complete_session_turn(
            turn["session_id"],
            turn["turn_id"],
            None,
            open_snapshot(REPO, classified),
            continuity_summary(classified),
            head_sha,
        )
        return turn, classified

    def test_first_turn_has_no_previous_findings_and_reuses_session(self):
        t1, _ = self._turn("sha1", "opened", [_finding(rule_id="A", evidence="a")])
        self.assertTrue(t1["is_new_session"])
        self.assertEqual([], t1["previous_findings"])
        t2 = self.store.start_session_turn("default", REPO, 7, "sha2", "synchronize")
        self.assertFalse(t2["is_new_session"])
        self.assertEqual(t1["session_id"], t2["session_id"])
        self.assertEqual("sha1", t2["previous_head_sha"])
        self.assertEqual(2, t2["sequence"])

    def test_previous_findings_carry_into_next_turn(self):
        self._turn(
            "sha1",
            "opened",
            [_finding(rule_id="A", evidence="a"), _finding(rule_id="B", evidence="b")],
        )
        _, classified = self._turn(
            "sha2",
            "synchronize",
            [_finding(rule_id="A", evidence="a"), _finding(rule_id="C", evidence="c")],
        )
        summary = continuity_summary(classified)
        self.assertEqual(1, summary["still_open"])  # A
        self.assertEqual(1, summary["new"])  # C
        self.assertEqual(1, summary["resolved"])  # B

    def test_turn_resolving_everything_leaves_empty_previous(self):
        self._turn("sha1", "opened", [_finding(rule_id="A", evidence="a")])
        self._turn("sha2", "synchronize", [])  # A resolved
        t3 = self.store.start_session_turn("default", REPO, 7, "sha3", "synchronize")
        self.assertEqual([], t3["previous_findings"])

    def test_timeline_lists_turns_with_summaries(self):
        t1, _ = self._turn("sha1", "opened", [_finding(rule_id="A", evidence="a")])
        self._turn("sha2", "synchronize", [_finding(rule_id="A", evidence="a")])
        timeline = self.store.get_session_timeline(t1["session_id"])
        self.assertEqual(2, len(timeline["turns"]))
        self.assertEqual("sha2", timeline["latest_head_sha"])
        self.assertEqual(1, timeline["turns"][1]["summary"]["still_open"])

    def test_get_session_by_pull_request(self):
        t1, _ = self._turn("sha1", "opened", [_finding(rule_id="A", evidence="a")])
        session = self.store.get_session("default", REPO, 7)
        self.assertEqual(t1["session_id"], session["id"])
        self.assertIsNone(self.store.get_session("default", REPO, 999))

    def test_input_required_lifecycle(self):
        t1, _ = self._turn("sha1", "opened", [_finding(rule_id="A", evidence="a")])
        self.assertFalse(self.store.resolve_session_input(t1["session_id"], "default"))
        self.store.set_session_input_required(t1["session_id"], "Which config should I assume?")
        session = self.store.get_session("default", REPO, 7)
        self.assertEqual("input-required", session["status"])
        self.assertEqual("Which config should I assume?", session["pending_input"])
        self.assertFalse(self.store.resolve_session_input(t1["session_id"], "another-tenant"))
        self.assertTrue(self.store.resolve_session_input(t1["session_id"], "default", "alice"))
        self.assertFalse(self.store.resolve_session_input(t1["session_id"], "default"))
        session = self.store.get_session("default", REPO, 7)
        self.assertEqual("open", session["status"])
        self.assertIsNone(session["pending_input"])
        audit = next(
            item
            for item in self.store.list_audit("default")
            if item["action"] == "session.input.provided"
        )
        self.assertEqual("alice", audit["actor"])
        self.assertEqual({}, audit["detail"])

    def test_out_of_order_completion_diffs_against_earlier_turn(self):
        # Turn 1 completes with A.
        t1 = self.store.start_session_turn("default", REPO, 7, "sha1", "opened")
        self.store.complete_session_turn(
            t1["session_id"],
            t1["turn_id"],
            None,
            open_snapshot(REPO, classify_findings(REPO, [], [_finding(rule_id="A", evidence="a")])),
            {},
            "sha1",
        )
        # Turns 2 and 3 both start before either review finishes.
        t2 = self.store.start_session_turn("default", REPO, 7, "sha2", "synchronize")
        t3 = self.store.start_session_turn("default", REPO, 7, "sha3", "synchronize")
        # Turn 3's review finishes first, recording B.
        self.store.complete_session_turn(
            t3["session_id"],
            t3["turn_id"],
            None,
            open_snapshot(REPO, classify_findings(REPO, [], [_finding(rule_id="B", evidence="b")])),
            {},
            "sha3",
        )
        # Turn 2 must diff against turn 1 (A), never the later turn 3 (B).
        prev2 = {
            p["fingerprint"]
            for p in self.store.previous_open_snapshot(t2["session_id"], t2["turn_id"])
        }
        self.assertIn(_finding(rule_id="A", evidence="a").scoped_fingerprint(REPO), prev2)
        self.assertNotIn(_finding(rule_id="B", evidence="b").scoped_fingerprint(REPO), prev2)
        self.store.complete_session_turn(t2["session_id"], t2["turn_id"], None, [], {}, "sha2")
        self.assertEqual("sha3", self.store.get_session("default", REPO, 7)["latest_head_sha"])

    def test_snapshot_records_finding_status(self):
        t1, _ = self._turn("sha1", "opened", [_finding(rule_id="A", evidence="a")])
        timeline = self.store.get_session_timeline(t1["session_id"])
        self.assertEqual(FindingStatus.NEW.value, timeline["turns"][0]["findings"][0]["status"])


if __name__ == "__main__":
    unittest.main()
