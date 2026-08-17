import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import scripts.import_github_pr_dataset as importer


class PublicPrImportTests(unittest.TestCase):
    @staticmethod
    def pull_request(sha="a" * 40, private=False, repository="acme/widgets", number=17):
        return {
            "number": number,
            "head": {"sha": sha},
            "base": {"repo": {"full_name": repository, "private": private}},
        }

    def manifest(self, expected_findings=None):
        value = {
            "repository": "acme/widgets",
            "pull_request": 17,
            "split": "holdout",
            "rights": {
                "usage_basis": "repository-license",
                "spdx_id": "Apache-2.0",
                "review_status": "approved",
                "data_review_status": "approved",
                "review_reference": "LEGAL-17",
            },
        }
        if expected_findings is not None:
            value["expected_findings"] = expected_findings
        return value

    def test_default_import_is_answer_free_and_content_addressed(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n value = 1\n+eval(user)\n"
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, "manifest.jsonl")
            output = os.path.join(directory, "inputs.jsonl")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.manifest()) + "\n")
            with (
                patch.object(
                    importer,
                    "fetch_pull_request",
                    return_value=(diff, "a" * 40),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["importer", manifest, output, "--limit", "1"],
                ),
            ):
                importer.main()

            with open(output, encoding="utf-8") as handle:
                case = json.loads(handle.readline())
            self.assertNotIn("expected_findings", case)
            self.assertNotIn("annotation", case)
            self.assertEqual("a" * 40, case["source"]["head_sha"])
            self.assertEqual(
                hashlib.sha256(diff.encode()).hexdigest(), case["source"]["diff_sha256"]
            )
            self.assertEqual("approved", case["source"]["rights"]["review_status"])

    def test_fetch_binds_diff_to_a_stable_pull_request_head(self):
        with patch.object(importer, "GitHubClient") as client_type:
            client = client_type.return_value
            client.get_pull_request.side_effect = [
                self.pull_request(),
                self.pull_request(),
            ]
            client.fetch_diff.return_value = "diff"

            diff, head_sha = importer.fetch_pull_request("acme/widgets", 17, "token")

        self.assertEqual("diff", diff)
        self.assertEqual("a" * 40, head_sha)
        client.fetch_diff.assert_called_once_with(
            "https://api.github.com/repos/acme/widgets/pulls/17",
            max_bytes=5 * 1024 * 1024,
        )

    def test_fetch_rejects_a_head_change_during_import(self):
        with patch.object(importer, "GitHubClient") as client_type:
            client = client_type.return_value
            client.get_pull_request.side_effect = [
                self.pull_request(),
                self.pull_request("b" * 40),
            ]
            client.fetch_diff.return_value = "diff"

            with self.assertRaisesRegex(RuntimeError, "head changed"):
                importer.fetch_pull_request("acme/widgets", 17)

    def test_fetch_rejects_private_or_mismatched_repository_metadata(self):
        for payload in (
            self.pull_request(private=True),
            self.pull_request(repository="other/widgets"),
            self.pull_request(number=18),
        ):
            with (
                self.subTest(payload=payload),
                patch.object(importer, "GitHubClient") as client_type,
            ):
                client_type.return_value.get_pull_request.return_value = payload
                with self.assertRaisesRegex(RuntimeError, "requested public repository"):
                    importer.fetch_pull_request("acme/widgets", 17)

    def test_default_import_rejects_embedded_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, "manifest.jsonl")
            output = os.path.join(directory, "inputs.jsonl")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.manifest([])) + "\n")
            with patch.object(
                sys,
                "argv",
                ["importer", manifest, output, "--limit", "1"],
            ):
                with self.assertRaisesRegex(ValueError, "contains answer data"):
                    importer.main()

    def test_import_requires_explicit_rights_review_record(self):
        record = self.manifest()
        record.pop("rights")
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, "manifest.jsonl")
            output = os.path.join(directory, "inputs.jsonl")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            with patch.object(
                sys,
                "argv",
                ["importer", manifest, output, "--limit", "1"],
            ):
                with self.assertRaisesRegex(ValueError, "rights-review"):
                    importer.main()


if __name__ == "__main__":
    unittest.main()
