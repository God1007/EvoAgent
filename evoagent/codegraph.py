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
from dataclasses import dataclass, field


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
