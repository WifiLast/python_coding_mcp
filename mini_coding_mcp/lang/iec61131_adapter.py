from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .adapter import CallEdge, FunctionComplexity, ImportEdge, LanguageAdapter, SymbolDetail

_ST_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_BLOCK_COMMENT_RE = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_CALL_RE = re.compile(rf"\b(?P<name>{_ST_IDENT}(?:\^?\.\s*{_ST_IDENT})?)\s*\(", re.IGNORECASE)
_VAR_SECTION_RE = re.compile(r"(^\s*VAR(?:_INPUT|_OUTPUT|_IN_OUT|_EXTERNAL)?\b[\s\S]*?^\s*END_VAR\b)", re.IGNORECASE | re.MULTILINE)
_VAR_DECL_RE = re.compile(rf"^\s*(?P<name>{_ST_IDENT})(?:\s+AT\s+%\w+(?:\.\d+)*)?\s*:\s*(?P<type>[^;]+);", re.IGNORECASE | re.MULTILINE)
_PRAGMA_RE = re.compile(r"\{\s*(?:include|library)\s+['\"]?([^'\"\}]+)['\"]?\s*\}", re.IGNORECASE)
_USES_RE = re.compile(r"^\s*USES\s+([^;\n]+)\s*;?", re.IGNORECASE | re.MULTILINE)
_FROM_IMPORT_RE = re.compile(r"^\s*FROM\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+IMPORT\s+([^;\n]+)", re.IGNORECASE | re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*IMPORT\s+([A-Za-z_][A-Za-z0-9_\.]*)\s*;?", re.IGNORECASE | re.MULTILINE)

_POU_END_KEYWORDS = {
    "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
    "FUNCTION": "END_FUNCTION",
    "PROGRAM": "END_PROGRAM",
    "TYPE": "END_TYPE",
    "INTERFACE": "END_INTERFACE",
}

_POU_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("class", re.compile(rf"^\s*FUNCTION_BLOCK\s+(?P<name>{_ST_IDENT})\b", re.IGNORECASE | re.MULTILINE), _POU_END_KEYWORDS["FUNCTION_BLOCK"]),
    ("function", re.compile(rf"^\s*FUNCTION\s+(?P<name>{_ST_IDENT})\s*:\s*(?P<returns>[^\r\n]+)", re.IGNORECASE | re.MULTILINE), _POU_END_KEYWORDS["FUNCTION"]),
    ("class", re.compile(rf"^\s*PROGRAM\s+(?P<name>{_ST_IDENT})\b", re.IGNORECASE | re.MULTILINE), _POU_END_KEYWORDS["PROGRAM"]),
    ("class", re.compile(rf"^\s*TYPE\s+(?P<name>{_ST_IDENT})\b", re.IGNORECASE | re.MULTILINE), _POU_END_KEYWORDS["TYPE"]),
    ("interface", re.compile(rf"^\s*INTERFACE\s+(?P<name>{_ST_IDENT})\b", re.IGNORECASE | re.MULTILINE), _POU_END_KEYWORDS["INTERFACE"]),
]

_CALL_KEYWORDS = {
    "and",
    "case",
    "do",
    "else",
    "elsif",
    "end_case",
    "end_for",
    "end_function",
    "end_function_block",
    "end_if",
    "end_interface",
    "end_program",
    "end_repeat",
    "end_type",
    "end_while",
    "for",
    "if",
    "not",
    "of",
    "repeat",
    "then",
    "to",
    "until",
    "while",
}


def _read_source(path: Path, source: str | None = None) -> str:
    if source is not None:
        return source
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_comments(source: str) -> str:
    def _repl_block(match: re.Match[str]) -> str:
        text = match.group(0)
        return "\n" * text.count("\n")

    stripped = _BLOCK_COMMENT_RE.sub(_repl_block, source)
    return _LINE_COMMENT_RE.sub("", stripped)


def _line_col_from_offset(source: str, offset: int) -> tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    line_start = source.rfind("\n", 0, offset)
    if line_start < 0:
        line_start = -1
    return line, offset - line_start


def _slice_lines(source: str, start_line: int, end_line: int) -> str:
    lines = source.splitlines(keepends=True)
    return "".join(lines[max(start_line - 1, 0) : min(end_line, len(lines))])


def _body_offset_in_source(source: str, start_line: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(lines[i]) for i in range(max(start_line - 1, 0)))


def _line_end_offset(source: str, offset: int) -> int:
    newline = source.find("\n", offset)
    return len(source) if newline < 0 else newline + 1


def _find_keyword_after(source: str, start_offset: int, keyword: str) -> int | None:
    pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
    match = pattern.search(source, start_offset)
    if match is None:
        return None
    return _line_end_offset(source, match.end())


def _find_header_limit(source: str, start_offset: int, limit: int | None = None) -> int:
    marker = re.compile(
        r"^\s*(?:VAR(?:_INPUT|_OUTPUT|_IN_OUT|_EXTERNAL)?|[A-Za-z_][A-Za-z0-9_]*\s*:=)",
        re.IGNORECASE | re.MULTILINE,
    )
    end = len(source) if limit is None else min(limit, len(source))
    match = marker.search(source, start_offset, end)
    return len(source) if match is None else match.start()


def _qualified_symbol(path: Path, qualname: str) -> str:
    return f"{path.stem}:{qualname}" if qualname else f"{path.stem}:"


def _pou_signature(kind: str, name: str, returns: str | None = None, bases: list[str] | None = None) -> str:
    if kind == "function":
        return f"FUNCTION {name} : {returns or 'VOID'}"
    if kind == "class" and bases:
        return f"{kind.upper()} {name} EXTENDS {', '.join(bases)}"
    return f"{kind.upper()} {name}"


def _parse_bases(header: str) -> list[str]:
    bases: list[str] = []
    extends = re.search(r"\bEXTENDS\s+([A-Za-z_][A-Za-z0-9_]*)", header, re.IGNORECASE)
    if extends:
        bases.append(extends.group(1))
    implements = re.search(r"\bIMPLEMENTS\s+([A-Za-z0-9_,\s]+)", header, re.IGNORECASE)
    if implements:
        for base in implements.group(1).split(","):
            candidate = base.strip()
            if candidate:
                bases.append(candidate)
    return bases


def _extract_docstring(header: str) -> str | None:
    match = re.search(r"\{(?:attribute\s+'docstring'|doc\b[^}]*)\}", header, re.IGNORECASE)
    if match is None:
        return None
    return match.group(0)


def _clean_type_text(type_text: str) -> str:
    return re.sub(r"\s+(CONSTANT|AT\s+%\w+(?:\.\d+)*)\s*$", "", type_text.strip(), flags=re.IGNORECASE)


def _mask_comments(source: str) -> str:
    chars = list(source)
    for match in _BLOCK_COMMENT_RE.finditer(source):
        for idx in range(match.start(), match.end()):
            if chars[idx] != "\n":
                chars[idx] = " "
    for match in _LINE_COMMENT_RE.finditer(source):
        for idx in range(match.start(), match.end()):
            if chars[idx] != "\n":
                chars[idx] = " "
    return "".join(chars)


def _extract_variable_symbols(path: Path, source: str, pou_name: str, parent_qualname: str, start_offset: int, end_offset: int) -> list[SymbolDetail]:
    section = source[start_offset:end_offset]
    masked = _mask_comments(section)
    results: list[SymbolDetail] = []

    for section_match in _VAR_SECTION_RE.finditer(masked):
        section_text = section_match.group(1)
        section_start = start_offset + section_match.start(1)
        for decl in _VAR_DECL_RE.finditer(section_text):
            name = decl.group("name").strip()
            type_text = _clean_type_text(decl.group("type"))
            decl_start = section_start + decl.start()
            decl_end = section_start + decl.end()
            line_start, col_start = _line_col_from_offset(source, decl_start)
            line_end, col_end = _line_col_from_offset(source, decl_end)
            qualname = f"{parent_qualname}.{name}"
            results.append(
                SymbolDetail(
                    file_path=str(path.resolve()),
                    kind="variable",
                    name=name,
                    qualname=qualname,
                    parent_qualname=parent_qualname,
                    start_line=line_start,
                    end_line=max(line_end, line_start),
                    start_col=col_start,
                    end_col=col_end,
                    signature=None,
                    docstring=None,
                    decorators=[],
                    bases=[],
                    returns=type_text,
                    source=_slice_lines(source, line_start, max(line_end, line_start)).strip() or decl.group(0).strip(),
                    node=None,
                )
            )
    return results


class IEC61131Adapter(LanguageAdapter):
    extensions = frozenset({".st", ".scl", ".iec"})

    def parse(self, source: str) -> Any | None:
        return {"source": source, "lines": source.splitlines(keepends=True), "stripped": _strip_comments(source)}

    def extract_symbols(self, tree: Any, path: Path) -> list[SymbolDetail]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        masked = _mask_comments(source)
        symbols: list[SymbolDetail] = []

        pou_matches: list[dict[str, Any]] = []
        for kind, pattern, end_keyword in _POU_PATTERNS:
            for match in pattern.finditer(masked):
                pou_matches.append(
                    {
                        "kind": kind,
                        "match": match,
                        "end_keyword": end_keyword,
                    }
                )

        pou_matches.sort(key=lambda item: item["match"].start())
        for index, item in enumerate(pou_matches):
            kind = item["kind"]
            match = item["match"]
            end_keyword = item["end_keyword"]
            name = match.group("name")
            start = match.start()
            next_start = pou_matches[index + 1]["match"].start() if index + 1 < len(pou_matches) else len(masked)
            header_limit = _find_header_limit(masked, match.end(), next_start)
            end = _find_keyword_after(masked, header_limit, end_keyword)
            if end is None or end > next_start:
                end = next_start
            if end < start:
                end = start
            start_line, start_col = _line_col_from_offset(source, start)
            end_line, end_col = _line_col_from_offset(source, end)
            header_text = source[start:header_limit]
            bases = _parse_bases(header_text)
            returns = match.groupdict().get("returns")
            qualname = name
            signature = _pou_signature(kind, name, returns=returns.strip() if isinstance(returns, str) else None, bases=bases)
            docstring = _extract_docstring(header_text)
            symbol = SymbolDetail(
                file_path=str(path.resolve()),
                kind="interface" if kind == "interface" else kind,
                name=name,
                qualname=qualname,
                parent_qualname=None,
                start_line=start_line,
                end_line=max(end_line, start_line),
                start_col=start_col,
                end_col=end_col,
                signature=signature,
                docstring=docstring,
                decorators=[],
                bases=bases,
                returns=returns.strip() if isinstance(returns, str) else None,
                source=_slice_lines(source, start_line, max(end_line, start_line)).strip() or source[start:end].strip(),
                node=None,
            )
            symbols.append(symbol)
            symbols.extend(_extract_variable_symbols(path, source, name, qualname, start, end))

        symbols.sort(key=lambda item: (item.start_line, item.start_col, item.qualname))
        return symbols

    def extract_imports(self, tree: Any, path: Path) -> list[ImportEdge]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        stripped = tree["stripped"] if isinstance(tree, dict) else _mask_comments(source)
        edges: list[ImportEdge] = []
        seen: set[tuple[str, str | None, str]] = set()

        def _add(target: str, kind: str, detail: str | None = None) -> None:
            key = (target, detail, kind)
            if key in seen:
                return
            seen.add(key)
            edges.append(
                ImportEdge(
                    source=path.stem,
                    source_file=str(path.resolve()),
                    target=target,
                    target_file=None,
                    kind=kind,
                    detail=detail,
                )
            )

        for match in _PRAGMA_RE.finditer(stripped):
            _add(match.group(1).strip(), "pragma")
        for match in _USES_RE.finditer(stripped):
            for name in match.group(1).split(","):
                candidate = name.strip()
                if candidate:
                    _add(candidate, "uses")
        for match in _FROM_IMPORT_RE.finditer(stripped):
            library = match.group(1).strip()
            for name in match.group(2).split(","):
                candidate = name.strip()
                if candidate:
                    _add(candidate, "from_import", detail=library)
        for match in _IMPORT_RE.finditer(stripped):
            _add(match.group(1).strip(), "import")

        return edges

    def extract_calls(self, tree: Any, path: Path, source: str) -> list[CallEdge]:
        source = _read_source(path, source)
        parsed = self.parse(source)
        if parsed is None:
            return []
        symbols = self.extract_symbols(parsed, path)
        calls: list[CallEdge] = []

        for symbol in symbols:
            if symbol.kind not in {"function", "class"}:
                continue
            body = _slice_lines(source, symbol.start_line, symbol.end_line)
            body_masked = _mask_comments(body)
            for match in _CALL_RE.finditer(body_masked):
                callee = re.sub(r"\s+", "", match.group("name"))
                callee_tail = callee.split(".")[-1].split("^")[-1]
                if callee_tail.lower() in _CALL_KEYWORDS:
                    continue
                relative_offset = match.start("name")
                absolute_offset = _body_offset_in_source(source, symbol.start_line) + relative_offset
                line, col = _line_col_from_offset(source, absolute_offset)
                calls.append(
                    CallEdge(
                        caller_qname=symbol.qualname,
                        caller_file=str(path.resolve()),
                        callee=callee,
                        line=line,
                        end_line=line,
                        col=col,
                        end_col=col + len(callee),
                        source=match.group(0),
                    )
                )
        calls.sort(key=lambda item: (item.line, item.col, item.callee))
        return calls

    def extract_complexity(self, tree: Any, path: Path) -> list[FunctionComplexity]:
        source = tree["source"] if isinstance(tree, dict) else _read_source(path)
        parsed = self.parse(source)
        if parsed is None:
            return []
        results: list[FunctionComplexity] = []
        for symbol in self.extract_symbols(parsed, path):
            if symbol.kind not in {"function", "class"}:
                continue
            body = _slice_lines(source, symbol.start_line, symbol.end_line)
            stripped = _strip_comments(body)
            for_loops = len(re.findall(r"\b(?:FOR|WHILE|REPEAT)\b", stripped, re.IGNORECASE))
            if_statements = len(re.findall(r"\b(?:IF|CASE)\b", stripped, re.IGNORECASE))
            reasons: list[str] = []
            if for_loops > 8:
                reasons.append("loops")
            if if_statements > 15:
                reasons.append("branches")
            results.append(
                FunctionComplexity(
                    qname=_qualified_symbol(path, symbol.qualname),
                    file_path=str(path.resolve()),
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
        if not addition:
            return source
        if anchor:
            parsed = self.parse(source)
            if parsed is not None:
                anchor_name = anchor.split(":")[-1].split(".")[-1]
                for symbol in self.extract_symbols(parsed, Path("placeholder.st")):
                    if symbol.name != anchor_name:
                        continue
                    if position == "before":
                        return _slice_lines(source, 1, symbol.start_line - 1).rstrip("\n") + ("\n\n" if symbol.start_line > 1 else "") + addition + "\n\n" + _slice_lines(source, symbol.start_line, len(source.splitlines()))
                    return _slice_lines(source, 1, symbol.end_line).rstrip("\n") + "\n\n" + addition + "\n\n" + _slice_lines(source, symbol.end_line + 1, len(source.splitlines()))
        if not stripped:
            return addition + "\n"
        return stripped + "\n\n" + addition + "\n"

    def replace_symbol(self, source: str, symbol: SymbolDetail, new_source: str) -> str:
        lines = source.splitlines(keepends=True)
        start = max(symbol.start_line - 1, 0)
        end = min(symbol.end_line, len(lines))
        replacement = new_source.rstrip("\n")
        if replacement:
            replacement += "\n"
        return "".join(lines[:start] + [replacement] + lines[end:])
