from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AnalysisRules:
    max_for_loops: int = 8
    max_if_statements: int = 15
    max_lines_per_function: int = 100
    max_nesting_depth: int = 4
    repetition_window: int = 3
    repetition_threshold: int = 2

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "AnalysisRules":
        rules = cls()
        for key, value in payload.items():
            if hasattr(rules, key):
                setattr(rules, key, int(value))
        return rules


@dataclass(slots=True)
class FunctionAnalysis:
    file_path: str
    name: str
    qualname: str
    definition_start_line: int
    definition_end_line: int
    start_line: int
    end_line: int
    body_start_line: int
    body_end_line: int
    start_col: int
    end_col: int
    line_count: int
    for_loops: int
    if_statements: int
    max_nesting_depth: int
    repeated_sequences: list[dict[str, Any]] = field(default_factory=list)
    is_complex: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FunctionRecord:
    file_path: str
    name: str
    qualname: str
    class_path: tuple[str, ...]
    start_line: int
    end_line: int
    start_col: int
    end_col: int
    node: ast.AST


class FunctionScanner(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stack: list[tuple[str, str]] = []
        self.functions: list[FunctionRecord] = []

    def _qualname(self, name: str) -> str:
        if not self.stack:
            return name
        return ".".join([*(scope_name for scope_name, _ in self.stack), name])

    def _class_path(self) -> tuple[str, ...]:
        return tuple(scope_name for scope_name, scope_kind in self.stack if scope_kind == "class")

    def _append(self, node: ast.AST, name: str) -> None:
        self.functions.append(
            FunctionRecord(
                file_path=str(self.path),
                name=name,
                qualname=self._qualname(name),
                class_path=self._class_path(),
                start_line=getattr(node, "lineno", 1),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                start_col=getattr(node, "col_offset", 0) + 1,
                end_col=getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) + 1,
                node=node,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.stack.append((node.name, "class"))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._append(node, node.name)
        self.stack.append((node.name, "function"))
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._append(node, node.name)
        self.stack.append((node.name, "function"))
        self.generic_visit(node)
        self.stack.pop()


class ComplexityCounter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.for_loops = 0
        self.if_statements = 0
        self.current_depth = 0
        self.max_nesting_depth = 0

    def _block(self, node: ast.AST) -> Any:
        self.current_depth += 1
        self.max_nesting_depth = max(self.max_nesting_depth, self.current_depth)
        self.generic_visit(node)
        self.current_depth -= 1

    def visit_For(self, node: ast.For) -> Any:
        self.for_loops += 1
        self._block(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> Any:
        self.for_loops += 1
        self._block(node)

    def visit_If(self, node: ast.If) -> Any:
        self.if_statements += 1
        self._block(node)

    def visit_While(self, node: ast.While) -> Any:
        self._block(node)

    def visit_With(self, node: ast.With) -> Any:
        self._block(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> Any:
        self._block(node)

    def visit_Try(self, node: ast.Try) -> Any:
        self._block(node)

    def visit_Match(self, node: ast.Match) -> Any:
        self._block(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        return None


def load_analysis_rules(config_path: Path | None = None) -> AnalysisRules:
    config_path = config_path or Path(__file__).with_name("config.json")
    payload: dict[str, Any] = {}
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            payload = {}
    return AnalysisRules.from_mapping(payload.get("analysis_rules", {}) or {})


def _read_source(path: Path) -> str:
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_module(path: Path) -> ast.Module | None:
    try:
        return ast.parse(_read_source(path))
    except SyntaxError:
        return None


def _normalized_statement(stmt: ast.AST) -> str:
    return ast.dump(stmt, include_attributes=False)


def _statement_source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ast.unparse(node)


def _effective_line_count(lines: list[str], start_line: int, end_line: int) -> int:
    count = 0
    for line_no in range(max(start_line - 1, 0), min(end_line, len(lines))):
        if lines[line_no].lstrip().startswith("#"):
            continue
        count += 1
    return count


def _collect_unused_imports(tree: ast.AST, source: str, path: Path) -> list[dict[str, Any]]:
    imported: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(
                    {
                        "name": alias.asname or alias.name.split(".")[-1],
                        "module": alias.name,
                        "line": getattr(node, "lineno", 1),
                        "kind": "import",
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + node.module if node.module else "." * node.level
            for alias in node.names:
                imported.append(
                    {
                        "name": alias.asname or alias.name.split(".")[-1],
                        "module": module,
                        "line": getattr(node, "lineno", 1),
                        "kind": "from_import",
                    }
                )
    used_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    unused: list[dict[str, Any]] = []
    for item in imported:
        if item["name"] in used_names:
            continue
        unused.append(
            {
                "file_path": str(path),
                "name": item["name"],
                "module": item["module"],
                "kind": item["kind"],
                "line": item["line"],
                "source": source.splitlines()[max(item["line"] - 1, 0)].strip() if source else "",
            }
        )
    return unused


def _find_repeated_sequences(source: str, node: FunctionRecord, window: int, threshold: int) -> list[dict[str, Any]]:
    if not isinstance(node.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    body = list(node.node.body)
    if len(body) < window * threshold:
        return []

    sequences: dict[tuple[str, ...], list[int]] = {}
    for index in range(0, len(body) - window + 1):
        signature = tuple(_normalized_statement(stmt) for stmt in body[index : index + window])
        sequences.setdefault(signature, []).append(index)

    repeated: list[dict[str, Any]] = []
    for signature, positions in sequences.items():
        if len(positions) < threshold:
            continue
        first = positions[0]
        statements = body[first : first + window]
        repeated.append(
            {
                "window": window,
                "occurrences": [
                    {
                        "start_line": getattr(body[pos], "lineno", node.start_line),
                        "end_line": getattr(body[pos + window - 1], "end_lineno", getattr(body[pos + window - 1], "lineno", node.end_line)),
                    }
                    for pos in positions
                ],
                "statements": [_statement_source(source, stmt) for stmt in statements],
            }
        )
    return repeated


def analyze_function(
    source: str,
    record: FunctionRecord,
    rules: AnalysisRules,
    source_lines: list[str] | None = None,
) -> FunctionAnalysis:
    if not isinstance(record.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise TypeError("record node is not a function")

    counter = ComplexityCounter()
    for statement in record.node.body:
        counter.visit(statement)

    body_start_line = record.start_line
    body_end_line = record.end_line
    if record.node.body:
        first_stmt = record.node.body[0]
        last_stmt = record.node.body[-1]
        body_start_line = getattr(first_stmt, "lineno", record.start_line)
        body_end_line = getattr(last_stmt, "end_lineno", getattr(last_stmt, "lineno", record.end_line))

    if source_lines is None:
        source_lines = source.splitlines()

    line_count = _effective_line_count(source_lines, body_start_line, body_end_line)
    repeated_sequences = _find_repeated_sequences(source, record, rules.repetition_window, rules.repetition_threshold)

    reasons: list[str] = []
    if counter.for_loops > rules.max_for_loops:
        reasons.append(f"for_loops>{rules.max_for_loops}")
    if counter.if_statements > rules.max_if_statements:
        reasons.append(f"if_statements>{rules.max_if_statements}")
    if counter.max_nesting_depth > rules.max_nesting_depth:
        reasons.append(f"nesting_depth>{rules.max_nesting_depth}")
    if line_count > rules.max_lines_per_function:
        reasons.append(f"lines>{rules.max_lines_per_function}")
    if repeated_sequences:
        reasons.append("repeated_sequences")

    return FunctionAnalysis(
        file_path=record.file_path,
        name=record.name,
        qualname=record.qualname,
        definition_start_line=record.start_line,
        definition_end_line=record.end_line,
        start_line=record.start_line,
        end_line=record.end_line,
        body_start_line=body_start_line,
        body_end_line=body_end_line,
        start_col=record.start_col,
        end_col=record.end_col,
        line_count=line_count,
        for_loops=counter.for_loops,
        if_statements=counter.if_statements,
        max_nesting_depth=counter.max_nesting_depth,
        repeated_sequences=repeated_sequences,
        is_complex=bool(reasons),
        reasons=reasons,
    )


def analyze_workspace(root: Path, config_path: Path | None = None, files: list[Path] | None = None) -> dict[str, Any]:
    root = root.resolve()
    rules = load_analysis_rules(config_path)
    complex_functions: list[dict[str, Any]] = []
    unused_imports: list[dict[str, Any]] = []
    analyzed_count = 0

    if files is None:
        paths = sorted(root.rglob("*.py"))
        paths = [
            path
            for path in paths
            if not any(part.startswith(".") and part not in {".", ".."} for part in path.relative_to(root).parts)
            and "__pycache__" not in path.parts
        ]
    else:
        paths = sorted({path.resolve() for path in files})

    for path in paths:
        tree = _parse_module(path)
        if tree is None:
            continue
        source = _read_source(path)
        source_lines = source.splitlines()
        unused_imports.extend(_collect_unused_imports(tree, source, path))
        scanner = FunctionScanner(path)
        scanner.visit(tree)
        for record in scanner.functions:
            analyzed_count += 1
            analysis = analyze_function(source, record, rules, source_lines=source_lines)
            if analysis.is_complex:
                complex_functions.append(
                    {
                        "file_path": analysis.file_path,
                        "name": analysis.name,
                        "qualname": analysis.qualname,
                        "definition_start_line": analysis.definition_start_line,
                        "definition_end_line": analysis.definition_end_line,
                        "start_line": analysis.start_line,
                        "end_line": analysis.end_line,
                        "body_start_line": analysis.body_start_line,
                        "body_end_line": analysis.body_end_line,
                        "start_col": analysis.start_col,
                        "end_col": analysis.end_col,
                        "line_count": analysis.line_count,
                        "for_loops": analysis.for_loops,
                        "if_statements": analysis.if_statements,
                        "max_nesting_depth": analysis.max_nesting_depth,
                        "repeated_sequences": analysis.repeated_sequences,
                        "reasons": analysis.reasons,
                        "suggestion": "Split this function into smaller functions that isolate repeated logic and nested branching.",
                    }
                )

    return {
        "root": str(root),
        "rules": asdict(rules),
        "summary": {
            "analyzed_functions": analyzed_count,
            "complex_functions": len(complex_functions),
            "unused_imports": len(unused_imports),
        },
        "complex_functions": complex_functions,
        "unused_imports": unused_imports,
    }
