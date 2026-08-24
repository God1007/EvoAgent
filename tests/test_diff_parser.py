import unittest

from evoagent.diff_parser import ParsedDiff, parse_unified_diff

DIFF = """diff --git a/app.py b/app.py
index 123..456 100644
--- a/app.py
+++ b/app.py
@@ -2,3 +2,4 @@ def run():
 keep = True
-old = 1
+new = 2
+eval(user_input)
 tail = 3
"""


class DiffParserTests(unittest.TestCase):
    def test_checkpoint_round_trip_and_schema_are_strict(self):
        parsed = parse_unified_diff(DIFF)
        self.assertEqual(parsed, ParsedDiff.from_dict(parsed.to_dict()))

        valid = parsed.to_dict()
        for value, message in (
            ([], "must be an object"),
            ({**valid, "files": "app.py"}, "unique list"),
            ({**valid, "files": [7]}, "unique list"),
            ({**valid, "files": ["app.py", "app.py"]}, "unique list"),
            ({**valid, "added_lines": {}}, "must be a list"),
            ({**valid, "added_lines": ["line"]}, "must be objects"),
            ({**valid, "added_lines": [{"path": 7, "line": 1, "content": "x"}]}, "strings"),
            (
                {**valid, "added_lines": [{"path": "a.py", "line": True, "content": "x"}]},
                "positive integer",
            ),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    ParsedDiff.from_dict(value)

    def test_parses_added_line_numbers(self):
        parsed = parse_unified_diff(DIFF)
        self.assertEqual(["app.py"], parsed.files)
        self.assertEqual(
            [(3, "new = 2"), (4, "eval(user_input)")],
            [(x.line, x.content) for x in parsed.added_lines],
        )

    def test_file_deduplication_preserves_first_seen_order(self):
        parsed = parse_unified_diff("+++ b/z.py\n+++ b/a.py\n+++ b/z.py\n")

        self.assertEqual(["z.py", "a.py"], parsed.files)

    def test_file_header_shaped_content_cannot_terminate_a_hunk(self):
        parsed = parse_unified_diff(
            "--- a/app.txt\n+++ b/app.txt\n@@ -0,0 +1,2 @@\n"
            "+++ b/decoy.py\n+eval(user_input)\n"
            "--- a/next.py\n+++ b/next.py\n@@ -0,0 +1 @@\n+print('next')\n"
        )

        self.assertEqual(["app.txt", "next.py"], parsed.files)
        self.assertEqual(
            [
                ("app.txt", 1, "++ b/decoy.py"),
                ("app.txt", 2, "eval(user_input)"),
                ("next.py", 1, "print('next')"),
            ],
            [(line.path, line.line, line.content) for line in parsed.added_lines],
        )


if __name__ == "__main__":
    unittest.main()
