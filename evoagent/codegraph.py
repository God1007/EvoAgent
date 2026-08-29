"""Incremental Python code knowledge graph (MVP).

The reviewer wastes tokens re-reading a whole PR when only a few symbols changed,
and it cannot tell a caller "this function you rely on just changed". This module
builds a lightweight symbol graph from Python sources and answers the question
that matters for incremental review: *given the changed files, which symbols are
in the blast radius?*

Design and honest limits:

* Python only, built on the stdlib ``ast`` (no Tree-sitter/SCIP dependency). The
  roadmap's multi-language, precise-resolution graph is future work.
* Symbols are module-qualified: ``pkg.mod.func`` / ``pkg.mod.Class.method``.
* Call edges are resolved *by name* (best-effort): a call to ``foo(...)`` links to
  every symbol whose short name is ``foo``. This over-connects rather than
  under-connects, which is the safe bias for an impact/blast-radius query.
* Import edges connect a module to the modules it imports (absolute and
  relative), so changing a module flags files that import it even without a
  direct call edge.

Known limitations (documented on purpose):

* A caller is credited with calls made by its *nested* functions (whole-subtree
  walk), which can inflate the blast radius — the safe over-connect direction.
* Calls made at module or class-body scope (outside any function) are not
  recorded as call edges; such files are still caught via import edges.
* Short names resolving to more than :attr:`CodeGraph.MAX_NAME_FANOUT` symbols
  are skipped as too common to be meaningful (and to bound cost).

The graph is incremental: :meth:`CodeGraph.update` re-parses only changed files.
"""

import ast
import posixpath
import stat
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from .models import (
    MAX_REPOSITORY_EVIDENCE_ITEM_BYTES,
    MAX_REPOSITORY_EVIDENCE_ITEMS,
    MAX_REVIEW_CONTEXT_SECTION_BYTES,
    RepositoryEvidence,
)

_ROOT_STANDARD_FILES = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CODING_STANDARDS.md",
    ".github/CONTRIBUTING.md",
)


def module_name_for_path(path: str) -> str:
    """``pkg/mod.py`` -> ``pkg.mod``; ``pkg/__init__.py`` -> ``pkg``."""
    normalized = path.replace("\\", "/")
    trimmed = normalized[:-3] if normalized.endswith(".py") else normalized
    trimmed = trimmed.strip("/")
    if trimmed.endswith("/__init__"):
        trimmed = trimmed[: -len("/__init__")]
    return trimmed.replace("/", ".")


@dataclass
class Symbol:
    qualname: str
    short_name: str
    kind: str  # "function" | "class" | "method"
    path: str
    lineno: int
    end_lineno: int


@dataclass
class _FileIndex:
    module: str
    symbols: list[Symbol] = field(default_factory=list)
    calls: dict[str, set[str]] = field(default_factory=dict)  # qualname -> short call names
    imports: set[str] = field(default_factory=set)  # imported module names


class CodeGraph:
    def __init__(self) -> None:
        self._files: dict[str, _FileIndex] = {}

    # ---- construction / incremental update -------------------------------

    def update(self, sources: dict[str, str]) -> None:
        """Parse (or re-parse) the given ``{path: source}`` files into the graph.

        Only the supplied files are touched, so callers can pass just the PR's
        changed files to keep an existing graph current. A file that fails to
        parse is indexed as empty (its stale symbols are dropped) rather than
        aborting the whole update.
        """
        for path, source in sources.items():
            if not path.endswith(".py"):
                continue
            self._files[path] = self._index_file(path, source)

    def remove(self, path: str) -> None:
        self._files.pop(path, None)

    @staticmethod
    def _index_file(path: str, source: str) -> _FileIndex:
        module = module_name_for_path(path)
        index = _FileIndex(module=module)
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            # Honour the "index as empty rather than abort" contract for
            # unparseable, null-byte-bearing, or pathologically nested input.
            return index
        call_aliases = {
            alias.asname: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.asname
        }

        def visit_body(body: list[ast.stmt], prefix: str, container_kind: str) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    qualname = "%s.%s" % (prefix, node.name)
                    index.symbols.append(
                        Symbol(
                            qualname,
                            node.name,
                            "class",
                            path,
                            node.lineno,
                            getattr(node, "end_lineno", node.lineno) or node.lineno,
                        )
                    )
                    visit_body(node.body, qualname, "method")
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    qualname = "%s.%s" % (prefix, node.name)
                    index.symbols.append(
                        Symbol(
                            qualname,
                            node.name,
                            container_kind if container_kind == "method" else "function",
                            path,
                            node.lineno,
                            getattr(node, "end_lineno", node.lineno) or node.lineno,
                        )
                    )
                    # Union rather than assign: two symbols can collide on a
                    # qualname (a nested function and a same-named method), and
                    # dropping one side would silently lose real call edges.
                    index.calls.setdefault(qualname, set()).update(
                        _called_names(node, call_aliases)
                    )
                    # Nested functions/classes are addressable too.
                    visit_body(node.body, qualname, "function")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        index.imports.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    for candidate in _import_from_targets(module, node):
                        index.imports.add(candidate)

        visit_body(tree.body, module, "function")
        return index

    # ---- queries ---------------------------------------------------------

    def symbols(self) -> list[Symbol]:
        return [symbol for index in self._files.values() for symbol in index.symbols]

    def symbols_in(self, path: str) -> list[Symbol]:
        index = self._files.get(path)
        return list(index.symbols) if index else []

    def _short_name_index(self) -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = {}
        for index in self._files.values():
            for symbol in index.symbols:
                mapping.setdefault(symbol.short_name, set()).add(symbol.qualname)
        return mapping

    #: A short name resolving to more symbols than this is treated as too common
    #: to be a useful edge (e.g. ``get``/``run``). Skipping it both bounds the
    #: worst-case O(callers × callees) cost and suppresses meaningless noise.
    MAX_NAME_FANOUT = 200

    def _reverse_call_edges(self) -> dict[str, set[str]]:
        """callee_qualname -> {caller_qualname} (best-effort, name-resolved)."""
        by_short = self._short_name_index()
        reverse: dict[str, set[str]] = {}
        for index in self._files.values():
            for caller, called in index.calls.items():
                for short in called:
                    callees = by_short.get(short, set())
                    if len(callees) > self.MAX_NAME_FANOUT:
                        continue
                    for callee in callees:
                        if callee != caller:
                            reverse.setdefault(callee, set()).add(caller)
        return reverse

    def _reverse_import_edges(self) -> dict[str, set[str]]:
        """module -> {path that imports it}."""
        reverse: dict[str, set[str]] = {}
        for path, index in self._files.items():
            for imported in index.imports:
                reverse.setdefault(imported, set()).add(path)
        return reverse

    def impact_of(self, changed_paths: list[str], max_depth: int = 5) -> dict:
        """Blast radius of a change set.

        Returns the changed symbols plus the transitive set of symbols that call
        them (reverse call reachability, bounded by ``max_depth``) and the files
        that import any changed module.
        """
        changed = {path for path in changed_paths}
        seed = {symbol.qualname for path in changed for symbol in self.symbols_in(path)}
        reverse_calls = self._reverse_call_edges()

        impacted: set[str] = set(seed)
        frontier = set(seed)
        depth = 0
        while frontier and depth < max_depth:
            nxt: set[str] = set()
            for qualname in frontier:
                for caller in reverse_calls.get(qualname, set()):
                    if caller not in impacted:
                        impacted.add(caller)
                        nxt.add(caller)
            frontier = nxt
            depth += 1

        # Truncated only if we stopped at the depth cap AND undiscovered callers
        # actually remain — otherwise a converged/cyclic graph is fully explored.
        truncated = any(
            caller not in impacted
            for qualname in frontier
            for caller in reverse_calls.get(qualname, set())
        )

        changed_modules = {module_name_for_path(path) for path in changed if path.endswith(".py")}
        reverse_imports = self._reverse_import_edges()
        importing_files: set[str] = set()
        for module in changed_modules:
            importing_files |= reverse_imports.get(module, set())
        importing_files -= changed

        downstream = sorted(impacted - seed)
        return {
            "changed_files": sorted(changed),
            "changed_symbols": sorted(seed),
            "impacted_symbols": downstream,
            "impacted_symbol_count": len(downstream),
            "importing_files": sorted(importing_files),
            "truncated": truncated,
        }


def _import_from_targets(module: str, node: ast.ImportFrom) -> set[str]:
    """Candidate imported module names for a ``from ... import ...`` statement.

    Handles both absolute and relative imports. For relative imports the base
    package is derived from the importing module so ``from . import util`` inside
    ``pkg.sub.mod`` resolves to ``pkg.sub.util`` (otherwise these edges are
    silently lost, which is the dangerous under-connection direction).
    """
    targets: set[str] = set()
    level = node.level or 0
    if level == 0:
        base = ""
    else:
        package_parts = module.split(".")[:-1]  # package of the importing module
        keep = len(package_parts) - (level - 1)
        base = ".".join(package_parts[:keep]) if keep > 0 else ""
    if node.module:
        full = "%s.%s" % (base, node.module) if base else node.module
        targets.add(full)
        for alias in node.names:
            targets.add("%s.%s" % (full, alias.name))
    else:
        for alias in node.names:
            targets.add("%s.%s" % (base, alias.name) if base else alias.name)
    return targets


def _called_names(func: ast.AST, aliases: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(aliases.get(target.id, target.id))
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def build_graph(sources: dict[str, str]) -> CodeGraph:
    graph = CodeGraph()
    graph.update(sources)
    return graph


def _repository_standard_paths(changed_paths: list[str]) -> set[str]:
    selected = set(_ROOT_STANDARD_FILES)
    for path in changed_paths:
        if "\x00" in path or "\\" in path or path.startswith("/"):
            continue
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            continue
        directory = posixpath.dirname(path)
        while directory:
            selected.add(posixpath.join(directory, "AGENTS.md"))
            directory = posixpath.dirname(directory)
    return selected


def repository_snapshot_from_archive(
    archive: bytes,
    changed_paths: list[str],
    revision: str,
    max_source_bytes: int,
) -> tuple[RepositoryEvidence, str, bool]:
    """Build code evidence and a bounded standards pack from one pinned zipball."""
    if (
        not isinstance(archive, bytes)
        or not archive
        or type(max_source_bytes) is not int
        or max_source_bytes <= 0
        or len(archive) > max_source_bytes
    ):
        raise ValueError("repository evidence archive exceeds its size limit")
    if not isinstance(changed_paths, list) or any(
        not isinstance(path, str) for path in changed_paths
    ):
        raise ValueError("repository evidence changed paths are invalid")
    changed = set(changed_paths)
    standard_paths = _repository_standard_paths(changed_paths)
    truncated = False
    standards_truncated = False
    sources: dict[str, str] = {}
    standard_candidates = []
    indexed_bytes = 0
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            members = bundle.infolist()
            if len(members) > 100_000:
                raise ValueError("repository evidence archive contains too many entries")
            candidates = []
            seen = set()
            for member in members:
                name = member.filename
                if "\x00" in name or "\\" in name or name.startswith("/"):
                    raise ValueError("repository evidence archive contains an unsafe path")
                parts = name.split("/")
                if any(part in {"", ".", ".."} for part in parts[:-1]):
                    raise ValueError("repository evidence archive contains an unsafe path")
                path = posixpath.normpath("/".join(parts[1:])) if len(parts) > 1 else ""
                if not path or member.is_dir():
                    continue
                is_source = path.endswith(".py")
                is_standard = path in standard_paths
                if not is_source and not is_standard:
                    continue
                if path.startswith("../") or path in seen:
                    raise ValueError(
                        "repository evidence archive contains duplicate or unsafe paths"
                    )
                seen.add(path)
                mode = member.external_attr >> 16
                if mode and stat.S_IFMT(mode) == stat.S_IFLNK:
                    truncated = truncated or is_source
                    standards_truncated = standards_truncated or is_standard
                    continue
                try:
                    path_bytes = path.encode("utf-8")
                except UnicodeError:
                    truncated = True
                    continue
                if len(path_bytes) > MAX_REPOSITORY_EVIDENCE_ITEM_BYTES:
                    truncated = truncated or is_source
                    standards_truncated = standards_truncated or is_standard
                    continue
                if is_source:
                    candidates.append((path not in changed, path, member))
                if is_standard:
                    standard_candidates.append((path, member))
            for _unchanged, path, member in sorted(candidates, key=lambda item: item[:2]):
                if len(sources) >= 5000 or member.file_size > max_source_bytes - indexed_bytes:
                    truncated = True
                    continue
                raw = bundle.read(member)
                if len(raw) != member.file_size or len(raw) > max_source_bytes - indexed_bytes:
                    raise ValueError("repository evidence archive entry exceeds its size limit")
                try:
                    source = raw.decode("utf-8")
                except UnicodeError:
                    truncated = True
                    continue
                if "\x00" in source:
                    truncated = True
                    continue
                sources[path] = source
                indexed_bytes += len(raw)

            standard_sections: list[str] = []
            standard_bytes = 0
            root_order = {path: index for index, path in enumerate(_ROOT_STANDARD_FILES)}
            for path, member in sorted(
                standard_candidates,
                key=lambda item: (
                    0 if item[0] in root_order else 1,
                    root_order.get(item[0], item[0].count("/")),
                    item[0],
                ),
            ):
                prefix = ("\n\n" if standard_sections else "") + "## %s\n" % path
                budget = (
                    MAX_REVIEW_CONTEXT_SECTION_BYTES - standard_bytes - len(prefix.encode("utf-8"))
                )
                if budget <= 0:
                    standards_truncated = True
                    break
                with bundle.open(member) as handle:
                    raw = handle.read(budget + 1)
                clipped = member.file_size > budget or len(raw) > budget
                raw = raw[:budget]
                if not clipped and len(raw) != member.file_size:
                    raise ValueError("repository standards archive entry is incomplete")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    # A bounded prefix may split only its final UTF-8 code point.
                    if clipped and exc.end == len(raw) and len(raw) - exc.start <= 4:
                        text = raw[: exc.start].decode("utf-8")
                    else:
                        standards_truncated = True
                        continue
                if "\x00" in text:
                    standards_truncated = True
                    continue
                text = text.rstrip()
                if text:
                    section = prefix + text
                    standard_sections.append(section)
                    standard_bytes += len(section.encode("utf-8"))
                if clipped:
                    standards_truncated = True
                    break
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValueError("repository evidence archive is invalid") from exc

    impact = build_graph(sources).impact_of(sorted(changed))

    def bounded(values: list[str]) -> tuple[str, ...]:
        nonlocal truncated
        valid: list[str] = []
        for value in sorted(set(values)):
            try:
                fits = len(value.encode("utf-8")) <= MAX_REPOSITORY_EVIDENCE_ITEM_BYTES
            except UnicodeError:
                fits = False
            if fits and len(valid) < MAX_REPOSITORY_EVIDENCE_ITEMS:
                valid.append(value)
            else:
                truncated = True
        return tuple(valid)

    truncated = truncated or bool(impact["truncated"])
    changed_files = bounded(impact["changed_files"])
    changed_symbols = bounded(impact["changed_symbols"])
    impacted_symbols = bounded(impact["impacted_symbols"])
    importing_files = bounded(impact["importing_files"])
    evidence = RepositoryEvidence(
        origin="github-archive",
        status="partial" if truncated else "available",
        revision=revision,
        indexed_files=len(sources),
        indexed_bytes=indexed_bytes,
        changed_paths=changed_files,
        changed_symbols=changed_symbols,
        impacted_symbols=impacted_symbols,
        importing_files=importing_files,
        truncated=truncated,
    )
    return (
        RepositoryEvidence.from_dict(evidence.to_dict()),
        "".join(standard_sections),
        standards_truncated,
    )


def repository_evidence_from_archive(
    archive: bytes,
    changed_paths: list[str],
    revision: str,
    max_source_bytes: int,
) -> RepositoryEvidence:
    """Backward-compatible evidence-only view of a repository snapshot."""
    return repository_snapshot_from_archive(archive, changed_paths, revision, max_source_bytes)[0]
