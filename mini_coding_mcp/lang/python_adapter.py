from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..indexer import collect_symbol_details, extract_function_calls, function_node_for_symbol
from ..manipulator import apply_python_snippet, replace_source_span
from ..stable_index import Symbol, module_name_for_path, qname_for_path
from .adapter import CallEdge, FunctionComplexity, ImportEdge, LanguageAdapter, SymbolDetail


def _read_source(path: Path, source: str | None = None) -> str:
    if source is not None:
        return source
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _detail_from_symbol(detail: Any) -> SymbolDetail:
    return SymbolDetail(
        file_path=detail.file_path,
        kind=detail.kind,
        name=detail.name,
        qualname=detail.qualname,
        parent_qualname=detail.parent_qualname,
        start_line=detail.start_line,
        end_line=detail.end_line,
        start_col=detail.start_col,
        end_col=detail.end_col,
        signature=detail.signature,
        docstring=detail.docstring,
        decorators=list(detail.decorators),
        bases=list(detail.bases),
        returns=detail.returns,
        source=detail.source,
        node=detail.node,
    )


class PythonAdapter(LanguageAdapter):
    extensions = frozenset({".py"})

    def parse(self, source: str) -> Any | None:
        try:
            return {"source": source, "tree": ast.parse(source)}
        except SyntaxError:
            return None

    def extract_symbols(self, tree: Any, path: Path) -> list[SymbolDetail]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        return [_detail_from_symbol(detail) for detail in collect_symbol_details(path, source)]

    def extract_imports(self, tree: Any, path: Path) -> list[ImportEdge]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        try:
            module = ast.parse(source)
        except SyntaxError:
            return []
        edges: list[ImportEdge] = []
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append(
                        ImportEdge(
                            source=module_name_for_path(path.parent, path),
                            source_file=str(path.resolve()),
                            target=alias.name,
                            target_file=None,
                            detail=alias.asname,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                target = "." * node.level + module_name if node.level else module_name
                for alias in node.names:
                    edges.append(
                        ImportEdge(
                            source=module_name_for_path(path.parent, path),
                            source_file=str(path.resolve()),
                            target=target,
                            target_file=None,
                            detail=alias.asname or alias.name,
                        )
                    )
        return edges

    def extract_calls(self, tree: Any, path: Path, source: str) -> list[CallEdge]:
        source = _read_source(path, source)
        parsed = self.parse(source)
        if parsed is None:
            return []
        calls: list[CallEdge] = []
        for detail in self.extract_symbols(parsed, path):
            if detail.kind not in {"function", "method"}:
                continue
            symbol = Symbol(
                qname=qname_for_path(path.parent, path, detail.qualname),
                kind=detail.kind,  # type: ignore[arg-type]
                file=path.resolve(),
                line_start=detail.start_line,
                line_end=detail.end_line,
                signature=detail.signature,
                docstring=detail.docstring,
                decorators=list(detail.decorators),
                parent=detail.parent_qualname,
                bases=list(detail.bases),
                returns=detail.returns,
            )
            node = function_node_for_symbol(symbol, source)
            if node is None:
                continue
            segment = ast.get_source_segment(source, node) or detail.source
            for call in extract_function_calls(segment, str(path.resolve()), detail.start_line):
                calls.append(
                    CallEdge(
                        caller_qname=symbol.qname,
                        caller_file=str(path.resolve()),
                        callee=call["call"],
                        line=call["line"],
                        end_line=call["end_line"],
                        col=call["col"],
                        end_col=call["end_col"],
                        source=call["source"],
                    )
                )
        return calls

    def extract_complexity(self, tree: Any, path: Path) -> list[FunctionComplexity]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        parsed = self.parse(source)
        if parsed is None:
            return []
        results: list[FunctionComplexity] = []
        for detail in self.extract_symbols(parsed, path):
            if detail.kind not in {"function", "method"}:
                continue
            try:
                node = ast.parse(detail.source).body[0]
            except Exception:
                continue
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            loops = 0
            branches = 0
            for child in ast.walk(node):
                if isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
                    loops += 1
                elif isinstance(child, ast.If):
                    branches += 1
            reasons: list[str] = []
            if loops > 8:
                reasons.append("loops")
            if branches > 15:
                reasons.append("branches")
            results.append(
                FunctionComplexity(
                    qname=qname_for_path(path.parent, path, detail.qualname),
                    file_path=str(path.resolve()),
                    name=detail.name,
                    qualname=detail.qualname,
                    start_line=detail.start_line,
                    end_line=detail.end_line,
                    line_count=max(1, detail.end_line - detail.start_line + 1),
                    for_loops=loops,
                    if_statements=branches,
                    is_complex=bool(reasons),
                    reasons=reasons,
                )
            )
        return results

    def insert_code(self, source: str, code: str, anchor: str | None, position: str) -> str:
        return apply_python_snippet(source, code, anchor=anchor, position=position)

    def replace_symbol(self, source: str, symbol: SymbolDetail, new_source: str) -> str:
        return replace_source_span(source, symbol.start_line, symbol.end_line, new_source)
