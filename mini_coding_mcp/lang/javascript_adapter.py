from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .adapter import CallEdge, FunctionComplexity, ImportEdge, LanguageAdapter, SymbolDetail

_IDENT = r"[A-Za-z_$][\w$]*"
_QUALIFIED_IDENT = rf"{_IDENT}(?:\.{_IDENT})*"


def _read_source(path: Path, source: str | None = None) -> str:
    if source is not None:
        return source
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer(r"\n", source):
        offsets.append(match.end())
    return offsets


def _line_col_from_offset(source: str, offset: int) -> tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    line_start = source.rfind("\n", 0, offset)
    if line_start < 0:
        line_start = -1
    return line, offset - line_start


def _slice_lines(source: str, start_line: int, end_line: int) -> str:
    lines = source.splitlines(keepends=True)
    return "".join(lines[max(start_line - 1, 0) : min(end_line, len(lines))])


def _leading_docstring(source: str) -> str | None:
    match = re.match(r"^\s*/\*\*([\s\S]*?)\*/", source)
    if match:
        body = match.group(1).strip()
        lines = [line.strip(" *") for line in body.splitlines()]
        return "\n".join(line for line in lines if line).strip() or None
    return None


def _block_end(source: str, start_offset: int) -> int:
    depth = 0
    in_single = False
    in_double = False
    in_template = False
    in_line_comment = False
    in_block_comment = False
    escaped = False
    started = False
    for idx in range(start_offset, len(source)):
        ch = source[idx]
        nxt = source[idx + 1] if idx + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_single:
            if not escaped and ch == "'":
                in_single = False
            escaped = ch == "\\" and not escaped
            continue
        if in_double:
            if not escaped and ch == '"':
                in_double = False
            escaped = ch == "\\" and not escaped
            continue
        if in_template:
            if not escaped and ch == "`":
                in_template = False
            escaped = ch == "\\" and not escaped
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch == "'":
            in_single = True
            escaped = False
            continue
        if ch == '"':
            in_double = True
            escaped = False
            continue
        if ch == "`":
            in_template = True
            escaped = False
            continue
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth <= 0:
                return idx + 1
    return len(source)


def _extract_body(source: str, start_line: int, end_line: int) -> str:
    return _slice_lines(source, start_line, end_line)


def _function_signature(params: str, async_prefix: bool = False) -> str:
    prefix = "async " if async_prefix else ""
    return f"{prefix}({params.strip()})"


class JavaScriptAdapter(LanguageAdapter):
    extensions = frozenset({".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"})

    def parse(self, source: str) -> Any | None:
        return {"source": source, "lines": source.splitlines(), "docstring": _leading_docstring(source)}

    def _module_name(self, path: Path) -> str:
        rel = path.stem
        return rel

    def _symbol_from_match(
        self,
        path: Path,
        source: str,
        name: str,
        kind: str,
        start: int,
        end: int,
        params: str = "",
        bases: list[str] | None = None,
        parent: str | None = None,
        async_prefix: bool = False,
        decorators: list[str] | None = None,
    ) -> SymbolDetail:
        start_line, start_col = _line_col_from_offset(source, start)
        end_line, end_col = _line_col_from_offset(source, end)
        qualname = f"{parent}.{name}" if parent else name
        source_text = source[start:end].strip() or _extract_body(source, start_line, end_line).strip()
        signature = None
        if kind in {"function", "method"}:
            signature = _function_signature(params, async_prefix=async_prefix)
        elif kind == "class":
            extends = f" extends {', '.join(bases or [])}" if bases else ""
            signature = f"class {name}{extends}"
        return SymbolDetail(
            file_path=str(path.resolve()),
            kind=kind,
            name=name,
            qualname=qualname,
            parent_qualname=parent,
            start_line=start_line,
            end_line=end_line,
            start_col=start_col,
            end_col=end_col,
            signature=signature,
            docstring=None,
            decorators=decorators or [],
            bases=bases or [],
            returns=None,
            source=source_text,
        )

    def extract_symbols(self, tree: Any, path: Path) -> list[SymbolDetail]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        symbols: list[SymbolDetail] = []
        lines = source.splitlines()
        class_re = re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?class\s+({_IDENT})(?:\s+extends\s+([^\{{]+))?\s*\{{?", re.M)
        func_re = re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?function\s+({_IDENT})\s*\(([^)]*)\)", re.M)
        arrow_re = re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_IDENT})\s*=\s*(async\s*)?\(?([^=;]*)\)?\s*=>", re.M)
        export_default_func_re = re.compile(rf"^\s*export\s+default\s+(?:async\s+)?function\s+({_IDENT})\s*\(([^)]*)\)", re.M)
        export_default_class_re = re.compile(rf"^\s*export\s+default\s+class\s+({_IDENT})(?:\s+extends\s+([^\{{]+))?\s*\{{?", re.M)

        seen_spans: set[tuple[int, int]] = set()

        def _add(symbol: SymbolDetail) -> None:
            key = (symbol.start_line, symbol.end_line)
            if key not in seen_spans:
                seen_spans.add(key)
                symbols.append(symbol)

        for match in export_default_class_re.finditer(source):
            start = match.start()
            end = _block_end(source, source.find("{", match.end() - 1))
            bases = [part.strip() for part in (match.group(2) or "").split(",") if part.strip()]
            _add(self._symbol_from_match(path, source, match.group(1), "class", start, end, bases=bases))
        for match in class_re.finditer(source):
            start = match.start()
            brace = source.find("{", match.end() - 1)
            end = _block_end(source, brace if brace >= 0 else match.end())
            bases = [part.strip() for part in (match.group(2) or "").split(",") if part.strip()]
            class_symbol = self._symbol_from_match(path, source, match.group(1), "class", start, end, bases=bases)
            _add(class_symbol)
            body = _slice_lines(source, class_symbol.start_line + 1, class_symbol.end_line - 1)
            method_re = re.compile(rf"^\s*(?:async\s+)?({_IDENT})\s*\(([^)]*)\)\s*\{{", re.M)
            for method in method_re.finditer(body):
                method_start = source.find(method.group(0), start)
                if method_start < 0:
                    continue
                method_end = _block_end(source, source.find("{", method_start))
                async_prefix = "async" in method.group(0).split()
                _add(
                    self._symbol_from_match(
                        path,
                        source,
                        method.group(1),
                        "method",
                        method_start,
                        method_end,
                        params=method.group(2),
                        parent=class_symbol.qualname,
                        async_prefix=async_prefix,
                    )
                )
        for match in export_default_func_re.finditer(source):
            start = match.start()
            brace = source.find("{", match.end() - 1)
            end = _block_end(source, brace if brace >= 0 else match.end())
            _add(self._symbol_from_match(path, source, match.group(1), "function", start, end, params=match.group(2)))
        for match in func_re.finditer(source):
            start = match.start()
            brace = source.find("{", match.end() - 1)
            end = _block_end(source, brace if brace >= 0 else match.end())
            async_prefix = "async" in match.group(0).split()
            _add(self._symbol_from_match(path, source, match.group(1), "function", start, end, params=match.group(2), async_prefix=async_prefix))
        for match in arrow_re.finditer(source):
            start = match.start()
            brace = source.find("{", match.end() - 1)
            end = _block_end(source, brace if brace >= 0 else match.end())
            async_prefix = bool(match.group(2))
            _add(self._symbol_from_match(path, source, match.group(1), "function", start, end, params=match.group(3), async_prefix=async_prefix))
        # Top-level exports/consts that are not functions/classes
        export_const_re = re.compile(rf"^\s*export\s+(?:const|let|var)\s+({_IDENT})\b", re.M)
        const_re = re.compile(rf"^\s*(?:const|let|var)\s+({_IDENT})\b", re.M)
        for match in export_const_re.finditer(source):
            start = match.start()
            end = match.end()
            _add(self._symbol_from_match(path, source, match.group(1), "variable", start, end))
        for match in const_re.finditer(source):
            start = match.start()
            end = match.end()
            _add(self._symbol_from_match(path, source, match.group(1), "variable", start, end))

        symbols.sort(key=lambda item: (item.start_line, item.start_col, item.qualname))
        return symbols

    def extract_imports(self, tree: Any, path: Path) -> list[ImportEdge]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        edges: list[ImportEdge] = []
        import_re = re.compile(r"^\s*import\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]\s*;?\s*$", re.M)
        named_re = re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", re.M)
        require_re = re.compile(rf"require\(\s*['\"]([^'\"]+)['\"]\s*\)")
        for match in import_re.finditer(source):
            target = match.group(1)
            edges.append(ImportEdge(source=path.stem, source_file=str(path.resolve()), target=target))
        for match in named_re.finditer(source):
            target = match.group(1)
            edges.append(ImportEdge(source=path.stem, source_file=str(path.resolve()), target=target))
        for match in require_re.finditer(source):
            target = match.group(1)
            edges.append(ImportEdge(source=path.stem, source_file=str(path.resolve()), target=target))
        return edges

    def extract_calls(self, tree: Any, path: Path, source: str) -> list[CallEdge]:
        source = _read_source(path, source)
        parsed = self.parse(source)
        if parsed is None:
            return []
        symbols = self.extract_symbols(parsed, path)
        calls: list[CallEdge] = []
        for symbol in symbols:
            if symbol.kind not in {"function", "method"}:
                continue
            body = _extract_body(source, symbol.start_line + 1, symbol.end_line - 1)
            base = source.splitlines(keepends=True)
            prefix = "".join(base[: max(symbol.start_line - 1, 0)])
            for match in re.finditer(rf"\b({_QUALIFIED_IDENT})\s*\(", body):
                callee = match.group(1)
                if callee in {"if", "for", "while", "switch", "catch", "return", "function", "class", "new"}:
                    continue
                start_offset = len(prefix) + source[: len(prefix)].count("\n")  # unused but keep shape
                body_offset = source.find(body)
                if body_offset < 0:
                    body_offset = 0
                absolute_offset = body_offset + match.start(1)
                line = source.count("\n", 0, absolute_offset) + 1
                line_start = source.rfind("\n", 0, absolute_offset)
                col = absolute_offset - line_start
                calls.append(
                    CallEdge(
                        caller_qname=symbol.qualname if symbol.parent_qualname else symbol.qualname,
                        caller_file=str(path.resolve()),
                        callee=callee,
                        line=line,
                        end_line=line,
                        col=col,
                        end_col=col + len(callee),
                        source=callee + "(",
                    )
                )
        return calls

    def extract_complexity(self, tree: Any, path: Path) -> list[FunctionComplexity]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        symbols = self.extract_symbols(tree, path)
        results: list[FunctionComplexity] = []
        for symbol in symbols:
            if symbol.kind not in {"function", "method"}:
                continue
            body = _extract_body(source, symbol.start_line, symbol.end_line)
            for_loops = len(re.findall(r"\b(for|while)\b", body))
            if_statements = len(re.findall(r"\bif\b", body))
            reasons: list[str] = []
            if for_loops > 8:
                reasons.append("loops")
            if if_statements > 15:
                reasons.append("branches")
            results.append(
                FunctionComplexity(
                    qname=symbol.qualname,
                    file_path=symbol.file_path,
                    name=symbol.name,
                    qualname=symbol.qualname,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    line_count=max(1, symbol.end_line - symbol.start_line + 1),
                    for_loops=for_loops,
                    if_statements=if_statements,
                    is_complex=bool(reasons),
                    reasons=reasons,
                )
            )
        return results

    def insert_code(self, source: str, code: str, anchor: str | None, position: str) -> str:
        stripped = source.rstrip("\n")
        addition = code.strip("\n")
        if not stripped:
            return addition + "\n"
        if position == "before" and anchor:
            idx = source.find(anchor)
            if idx >= 0:
                return source[:idx] + addition + "\n\n" + source[idx:]
        return stripped + "\n\n" + addition + "\n"

    def replace_symbol(self, source: str, symbol: SymbolDetail, new_source: str) -> str:
        lines = source.splitlines(keepends=True)
        start = max(symbol.start_line - 1, 0)
        end = min(symbol.end_line, len(lines))
        replacement = new_source.rstrip("\n")
        if replacement:
            replacement += "\n"
        return "".join(lines[:start] + [replacement] + lines[end:])
