from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Directories never descended into during source scanning.
# Matches _SOURCE_EXCLUDED_DIRS in app.py plus standard hidden/build dirs.
_PRUNE_DIRS: frozenset[str] = frozenset({
    "__pycache__", "node_modules",
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "build", "dist",
    "tests", "test", "testing",
    "docs", "doc",
    "examples", "example",
    "fixtures",
    "migrations", "migration",
    "vendor", "vendors",
})

SymbolKind = Literal["module", "class", "function", "method", "variable"]
Severity = Literal["error", "warning", "info"]


def module_name_for_path(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def qname_for_path(root: Path, path: Path, qualname: str) -> str:
    module = module_name_for_path(root, path)
    return f"{module}:{qualname}" if qualname else f"{module}:"


@dataclass(slots=True)
class Symbol:
    qname: str
    kind: SymbolKind
    file: Path
    line_start: int
    line_end: int
    signature: str | None
    docstring: str | None
    decorators: list[str]
    parent: str | None
    children: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    returns: str | None = None
    imports: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    file: Path
    qname: str | None
    line: int
    code: str
    severity: Severity
    message: str

    def fingerprint(self) -> tuple[str, str | None, str, str]:
        return (str(self.file), self.qname, self.code, self.message)


@dataclass(slots=True)
class Delta:
    introduced: list[Diagnostic]
    fixed: list[tuple[str, str | None, str, str]]


class WorkspaceIndex:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.symbols: dict[str, Symbol] = {}
        self.by_file: dict[Path, list[str]] = {}
        self._scanned = False

    def ensure_scanned(self) -> None:
        if not self._scanned:
            self.scan()

    def ensure_file_scanned(self, path: Path) -> None:
        """Index a single file without triggering a full workspace scan.

        Safe to call before ensure_scanned() — inserts the file's symbols into
        the index so callers can use them immediately, then lets the full scan
        happen lazily on the first tool call that needs it.
        """
        resolved = path.resolve()
        if resolved not in self.by_file and path.exists():
            self._scan_file(resolved)

    def _iter_workspace_source_files(self, langs: set[str] | None = None) -> list[Path]:
        from .lang.router import supported_extensions

        allowed = ({ext.lower() for ext in langs} if langs is not None
                   else supported_extensions())
        paths: list[Path] = []
        for dirpath_str, dirnames, filenames in os.walk(self.root):
            # Prune in-place: os.walk will not descend into removed entries.
            dirnames[:] = [
                d for d in dirnames
                if d not in _PRUNE_DIRS and not d.startswith(".")
            ]
            dirpath = Path(dirpath_str)
            for filename in filenames:
                if filename.endswith(".min.js") or filename.endswith(".bundle.js"):
                    continue
                p = dirpath / filename
                if p.suffix.lower() in allowed:
                    paths.append(p)
        return sorted(paths)

    def _iter_workspace_python_files(self) -> list[Path]:
        return [path for path in self._iter_workspace_source_files({".py"})]

    def _scan_file(self, path: Path) -> list[Symbol]:
        from .lang.router import adapter_for

        adapter = adapter_for(path)
        if adapter is None or not path.exists():
            return []
        source = path.read_text(encoding="utf-8")
        tree = adapter.parse(source)
        if tree is None:
            return []
        module_name = self._module_qname(path).rstrip(":")
        module_qname = f"{module_name}:"
        imports = [edge.target for edge in adapter.extract_imports(tree, path)]
        module_symbol = Symbol(
            qname=module_qname,
            kind="module",
            file=path.resolve(),
            line_start=1,
            line_end=max(len(source.splitlines()), 1),
            signature=module_name,
            docstring=None,
            decorators=[],
            parent=None,
            imports=imports,
        )
        symbols = [module_symbol]
        for detail in adapter.extract_symbols(tree, path):
            if detail.kind == "module":
                continue
            qname = qname_for_path(self.root, path, detail.qualname)
            parent = None
            if detail.parent_qualname is not None:
                parent = qname_for_path(self.root, path, detail.parent_qualname)
            elif detail.kind != "module":
                parent = module_qname
            symbol = Symbol(
                qname=qname,
                kind=detail.kind,  # type: ignore[arg-type]
                file=path.resolve(),
                line_start=detail.start_line,
                line_end=detail.end_line,
                signature=detail.signature,
                docstring=detail.docstring,
                decorators=list(detail.decorators),
                parent=parent,
                children=[],
                bases=list(detail.bases),
                returns=detail.returns,
                imports=list(detail.imports),
            )
            symbols.append(symbol)
        for symbol in symbols:
            self.symbols[symbol.qname] = symbol
        self.by_file[path.resolve()] = [symbol.qname for symbol in symbols]
        self._link_children(path.resolve())
        return symbols

    def _link_children(self, path: Path) -> None:
        qnames = self.by_file.get(path, [])
        for qname in qnames:
            symbol = self.symbols[qname]
            symbol.children = []
        for qname in qnames:
            symbol = self.symbols[qname]
            if symbol.parent and symbol.parent in self.symbols:
                parent = self.symbols[symbol.parent]
                if qname not in parent.children:
                    parent.children.append(qname)
        for qname in qnames:
            symbol = self.symbols[qname]
            symbol.children.sort()

    def scan(self) -> None:
        self.symbols.clear()
        self.by_file.clear()
        for path in self._iter_workspace_source_files():
            self._scan_file(path)
        self._scanned = True

    def reindex_file(self, path: Path) -> None:
        resolved = path.resolve()
        for qname in self.by_file.get(resolved, []):
            self.symbols.pop(qname, None)
        self.by_file.pop(resolved, None)
        self._scan_file(resolved)

    def resolve(self, qname: str) -> Symbol:
        self.ensure_scanned()
        if qname not in self.symbols:
            raise KeyError(qname)
        return self.symbols[qname]

    def children_of(self, qname: str) -> list[str]:
        self.ensure_scanned()
        return list(self.resolve(qname).children)

    def locate(self, qname: str) -> str:
        self.ensure_scanned()
        symbol = self.resolve(qname)
        return f"{symbol.file}:{symbol.line_start}-{symbol.line_end}"

    def symbol_at(self, path: Path, line: int) -> Symbol | None:
        self.ensure_scanned()
        qnames = self.by_file.get(path.resolve(), [])
        candidates = [self.symbols[qname] for qname in qnames if self.symbols[qname].line_start <= line <= self.symbols[qname].line_end]
        if not candidates:
            return None
        candidates.sort(key=lambda item: ((item.line_end - item.line_start), -item.line_start))
        return candidates[0]

    def _module_qname(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts) if parts else path.stem
        return f"{module}:"

    def outline(self, module_qname: str, verbose: bool = False) -> str:
        self.ensure_scanned()
        qname = module_qname if module_qname.endswith(":") else f"{module_qname}:"
        module = self.resolve(qname)
        lines = [self._format_outline_line(module, verbose)]
        for child in module.children:
            lines.extend(self._outline_recursive(child, verbose, indent=1))
        return "\n".join(lines)

    def _outline_recursive(self, qname: str, verbose: bool, indent: int) -> list[str]:
        symbol = self.resolve(qname)
        lines = [self._format_outline_line(symbol, verbose, indent)]
        for child in symbol.children:
            lines.extend(self._outline_recursive(child, verbose, indent + 1))
        return lines

    def _format_outline_line(self, symbol: Symbol, verbose: bool, indent: int = 0) -> str:
        prefix = "  " * indent
        base = f"{symbol.qname} [{symbol.kind}]"
        if symbol.kind == "module" and symbol.imports:
            base += f" deps:{','.join(symbol.imports)}"
        if verbose:
            base += f" {symbol.file}:{symbol.line_start}-{symbol.line_end}"
            if symbol.signature:
                base += f" :: {symbol.signature}"
        return prefix + base

    def symbols_for_file(self, path: Path) -> list[Symbol]:
        self.ensure_scanned()
        return [self.symbols[qname] for qname in self.by_file.get(path.resolve(), [])]


@dataclass(slots=True)
class DiagnosticStore:
    root: Path
    index: WorkspaceIndex
    current: dict[tuple[str, str | None, str, str], Diagnostic] = field(default_factory=dict)

    def snapshot(self) -> set[tuple[str, str | None, str, str]]:
        return set(self.current)

    def _qname_for_location(self, path: Path, line: int) -> str | None:
        symbol = self.index.symbol_at(path, line)
        return None if symbol is None else symbol.qname

    def refresh(self, files: list[Path]) -> None:
        files = [path.resolve() for path in files]
        for path in files:
            for key in [key for key, diag in self.current.items() if diag.file.resolve() == path]:
                self.current.pop(key, None)

        if not files:
            return

        cmd = [
            "ruff",
            "check",
            "--output-format=json",
            *[str(path) for path in files],
        ]
        try:
            result = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            return
        except FileNotFoundError:
            return
        payload = result.stdout.strip()
        if not payload:
            return
        try:
            diagnostics = json.loads(payload)
        except json.JSONDecodeError:
            return

        for item in diagnostics:
            path = Path(item.get("filename") or item.get("file") or item.get("path") or "")
            if not path.is_absolute():
                path = (self.root / path).resolve()
            line = int(item.get("location", {}).get("row") or item.get("row") or item.get("line") or 1)
            qname = self._qname_for_location(path, line)
            severity = item.get("severity") or "warning"
            diagnostic = Diagnostic(
                file=path,
                qname=qname,
                line=line,
                code=item.get("code", "ruff"),
                severity=severity if severity in {"error", "warning", "info"} else "warning",
                message=item.get("message", ""),
            )
            self.current[diagnostic.fingerprint()] = diagnostic

    def delta(self, before: set[tuple[str, str | None, str, str]]) -> Delta:
        after = set(self.current)
        _sort_key = lambda t: (t[0], t[1] or "", t[2], t[3])
        introduced = [self.current[key] for key in sorted(after - before, key=_sort_key)]
        fixed = sorted(before - after, key=_sort_key)
        return Delta(introduced=introduced, fixed=fixed)

    def filter(self, qname: str) -> list[Diagnostic]:
        return [
            diagnostic
            for diagnostic in self.current.values()
            if diagnostic.qname == qname or (diagnostic.qname is not None and diagnostic.qname.startswith(qname + "."))
        ]
