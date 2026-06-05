from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


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
    imports: list[str] = field(default_factory=list)
    node: Any = None


@dataclass(slots=True)
class ImportEdge:
    source: str
    source_file: str
    target: str
    target_file: str | None = None
    kind: str = "import"
    detail: str | None = None


@dataclass(slots=True)
class CallEdge:
    caller_qname: str
    caller_file: str
    callee: str
    line: int
    end_line: int
    col: int
    end_col: int
    source: str
    resolved_qname: str | None = None


@dataclass(slots=True)
class FunctionComplexity:
    qname: str
    file_path: str
    name: str
    qualname: str
    start_line: int
    end_line: int
    line_count: int
    for_loops: int
    if_statements: int
    is_complex: bool
    reasons: list[str] = field(default_factory=list)


@runtime_checkable
class LanguageAdapter(Protocol):
    extensions: frozenset[str]

    def parse(self, source: str) -> Any | None: ...

    def extract_symbols(self, tree: Any, path: Path) -> list[SymbolDetail]: ...

    def extract_imports(self, tree: Any, path: Path) -> list[ImportEdge]: ...

    def extract_calls(self, tree: Any, path: Path, source: str) -> list[CallEdge]: ...

    def extract_complexity(self, tree: Any, path: Path) -> list[FunctionComplexity]: ...

    def insert_code(self, source: str, code: str, anchor: str | None, position: str) -> str: ...

    def replace_symbol(self, source: str, symbol: SymbolDetail, new_source: str) -> str: ...
