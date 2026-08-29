"""Deterministic, independently composable source-repair rules."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RuleMutation:
    changed: bool
    required_imports: frozenset[str] = frozenset()


@runtime_checkable
class FixRule(Protocol):
    """One narrow, deterministic repair policy for a stable finding rule id."""

    rule_id: str

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        """Mutate a parsed Python module only at verified target lines."""

    def propose_line(self, line: str) -> str:
        """Conservative fallback for files that cannot be parsed as Python."""


class DebugPrintFixRule:
    rule_id = "REL-DEBUG-PRINT"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Expr(inner, node):
                nonlocal changed
                if (
                    node.lineno in target_lines
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "print"
                ):
                    changed = True
                    return None
                return inner.generic_visit(node)

        Transformer().visit(tree)
        return RuleMutation(changed)

    def propose_line(self, line: str) -> str:
        return "" if re.match(r"^\s*print\s*\(", line) else line


class LiteralEvalFixRule:
    rule_id = "SEC-EVAL"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Call(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if (
                    node.lineno in target_lines
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "eval"
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    node.func = ast.Attribute(
                        value=ast.Name(id="ast", ctx=ast.Load()),
                        attr="literal_eval",
                        ctx=ast.Load(),
                    )
                    changed = True
                return node

        Transformer().visit(tree)
        return RuleMutation(changed, frozenset({"ast"}) if changed else frozenset())

    def propose_line(self, line: str) -> str:
        return line


class SubprocessShellFixRule:
    rule_id = "SEC-SUBPROCESS-SHELL"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Call(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if node.lineno not in target_lines:
                    return node
                for keyword in node.keywords:
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        keyword.value = ast.Constant(False)
                        changed = True
                return node

        Transformer().visit(tree)
        return RuleMutation(changed)

    def propose_line(self, line: str) -> str:
        return re.sub(r"shell\s*=\s*True", "shell=False", line)


class HardcodedSecretFixRule:
    rule_id = "SEC-HARDCODED-SECRET"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Assign(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if (
                    node.lineno in target_lines
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    name = node.targets[0].id
                    node.value = ast.Subscript(
                        value=ast.Attribute(
                            value=ast.Name(id="os", ctx=ast.Load()),
                            attr="environ",
                            ctx=ast.Load(),
                        ),
                        slice=ast.Constant(name.upper()),
                        ctx=ast.Load(),
                    )
                    changed = True
                return node

        Transformer().visit(tree)
        return RuleMutation(changed, frozenset({"os"}) if changed else frozenset())

    def propose_line(self, line: str) -> str:
        match = re.match(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"]).+?\3\s*$", line)
        if not match:
            return line
        return '%s%s = os.environ["%s"]' % (
            match.group(1),
            match.group(2),
            match.group(2).upper(),
        )


class SafeYamlLoadFixRule:
    rule_id = "SEC-YAML-LOAD"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Call(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if (
                    node.lineno in target_lines
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "yaml"
                    and node.func.attr == "load"
                    and len(node.args) == 1
                    and not node.keywords
                ):
                    # safe_load has one stream argument. Calls with an explicit
                    # custom Loader are intentionally left untouched.
                    node.func.attr = "safe_load"
                    changed = True
                return node

        Transformer().visit(tree)
        return RuleMutation(changed)

    def propose_line(self, line: str) -> str:
        return line


class SecureCookieFixRule:
    rule_id = "SEC-INSECURE-COOKIE"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Call(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if (
                    node.lineno not in target_lines
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr != "set_cookie"
                ):
                    return node
                secure_keywords = [keyword for keyword in node.keywords if keyword.arg == "secure"]
                if (
                    len(secure_keywords) == 1
                    and isinstance(secure_keywords[0].value, ast.Constant)
                    and secure_keywords[0].value.value is False
                ):
                    secure_keywords[0].value = ast.Constant(True)
                    changed = True
                return node

        Transformer().visit(tree)
        return RuleMutation(changed)

    def propose_line(self, line: str) -> str:
        return line


class IntConversionExceptionFixRule:
    rule_id = "REL-EMPTY-EXCEPT"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Try(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if (
                    len(node.body) != 1
                    or len(node.handlers) != 1
                    or node.orelse
                    or node.finalbody
                    or not isinstance(node.body[0], ast.Return)
                    or not isinstance(node.body[0].value, ast.Call)
                ):
                    return node
                call = node.body[0].value
                handler = node.handlers[0]
                if (
                    handler.lineno not in target_lines
                    or not isinstance(call.func, ast.Name)
                    or call.func.id != "int"
                    or len(call.args) != 1
                    or not isinstance(call.args[0], (ast.Name, ast.Constant))
                    or call.keywords
                    or not isinstance(handler.type, ast.Name)
                    or handler.type.id != "Exception"
                    or handler.name is not None
                    or len(handler.body) != 1
                    or not isinstance(handler.body[0], ast.Return)
                    or not isinstance(handler.body[0].value, ast.Constant)
                    or handler.body[0].value.value is not None
                ):
                    return node
                handler.type = ast.Tuple(
                    elts=[
                        ast.Name(id="TypeError", ctx=ast.Load()),
                        ast.Name(id="ValueError", ctx=ast.Load()),
                    ],
                    ctx=ast.Load(),
                )
                changed = True
                return node

        Transformer().visit(tree)
        return RuleMutation(changed)

    def propose_line(self, line: str) -> str:
        return line


class AssertAdminFixRule:
    rule_id = "SEC-ASSERT-AUTH"

    def apply_python(self, tree: ast.Module, target_lines: frozenset[int]) -> RuleMutation:
        changed = False

        class Transformer(ast.NodeTransformer):
            def visit_Assert(inner, node):
                nonlocal changed
                node = inner.generic_visit(node)
                if (
                    node.lineno not in target_lines
                    or node.msg is not None
                    or not isinstance(node.test, ast.Attribute)
                    or node.test.attr != "is_admin"
                    or not isinstance(node.test.value, ast.Name)
                    or node.test.value.id != "user"
                ):
                    return node
                changed = True
                return ast.copy_location(
                    ast.If(
                        test=ast.UnaryOp(op=ast.Not(), operand=node.test),
                        body=[
                            ast.Raise(
                                exc=ast.Call(
                                    func=ast.Name(id="PermissionError", ctx=ast.Load()),
                                    args=[ast.Constant("administrator access required")],
                                    keywords=[],
                                ),
                                cause=None,
                            )
                        ],
                        orelse=[],
                    ),
                    node,
                )

        Transformer().visit(tree)
        return RuleMutation(changed)

    def propose_line(self, line: str) -> str:
        return line


def default_fix_rules() -> list[FixRule]:
    return [
        DebugPrintFixRule(),
        LiteralEvalFixRule(),
        SubprocessShellFixRule(),
        HardcodedSecretFixRule(),
        SafeYamlLoadFixRule(),
        SecureCookieFixRule(),
        IntConversionExceptionFixRule(),
        AssertAdminFixRule(),
    ]
