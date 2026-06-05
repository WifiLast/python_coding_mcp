from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .symbols import (
    SymbolDetail,
    SymbolEntry,
    SymbolIntrospector,
    _FunctionSummaryVisitor,
    _call_name,
)
from .stable_index import Symbol


class PythonSymbolIndexer(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.entries: list[SymbolEntry] = []
        self.stack: list[tuple[str, str]] = []

    def _qualname(self, name: str) -> str:
        if not self.stack:
            return name
        return ".".join([*(scope_name for scope_name, _ in self.stack), name])

    def _in_function_scope(self) -> bool:
        return any(scope_kind == "function" for _, scope_kind in self.stack)

    def _append(self, node: ast.AST, name: str, kind: str) -> None:
        self.entries.append(
            SymbolEntry(
                file_path=str(self.path),
                name=name,
                kind=kind,
                qualname=self._qualname(name),
                start_line=getattr(node, "lineno", 1),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                start_col=getattr(node, "col_offset", 0) + 1,
                end_col=getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) + 1,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self._append(node, node.name, "class")
        self.stack.append((node.name, "class"))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._append(node, node.name, "function")
        self.stack.append((node.name, "function"))
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._append(node, node.name, "function")
        self.stack.append((node.name, "function"))
        self.generic_visit(node)
        self.stack.pop()

    def _record_variable_target(self, target: ast.AST, node: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._append(node, target.id, "variable")
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._record_variable_target(item, node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        if not self._in_function_scope():
            for target in node.targets:
                self._record_variable_target(target, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        if not self._in_function_scope():
            self._record_variable_target(node.target, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> Any:
        if not self._in_function_scope():
            self._record_variable_target(node.target, node)
        self.generic_visit(node)


def find_reference_hits_in_file(path: Path, name: str, source: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    hits: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int, str]] = set()

    class ReferenceVisitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> Any:
            if node.id == name:
                key = (
                    getattr(node, "lineno", 1),
                    getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                    getattr(node, "col_offset", 0),
                    getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
                    "name",
                )
                if key not in seen:
                    seen.add(key)
                    hits.append(
                        {
                            "file_path": str(path),
                            "kind": "name",
                            "name": name,
                            "line": getattr(node, "lineno", 1),
                            "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                            "col": getattr(node, "col_offset", 0) + 1,
                            "end_col": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) + 1,
                            "source": ast.get_source_segment(source, node) or ast.unparse(node),
                        }
                    )
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> Any:
            if node.attr == name:
                key = (
                    getattr(node, "lineno", 1),
                    getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                    getattr(node, "col_offset", 0),
                    getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
                    "attribute",
                )
                if key not in seen:
                    seen.add(key)
                    hits.append(
                        {
                            "file_path": str(path),
                            "kind": "attribute",
                            "name": name,
                            "line": getattr(node, "lineno", 1),
                            "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                            "col": getattr(node, "col_offset", 0) + 1,
                            "end_col": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) + 1,
                            "source": ast.get_source_segment(source, node) or ast.unparse(node),
                        }
                    )
            self.generic_visit(node)

        def visit_arg(self, node: ast.arg) -> Any:
            if node.arg == name:
                key = (
                    getattr(node, "lineno", 1),
                    getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                    getattr(node, "col_offset", 0),
                    getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
                    "arg",
                )
                if key not in seen:
                    seen.add(key)
                    hits.append(
                        {
                            "file_path": str(path),
                            "kind": "parameter",
                            "name": name,
                            "line": getattr(node, "lineno", 1),
                            "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                            "col": getattr(node, "col_offset", 0) + 1,
                            "end_col": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) + 1,
                            "source": ast.get_source_segment(source, node) or node.arg,
                        }
                    )

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            if node.name == name:
                hits.append(
                    {
                        "file_path": str(path),
                        "kind": "function_definition",
                        "name": name,
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                        "col": node.col_offset + 1,
                        "end_col": getattr(node, "end_col_offset", node.col_offset) + 1,
                        "source": ast.get_source_segment(source, node) or ast.unparse(node),
                    }
                )
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            if node.name == name:
                hits.append(
                    {
                        "file_path": str(path),
                        "kind": "function_definition",
                        "name": name,
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                        "col": node.col_offset + 1,
                        "end_col": getattr(node, "end_col_offset", node.col_offset) + 1,
                        "source": ast.get_source_segment(source, node) or ast.unparse(node),
                    }
                )
            self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            if node.name == name:
                hits.append(
                    {
                        "file_path": str(path),
                        "kind": "class_definition",
                        "name": name,
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                        "col": node.col_offset + 1,
                        "end_col": getattr(node, "end_col_offset", node.col_offset) + 1,
                        "source": ast.get_source_segment(source, node) or ast.unparse(node),
                    }
                )
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> Any:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    hits.append(
                        {
                            "file_path": str(path),
                            "kind": "variable_definition",
                            "name": name,
                            "line": node.lineno,
                            "end_line": getattr(node, "end_lineno", node.lineno),
                            "col": node.col_offset + 1,
                            "end_col": getattr(node, "end_col_offset", node.col_offset) + 1,
                            "source": ast.get_source_segment(source, node) or ast.unparse(node),
                        }
                    )
            self.generic_visit(node)

    ReferenceVisitor().visit(tree)
    return hits


def extract_function_calls(source: str, file_path: str, base_line: int) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if not tree.body:
        return []
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []

    calls: list[dict[str, Any]] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        call_name = _call_name(call.func)
        calls.append(
            {
                "file_path": file_path,
                "call": call_name,
                "line": base_line + getattr(call, "lineno", 1) - 1,
                "col": getattr(call, "col_offset", 0) + 1,
                "end_line": base_line + getattr(call, "end_lineno", getattr(call, "lineno", 1)) - 1,
                "end_col": getattr(call, "end_col_offset", getattr(call, "col_offset", 0)) + 1,
                "source": ast.get_source_segment(source, call) or ast.unparse(call),
            }
        )
    calls.sort(key=lambda item: (item["line"], item["col"], item["call"]))
    return calls


def extract_symbol_summary(detail: SymbolDetail, source: str) -> dict[str, Any]:
    node = detail.node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        visitor = _FunctionSummaryVisitor()
        for statement in node.body:
            visitor.visit(statement)
        return {
            "kind": "function",
            "docstring": detail.docstring,
            "signature": detail.signature,
            "returns": detail.returns,
            "decorators": detail.decorators,
            "names_read": sorted(visitor.reads),
            "names_written": sorted(visitor.writes),
            "calls": sorted(visitor.calls),
            "return_count": visitor.return_count,
        }
    if isinstance(node, ast.ClassDef):
        method_names = [
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        class_assignments = []
        for child in node.body:
            if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                class_assignments.append(ast.get_source_segment(source, child) or ast.unparse(child))
        return {
            "kind": "class",
            "docstring": detail.docstring,
            "signature": detail.signature,
            "decorators": detail.decorators,
            "bases": detail.bases,
            "methods": method_names,
            "class_attributes": class_assignments,
        }
    value_source = detail.source
    return {
        "kind": "variable",
        "source": value_source,
        "docstring": detail.docstring,
    }


def collect_symbol_details(path: Path, source: str) -> list[SymbolDetail]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = SymbolIntrospector(path, source)
    visitor.visit(tree)
    return visitor.records


def symbol_details_to_index_entry(detail: SymbolDetail) -> dict[str, Any]:
    return {
        "file_path": detail.file_path,
        "name": detail.name,
        "kind": detail.kind,
        "qualname": detail.qualname,
        "parent_qualname": detail.parent_qualname,
        "start_line": detail.start_line,
        "end_line": detail.end_line,
        "start_col": detail.start_col,
        "end_col": detail.end_col,
        "signature": detail.signature,
        "docstring": detail.docstring,
        "decorators": detail.decorators,
        "bases": detail.bases,
        "returns": detail.returns,
        "source": detail.source,
    }


MAX_INSERT_LINES = 50


def validate_single_python_unit(source: str) -> tuple[bool, str | None, int]:
    line_count = len(source.splitlines())
    if line_count > MAX_INSERT_LINES:
        return False, f"snippet_too_large:{line_count}_lines_max_{MAX_INSERT_LINES}", 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False, "invalid_python_snippet", 0
    if not tree.body:
        return False, "empty_python_snippet", 0
    top_level_definitions = sum(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body
    )
    if top_level_definitions > 1:
        return False, "too_many_top_level_definitions", top_level_definitions
    return True, None, top_level_definitions


def function_node_for_symbol(symbol: Symbol, source: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and getattr(node, "lineno", None) == symbol.line_start and getattr(node, "end_lineno", None) == symbol.line_end:
            return node
    return None
