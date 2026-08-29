import io
import stat
import unittest
import zipfile

from evoagent.codegraph import (
    CodeGraph,
    build_graph,
    module_name_for_path,
    repository_evidence_from_archive,
    repository_snapshot_from_archive,
)
from evoagent.models import MAX_REVIEW_CONTEXT_SECTION_BYTES


def _zip(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path, content in files.items():
            bundle.writestr("repo-head/" + path, content)
    return buffer.getvalue()


class ModuleNameTests(unittest.TestCase):
    def test_plain_module(self):
        self.assertEqual("pkg.mod", module_name_for_path("pkg/mod.py"))

    def test_package_init(self):
        self.assertEqual("pkg", module_name_for_path("pkg/__init__.py"))

    def test_leading_slash_stripped(self):
        self.assertEqual("a.b", module_name_for_path("/a/b.py"))

    def test_windows_backslashes_normalised(self):
        self.assertEqual("pkg.mod", module_name_for_path("pkg\\mod.py"))


class SymbolExtractionTests(unittest.TestCase):
    def test_functions_classes_and_methods(self):
        graph = build_graph(
            {"pkg/mod.py": "def top():\n    pass\n\nclass A:\n    def m(self):\n        pass\n"}
        )
        quals = {s.qualname: s.kind for s in graph.symbols()}
        self.assertEqual("function", quals["pkg.mod.top"])
        self.assertEqual("class", quals["pkg.mod.A"])
        self.assertEqual("method", quals["pkg.mod.A.m"])

    def test_syntax_error_indexed_empty(self):
        graph = build_graph({"a.py": "def broken(:\n"})
        self.assertEqual([], graph.symbols())

    def test_non_python_ignored(self):
        graph = build_graph({"a.txt": "def x(): pass"})
        self.assertEqual([], graph.symbols())


class ImpactTests(unittest.TestCase):
    def _graph(self):
        util = "def helper():\n    return 1\n"
        service = (
            "from pkg import util\n"
            "def use_helper():\n"
            "    return util.helper()\n"
            "def use_service():\n"
            "    return use_helper()\n"
        )
        return build_graph({"pkg/util.py": util, "pkg/service.py": service})

    def test_changed_leaf_impacts_transitive_callers(self):
        impact = self._graph().impact_of(["pkg/util.py"])
        self.assertIn("pkg.util.helper", impact["changed_symbols"])
        # helper <- use_helper <- use_service
        self.assertIn("pkg.service.use_helper", impact["impacted_symbols"])
        self.assertIn("pkg.service.use_service", impact["impacted_symbols"])

    def test_importing_files_are_reported(self):
        impact = self._graph().impact_of(["pkg/util.py"])
        self.assertIn("pkg/service.py", impact["importing_files"])
        self.assertNotIn("pkg/util.py", impact["importing_files"])

    def test_changed_symbols_excluded_from_impacted(self):
        impact = self._graph().impact_of(["pkg/util.py"])
        self.assertNotIn("pkg.util.helper", impact["impacted_symbols"])

    def test_depth_limit_truncates(self):
        impact = self._graph().impact_of(["pkg/util.py"], max_depth=1)
        # Only the direct caller is reached at depth 1.
        self.assertIn("pkg.service.use_helper", impact["impacted_symbols"])
        self.assertNotIn("pkg.service.use_service", impact["impacted_symbols"])
        self.assertTrue(impact["truncated"])

    def test_unrelated_change_has_no_downstream(self):
        graph = build_graph({"pkg/lonely.py": "def alone():\n    return 0\n"})
        impact = graph.impact_of(["pkg/lonely.py"])
        self.assertEqual([], impact["impacted_symbols"])
        self.assertFalse(impact["truncated"])


class RepositoryEvidenceArchiveTests(unittest.TestCase):
    def test_archive_builds_bounded_changed_and_reverse_impact_summary(self):
        archive = _zip(
            {
                "pkg/util.py": "def helper():\n    return 1\n",
                "pkg/service.py": (
                    "from pkg.util import helper\ndef use():\n    return helper()\n"
                ),
                "README.md": "ignored",
            }
        )

        evidence = repository_evidence_from_archive(archive, ["pkg/util.py"], "abc123", 100_000)

        self.assertEqual("available", evidence.status)
        self.assertEqual(2, evidence.indexed_files)
        self.assertIn("pkg.util.helper", evidence.changed_symbols)
        self.assertIn("pkg.service.use", evidence.impacted_symbols)
        self.assertIn("pkg/service.py", evidence.importing_files)

    def test_archive_builds_root_and_changed_path_scoped_standards_pack(self):
        evidence, standards, truncated = repository_snapshot_from_archive(
            _zip(
                {
                    "AGENTS.md": "Root rules.",
                    "CONTRIBUTING.md": "Contribution rules.",
                    ".github/CONTRIBUTING.md": "GitHub rules.",
                    "pkg/AGENTS.md": "Package rules.",
                    "other/AGENTS.md": "Unrelated private rules.",
                    "pkg/service.py": "def changed():\n    return 1\n",
                }
            ),
            ["pkg/service.py"],
            "abc123",
            100_000,
        )

        self.assertEqual("available", evidence.status)
        self.assertFalse(truncated)
        self.assertEqual(
            "## AGENTS.md\nRoot rules.\n\n"
            "## CONTRIBUTING.md\nContribution rules.\n\n"
            "## .github/CONTRIBUTING.md\nGitHub rules.\n\n"
            "## pkg/AGENTS.md\nPackage rules.",
            standards,
        )
        self.assertNotIn("Unrelated private rules", standards)

    def test_repository_standards_are_utf8_bounded_without_losing_code_evidence(self):
        evidence, standards, truncated = repository_snapshot_from_archive(
            _zip(
                {
                    "AGENTS.md": "界" * MAX_REVIEW_CONTEXT_SECTION_BYTES,
                    "a.py": "def a():\n    pass\n",
                }
            ),
            ["a.py"],
            "abc123",
            200_000,
        )

        self.assertEqual("available", evidence.status)
        self.assertTrue(truncated)
        self.assertLessEqual(len(standards.encode("utf-8")), MAX_REVIEW_CONTEXT_SECTION_BYTES)
        standards.encode("utf-8").decode("utf-8")

    def test_unsafe_or_non_utf8_standard_documents_are_not_exposed(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            link = zipfile.ZipInfo("repo-head/AGENTS.md")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(link, "private-target")
            bundle.writestr("repo-head/CONTRIBUTING.md", b"rules-\xff")
            bundle.writestr("repo-head/a.py", "def a():\n    pass\n")

        evidence, standards, truncated = repository_snapshot_from_archive(
            output.getvalue(), ["a.py"], "abc123", 100_000
        )

        self.assertEqual("available", evidence.status)
        self.assertEqual("", standards)
        self.assertTrue(truncated)

    def test_non_utf8_source_is_skipped_and_marks_partial(self):
        evidence = repository_evidence_from_archive(
            _zip({"a.py": "def a():\n    pass\n", "broken.py": b"\xff"}),
            ["a.py"],
            "abc123",
            100_000,
        )

        self.assertEqual("partial", evidence.status)
        self.assertTrue(evidence.truncated)
        self.assertEqual(1, evidence.indexed_files)

    def test_invalid_or_oversized_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid"):
            repository_evidence_from_archive(b"not-a-zip", ["a.py"], "abc123", 1000)
        archive = _zip({"a.py": "pass\n"})
        with self.assertRaisesRegex(ValueError, "size limit"):
            repository_evidence_from_archive(archive, ["a.py"], "abc123", len(archive) - 1)


class RelativeImportTests(unittest.TestCase):
    def test_from_dot_import_resolves_to_package(self):
        graph = build_graph(
            {
                "pkg/util.py": "def helper():\n    return 1\n",
                "pkg/service.py": "from . import util\ndef use():\n    return util.helper()\n",
            }
        )
        impact = graph.impact_of(["pkg/util.py"])
        self.assertIn("pkg/service.py", impact["importing_files"])

    def test_from_dot_module_import_name_resolves(self):
        graph = build_graph(
            {
                "pkg/util.py": "def helper():\n    return 1\n",
                "pkg/service.py": "from .util import helper\ndef use():\n    return helper()\n",
            }
        )
        impact = graph.impact_of(["pkg/util.py"])
        self.assertIn("pkg/service.py", impact["importing_files"])

    def test_direct_import_alias_preserves_call_edge(self):
        graph = build_graph(
            {
                "pkg/util.py": "def helper():\n    return 1\n",
                "pkg/service.py": (
                    "from pkg.util import helper as execute\ndef use():\n    return execute()\n"
                ),
            }
        )

        self.assertIn("pkg.service.use", graph.impact_of(["pkg/util.py"])["impacted_symbols"])


class CallEdgeRobustnessTests(unittest.TestCase):
    def _graph(self):
        return build_graph(
            {
                "pkg/a.py": "def helper_a():\n    pass\n",
                "pkg/b.py": "def helper_b():\n    pass\n",
                "pkg/m.py": (
                    "from pkg import a, b\n"
                    "def foo():\n"
                    "    def bar():\n"
                    "        a.helper_a()\n"
                    "class foo:\n"
                    "    def bar(self):\n"
                    "        b.helper_b()\n"
                ),
            }
        )

    def test_colliding_qualnames_preserve_both_call_sets(self):
        graph = self._graph()
        # Both branches share qualname pkg.m.foo.bar; the union must keep both
        # call sets so a change to either helper reaches it.
        self.assertIn("pkg.m.foo.bar", graph.impact_of(["pkg/a.py"])["impacted_symbols"])
        self.assertIn("pkg.m.foo.bar", graph.impact_of(["pkg/b.py"])["impacted_symbols"])


class TruncationAccuracyTests(unittest.TestCase):
    def _chain(self):
        return build_graph(
            {
                "pkg/a.py": "def a():\n    return 1\n",
                "pkg/m.py": (
                    "from pkg import a\ndef b():\n    return a.a()\ndef c():\n    return b()\n"
                ),
            }
        )

    def test_truncated_when_callers_remain_beyond_depth(self):
        impact = self._chain().impact_of(["pkg/a.py"], max_depth=1)
        self.assertTrue(impact["truncated"])

    def test_not_truncated_when_fully_explored_at_boundary(self):
        impact = self._chain().impact_of(["pkg/a.py"], max_depth=2)
        self.assertFalse(impact["truncated"])
        self.assertIn("pkg.m.c", impact["impacted_symbols"])

    def test_cycle_is_not_truncated(self):
        graph = build_graph({"m.py": "def a():\n    return b()\ndef b():\n    return a()\n"})
        self.assertFalse(graph.impact_of(["m.py"])["truncated"])


class IncrementalUpdateTests(unittest.TestCase):
    def test_update_replaces_symbols_for_a_file(self):
        graph = CodeGraph()
        graph.update({"a.py": "def old():\n    pass\n"})
        self.assertEqual(["a.old"], [s.qualname for s in graph.symbols_in("a.py")])
        graph.update({"a.py": "def new():\n    pass\n"})
        self.assertEqual(["a.new"], [s.qualname for s in graph.symbols_in("a.py")])

    def test_remove_drops_file(self):
        graph = build_graph({"a.py": "def x():\n    pass\n"})
        graph.remove("a.py")
        self.assertEqual([], graph.symbols())


if __name__ == "__main__":
    unittest.main()
