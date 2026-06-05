from __future__ import annotations

import ast
from typing import Literal

BLOCK_KIND = Literal["import", "constant", "definition", "raw"]


def _line_index(source: str) -> list[str]:
    return source.splitlines(keepends=True)


def _range_slice(lines: list[str], start_line: int, end_line: int) -> str:
    start_idx = max(start_line - 1, 0)
    end_idx = max(end_line, 0)
    return "".join(lines[start_idx:end_idx])


def _classify_block(block: str) -> BLOCK_KIND:
    try:
        node = ast.parse(block)
    except SyntaxError:
        return "raw"
    if not node.body:
        return "raw"
    kinds = {type(item) for item in node.body}
    if kinds.issubset({ast.Import, ast.ImportFrom}):
        return "import"
    if all(isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign)) for item in node.body):
        return "constant"
    if any(isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for item in node.body):
        return "definition"
    return "raw"


def _split_snippet(snippet: str) -> list[tuple[BLOCK_KIND, str]]:
    try:
        tree = ast.parse(snippet)
    except SyntaxError:
        return [("raw", snippet)]

    if not tree.body:
        return [("raw", snippet)]

    lines = _line_index(snippet)
    blocks: list[tuple[BLOCK_KIND, str]] = []
    for node in tree.body:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        block = _range_slice(lines, start, end)
        kind = _classify_block(block)
        blocks.append((kind, block))
    return blocks


def _append_block(source: str, block: str) -> str:
    if not source:
        return block.rstrip() + "\n"
    trimmed = source.rstrip("\n")
    addition = block.strip("\n")
    if not addition:
        return source
    return trimmed + "\n\n" + addition + "\n"


def _compose_sections(prefix: str, block: str, suffix: str = "") -> str:
    prefix = prefix.rstrip("\n")
    block = block.strip("\n")
    suffix = suffix.lstrip("\n")
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if block:
        if parts:
            parts.append("\n\n")
        parts.append(block)
    if suffix:
        if parts:
            parts.append("\n\n")
        parts.append(suffix)
    text = "".join(parts)
    return text.rstrip("\n") + ("\n" if text else "")


def insert_after_line(source: str, line_no: int, block: str) -> str:
    lines = _line_index(source)
    insert_at = min(max(line_no, 0), len(lines))
    prefix = "".join(lines[:insert_at])
    suffix = "".join(lines[insert_at:])
    return _compose_sections(prefix, block, suffix)


def _module_docstring_end(tree: ast.Module) -> int:
    if not tree.body:
        return 0
    first = tree.body[0]
    if not isinstance(first, ast.Expr):
        return 0
    value = first.value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return 0
    return getattr(first, "end_lineno", first.lineno)


def _top_level_ranges(tree: ast.Module) -> list[tuple[int, int, ast.AST]]:
    ranges: list[tuple[int, int, ast.AST]] = []
    for node in tree.body:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        ranges.append((start, end, node))
    return ranges


def _parse_python(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _insert_before_first_definition(source: str, block: str, tree: ast.Module | None = None) -> str:
    tree = tree or _parse_python(source)
    if tree is None:
        return _append_block(source, block)

    lines = _line_index(source)
    first_definition_line = None
    for start, _, node in _top_level_ranges(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first_definition_line = start
            break
    if first_definition_line is None:
        return _append_block(source, block)
    prefix = _range_slice(lines, 1, first_definition_line - 1).rstrip("\n")
    suffix = _range_slice(lines, first_definition_line, len(lines))
    return _compose_sections(prefix, block, suffix)


def _insert_imports(source: str, block: str, tree: ast.Module | None = None) -> str:
    tree = tree or _parse_python(source)
    if tree is None:
        return _append_block(source, block)

    docstring_end = _module_docstring_end(tree)
    top_level = _top_level_ranges(tree)
    import_end = docstring_end
    for start, end, node in top_level:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_end = max(import_end, end)
    if import_end == 0:
        return _insert_before_first_definition(source, block, tree=tree)
    return insert_after_line(source, import_end, block)


def _insert_constants(source: str, block: str, tree: ast.Module | None = None) -> str:
    tree = tree or _parse_python(source)
    if tree is None or not tree.body:
        return _append_block(source, block)

    docstring_end = _module_docstring_end(tree)
    top_level = _top_level_ranges(tree)
    insert_after = docstring_end
    for start, end, node in top_level:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            insert_after = max(insert_after, end)
    return insert_after_line(source, insert_after, block)


def _insert_definitions(source: str, block: str, tree: ast.Module | None = None) -> str:
    tree = tree or _parse_python(source)
    if tree is None:
        return _append_block(source, block)

    top_level = _top_level_ranges(tree)
    if not top_level:
        return _append_block(source, block)
    insert_after = max(end for _, end, _ in top_level)
    return insert_after_line(source, insert_after, block)


def apply_python_snippet(source: str, snippet: str, anchor: str | None = None, position: str = "auto") -> str:
    blocks = _split_snippet(snippet)
    for kind, block in blocks:
        tree = _parse_python(source)
        anchor_start = anchor_end = None
        if anchor and tree is not None:
            top_level = _top_level_ranges(tree)
            for start, end, node in top_level:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == anchor:
                    anchor_start = start
                    anchor_end = end
                    break
        if kind == "import":
            source = _insert_imports(source, block, tree=tree)
        elif kind == "constant":
            source = _insert_constants(source, block, tree=tree)
        elif kind == "definition":
            if anchor_start is not None and anchor_end is not None:
                if position == "before":
                    lines = _line_index(source)
                    prefix = _range_slice(lines, 1, anchor_start - 1).rstrip("\n")
                    suffix = _range_slice(lines, anchor_start, len(lines)).lstrip("\n")
                    source = _compose_sections(prefix, block, suffix)
                else:
                    source = insert_after_line(source, anchor_end, block)
            else:
                source = _insert_definitions(source, block, tree=tree)
        else:
            source = _append_block(source, block)
    return source


def replace_source_span(source: str, start_line: int, end_line: int, new_source: str) -> str:
    lines = _line_index(source)
    start_idx = max(start_line - 1, 0)
    end_idx = min(end_line, len(lines))
    replacement = new_source.rstrip("\n")
    if replacement:
        replacement += "\n"
    new_lines = lines[:start_idx] + [replacement] + lines[end_idx:]
    return "".join(new_lines)
