from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


BLOCK_KIND = Literal["import", "constant", "definition", "raw"]


@dataclass(slots=True)
class SymbolEntry:
    file_path: str
    name: str
    kind: str
    qualname: str
    start_line: int
    end_line: int
    start_col: int
    end_col: int


@dataclass(slots=True)
class SymbolDetail:
    file_path: str
    kind: str
    name: str
    qualname: str
    parent_qualname: str | None
    start_line: int
    end_line: int
    start_col: int
    end_col: int
    signature: str | None
    docstring: str | None
    decorators: list[str]
    bases: list[str]
    returns: str | None
    source: str
    node: ast.AST


def _decorator_texts(node: ast.AST) -> list[str]:
    decorators = getattr(node, "decorator_list", [])
    return [ast.unparse(decorator) for decorator in decorators]


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    dummy = ast.AsyncFunctionDef if isinstance(node, ast.AsyncFunctionDef) else ast.FunctionDef
    clone = dummy(
        name=node.name,
        args=node.args,
        body=[ast.Pass()],
        decorator_list=[],
        returns=node.returns,
        type_comment=None,
    )
    ast.fix_missing_locations(clone)
    return ast.unparse(clone).split(":", 1)[0]


def _class_signature(node: ast.ClassDef) -> str:
    clone = ast.ClassDef(
        name=node.name,
        bases=node.bases,
        keywords=node.keywords,
        body=[ast.Pass()],
        decorator_list=[],
    )
    ast.fix_missing_locations(clone)
    return ast.unparse(clone).split(":", 1)[0]


def _symbol_source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ast.unparse(node)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = [node.attr]
        current = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    try:
        return ast.unparse(node)
    except Exception:
        return "<unknown>"


class SymbolIntrospector(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.records: list[SymbolDetail] = []
        self.stack: list[tuple[str, str]] = []

    def _qualname(self, name: str) -> str:
        if not self.stack:
            return name
        return ".".join([*(scope_name for scope_name, _ in self.stack), name])

    def _parent_qualname(self) -> str | None:
        if not self.stack:
            return None
        return ".".join(scope_name for scope_name, _ in self.stack)

    def _in_function_scope(self) -> bool:
        return any(scope_kind == "function" for _, scope_kind in self.stack)

    def _append(self, node: ast.AST, name: str, kind: str) -> None:
        signature: str | None = None
        docstring: str | None = None
        decorators: list[str] = []
        bases: list[str] = []
        returns: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = _function_signature(node)
            docstring = ast.get_docstring(node)
            decorators = _decorator_texts(node)
            returns = ast.unparse(node.returns) if node.returns is not None else None
        elif isinstance(node, ast.ClassDef):
            signature = _class_signature(node)
            docstring = ast.get_docstring(node)
            decorators = _decorator_texts(node)
            bases = [ast.unparse(base) for base in node.bases]

        self.records.append(
            SymbolDetail(
                file_path=str(self.path),
                kind=kind,
                name=name,
                qualname=self._qualname(name),
                parent_qualname=self._parent_qualname(),
                start_line=getattr(node, "lineno", 1),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                start_col=getattr(node, "col_offset", 0) + 1,
                end_col=getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) + 1,
                signature=signature,
                docstring=docstring,
                decorators=decorators,
                bases=bases,
                returns=returns,
                source=_symbol_source(self.source, node),
                node=node,
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


class _FunctionSummaryVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.calls: set[str] = set()
        self.return_count = 0

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.reads.add(node.id)
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self.writes.add(node.id)

    def visit_Call(self, node: ast.Call) -> Any:
        self.calls.add(_call_name(node.func))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> Any:
        self.return_count += 1
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        return None


