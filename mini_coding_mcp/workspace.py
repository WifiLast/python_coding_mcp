from __future__ import annotations

import ast
import difflib
import logging
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections import deque
from pathlib import Path
from threading import RLock
from typing import Any
from logging.handlers import RotatingFileHandler


from .call_graph import FunctionCallCollector, _resolve_local_import, build_call_graph
from .lang.router import adapter_for, supported_extensions
from .stable_index import (
    _PRUNE_DIRS,
    Diagnostic,
    DiagnosticStore,
    Delta,
    Symbol,
    WorkspaceIndex,
    module_name_for_path,
    qname_for_path,
)
from .symbols import SymbolDetail

# Import AST and indexing helpers from .indexer
from .indexer import (
    find_reference_hits_in_file,
    extract_function_calls,
    extract_symbol_summary,
    symbol_details_to_index_entry,
    validate_single_python_unit,
    function_node_for_symbol,
)

# Import manipulation helpers from .manipulator
from .manipulator import (
    apply_python_snippet,
    replace_source_span,
    insert_after_line,
    _append_block,
    _insert_constants,
    _insert_definitions,
    _insert_imports,
    _split_snippet,
)

# Import app entry points from .app to preserve backwards compatibility.
from .app import create_app, main  # noqa: F401

__all__ = ["MiniWorkspace", "create_app", "main"]

_PLAN_FILE = ".mcp_plan.json"


def _load_plan_payload(root: Path) -> dict[str, Any]:
    plan_path = root / _PLAN_FILE
    if not plan_path.exists():
        return {}
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(key, str)}


class MiniWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = RLock()
        self._workspace_index_cache: list[dict[str, Any]] | None = None
        self._workspace_index_scan_id = 0
        self._workspace_index_snapshots: dict[int, list[dict[str, Any]]] = {0: []}
        # Values are (mtime, entries); mtime=0.0 when stat failed.
        self._file_index_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._ignored_paths: set[Path] = set()
        # Optional subdirectory that relative paths resolve from.
        self._focus_dir: Path | None = None
        self.symbol_index = WorkspaceIndex(self.root)
        self.diagnostic_store = DiagnosticStore(self.root, self.symbol_index)
        self._logger = self._configure_logger()

    def _ensure_symbol_index(self) -> None:
        self.symbol_index.ensure_scanned()

    def _reject_escaped_quotes(self, source: str, field_name: str) -> dict[str, Any] | None:
        if "\\\"" in source:
            return {
                "accepted": False,
                "ok": False,
                "reason": "escaped_quote_sequence",
                "retryable": False,
                "hint": f"Remove JSON-escaped quotes from {field_name}; pass raw Python source instead.",
                "edited": [],
                "introduced": [],
                "fixed_count": 0,
                "delta": {"introduced": 0, "fixed": 0},
            }
        return None

    def _configure_logger(self) -> logging.Logger:
        logger = logging.getLogger("mini_coding_mcp")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
            logger.addHandler(stream_handler)

            log_dir = Path(tempfile.gettempdir()) / "claude_mcp"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "mini_coding_mcp.log"
            file_handler = RotatingFileHandler(log_path, maxBytes=512_000, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(file_handler)
            logger.propagate = False
        return logger

    def _resolve_path(self, destination_file: str | Path) -> Path:
        candidate = Path(destination_file)
        if not candidate.is_absolute():
            if self._focus_dir is not None:
                focus_rel = self._focus_dir.relative_to(self.root)
                try:
                    # Path already starts with the focus prefix — resolve from root
                    # to avoid double-nesting (e.g. focus/focus/file.py).
                    candidate.relative_to(focus_rel)
                    base = self.root
                except ValueError:
                    base = self._focus_dir
            else:
                base = self.root
            candidate = base / candidate
        candidate = candidate.resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise ValueError(f"path escapes workspace root: {destination_file}")
        return candidate

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _parse_python(self, source: str) -> ast.Module | None:
        try:
            return ast.parse(source)
        except SyntaxError:
            return None

    def _public_symbol(self, symbol: Symbol, verbose: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "qname": symbol.qname,
            "kind": symbol.kind,
            "file": str(symbol.file),
            "signature": symbol.signature,
            "docstring": symbol.docstring,
            "decorators": list(symbol.decorators),
            "parent": symbol.parent,
            "children": list(symbol.children),
            "imports": list(symbol.imports),
        }
        if verbose:
            payload.update(
                {
                    "line_start": symbol.line_start,
                    "line_end": symbol.line_end,
                    "locate": self.symbol_index.locate(symbol.qname),
                    "bases": list(symbol.bases),
                    "returns": symbol.returns,
                }
            )
        return payload

    def _public_index_entry(self, entry: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
        payload = {
            "qname": entry.get("qname") or qname_for_path(self.root, Path(entry["file_path"]), entry.get("qualname", "")),
            "name": entry["name"],
            "kind": entry["kind"],
            "signature": entry.get("signature"),
        }
        if verbose:
            payload.update(
                {
                    "file_path": entry["file_path"],
                    "qualname": entry["qualname"],
                    "start_line": entry["start_line"],
                    "end_line": entry["end_line"],
                    "start_col": entry["start_col"],
                    "end_col": entry["end_col"],
                }
            )
        return payload

    def _public_function_match(self, match: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
        payload = {
            "qname": qname_for_path(self.root, Path(match["file_path"]), match["qualname"]),
            "file_path": match["file_path"],
            "name": match["name"],
            "kind": match["kind"],
            "qualname": match["qualname"],
            "source": match["source"],
        }
        if verbose:
            payload.update(
                {
                    "start_line": match["start_line"],
                    "end_line": match["end_line"],
                    "start_col": match["start_col"],
                    "end_col": match["end_col"],
                }
            )
        return payload

    def _public_detail_entry(self, detail: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
        payload = dict(detail)
        if not verbose:
            payload.pop("start_line", None)
            payload.pop("end_line", None)
            payload.pop("start_col", None)
            payload.pop("end_col", None)
        return payload

    def _public_call_match(self, call: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
        payload = {
            "call": call["call"],
            "source": call["source"],
        }
        if verbose:
            payload.update(
                {
                    "file_path": call["file_path"],
                    "line": call["line"],
                    "end_line": call["end_line"],
                    "col": call["col"],
                    "end_col": call["end_col"],
                }
            )
        return payload

    def _symbol_source_text(self, symbol: Symbol) -> str:
        lines = self._read_text(symbol.file).splitlines(keepends=True)
        start_idx = max(symbol.line_start - 1, 0)
        end_idx = min(symbol.line_end, len(lines))
        return "".join(lines[start_idx:end_idx])

    def _symbol_position_payload(self, symbol: Symbol) -> dict[str, Any]:
        return {
            "qname": symbol.qname,
            "kind": symbol.kind,
            "file": str(symbol.file),
            "line_start": symbol.line_start,
            "line_end": symbol.line_end,
        }

    def _human_symbol(self, symbol: Symbol) -> str:
        return f"{symbol.file}:{symbol.line_start}-{symbol.line_end}  {symbol.qname}"

    def _human_diag(self, diagnostic: Diagnostic) -> str:
        return f"{diagnostic.file}:{diagnostic.line}  {diagnostic.qname or '<module>'}  {diagnostic.code}  {diagnostic.message}"

    def _source_excerpt(self, path: Path, line: int, context: int = 5) -> dict[str, Any]:
        source = self._read_text(path)
        lines = source.splitlines()
        if not lines:
            return {"start_line": line, "end_line": line, "lines": []}
        start = max(1, line - context)
        end = min(len(lines), line + context)
        return {
            "start_line": start,
            "end_line": end,
            "lines": [
                {"line": index, "text": lines[index - 1]}
                for index in range(start, end + 1)
            ],
        }

    def _function_node_for_symbol(self, symbol: Symbol) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        source = self._read_text(symbol.file)
        return function_node_for_symbol(symbol, source)

    def _indent_for_line(self, source: str, line_no: int) -> str:
        lines = source.splitlines(keepends=True)
        if 1 <= line_no <= len(lines):
            line = lines[line_no - 1]
            return line[: len(line) - len(line.lstrip())]
        return ""

    def _reindent_block(self, block: str, indent: str) -> str:
        stripped = textwrap.dedent(block).rstrip("\n")
        if not stripped:
            return ""
        return "\n".join((indent + line if line.strip() else line) for line in stripped.splitlines())

    def _compose_sections(self, prefix: str, block: str, suffix: str = "") -> str:
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

    def _insert_before_line(self, source: str, line_no: int, block: str) -> str:
        lines = source.splitlines(keepends=True)
        insert_at = min(max(line_no - 1, 0), len(lines))
        prefix = "".join(lines[:insert_at])
        suffix = "".join(lines[insert_at:])
        return self._compose_sections(prefix, block, suffix)

    def _anchor_symbol(self, anchor: str, path: Path) -> Symbol | None:
        self._ensure_symbol_index()
        resolved = path.resolve()
        if anchor in self.symbol_index.symbols:
            symbol = self.symbol_index.resolve(anchor)
            if symbol.file.resolve() == resolved:
                return symbol
        if ":" in anchor:
            try:
                symbol = self.symbol_index.resolve(anchor)
            except KeyError:
                symbol = None
            else:
                if symbol.file.resolve() == resolved:
                    return symbol
        leaf_name = anchor.split(":")[-1].split(".")[-1]
        matches = self._find_symbol_details(name=leaf_name, path=resolved, qualname=anchor)
        if len(matches) != 1:
            return None
        match = matches[0]
        return Symbol(
            qname=qname_for_path(self.root, resolved, match.qualname),
            kind=match.kind,  # type: ignore[arg-type]
            file=resolved,
            line_start=match.start_line,
            line_end=match.end_line,
            signature=match.signature,
            docstring=match.docstring,
            decorators=list(match.decorators),
            parent=match.parent_qualname,
            children=[],
            bases=list(match.bases),
            returns=match.returns,
        )

    def _log_action(self, action: str, symbols: list[Symbol], delta: Delta) -> None:
        for symbol in symbols:
            self._logger.info("%s  %s", action, self._human_symbol(symbol))
        for diagnostic in delta.introduced:
            self._logger.info("diag  %s", self._human_diag(diagnostic))

    def _refresh_diagnostics_for_symbols(self, qnames: list[str]) -> Delta:
        self._ensure_symbol_index()
        affected_files = set(self._affected_files_for_qnames(qnames))
        before = self.diagnostic_store.snapshot()
        self.diagnostic_store.refresh(list(affected_files))
        return self.diagnostic_store.delta(before)

    def _transitive_caller_qnames(self, qname: str) -> list[str]:
        self._ensure_symbol_index()
        graph = build_call_graph(self.root)
        callers_by_callee: dict[str, set[str]] = {}
        for item in graph["call_graph"]:
            caller = item["caller"].get("qname") or item["caller"].get("qualname")
            for call in item["calls"]:
                resolved = call.get("resolved")
                if resolved is None:
                    continue
                callee = resolved.get("qname") or resolved.get("qualname")
                if callee is None:
                    continue
                if caller is not None:
                    callers_by_callee.setdefault(callee, set()).add(caller)

        visited: set[str] = set()
        stack = [qname]
        while stack:
            current = stack.pop()
            for caller in callers_by_callee.get(current, set()):
                if caller not in visited:
                    visited.add(caller)
                    stack.append(caller)
        return sorted(visited)

    def _affected_files_for_qnames(self, qnames: list[str]) -> list[Path]:
        self._ensure_symbol_index()
        files: set[Path] = set()
        for qname in qnames:
            if qname in self.symbol_index.symbols:
                files.add(self.symbol_index.resolve(qname).file)
        for caller_qname in qnames:
            for transitive in self._transitive_caller_qnames(caller_qname):
                if transitive in self.symbol_index.symbols:
                    files.add(self.symbol_index.resolve(transitive).file)
        return sorted(files)

    def _finalize_write(self, action: str, edited_qnames: list[str], touched_files: list[Path]) -> dict[str, Any]:
        self._ensure_symbol_index()
        for path in touched_files:
            self.symbol_index.reindex_file(path)
        before = self.diagnostic_store.snapshot()
        self.diagnostic_store.refresh(touched_files)
        delta = self.diagnostic_store.delta(before)
        edited_symbols = [self.symbol_index.resolve(qname) for qname in edited_qnames if qname in self.symbol_index.symbols]
        self._log_action(action, edited_symbols, delta)
        files = [str(path) for path in touched_files]
        return {
            "ok": not any(d.severity == "error" for d in delta.introduced),
            "accepted": not any(d.severity == "error" for d in delta.introduced),
            "edited": [symbol.qname for symbol in edited_symbols],
            "files": files,
            "destination_file": files[0] if files else None,
            "introduced": [
                {
                    "qname": diagnostic.qname,
                    "code": diagnostic.code,
                    "severity": diagnostic.severity,
                    "message": diagnostic.message,
                }
                for diagnostic in delta.introduced
            ],
            "fixed_count": len(delta.fixed),
            "delta": {
                "introduced": len(delta.introduced),
                "fixed": len(delta.fixed),
            },
        }

    def _collect_symbol_details(self, path: Path, source: str | None = None) -> list[SymbolDetail]:
        adapter = adapter_for(path)
        if adapter is None:
            return []
        source = self._read_text(path) if source is None else source
        tree = adapter.parse(source)
        if tree is None:
            return []
        return adapter.extract_symbols(tree, path)

    def _symbol_details_to_index_entry(self, detail: SymbolDetail) -> dict[str, Any]:
        entry = symbol_details_to_index_entry(detail)
        entry["qname"] = qname_for_path(self.root, Path(detail.file_path), detail.qualname)
        return entry

    def _collect_workspace_symbol_details(self) -> list[SymbolDetail]:
        details: list[SymbolDetail] = []
        for path in self._iter_workspace_source_files():
            details.extend(self._collect_symbol_details(path))
        return details

    def _find_symbols_in_index(self, name: str, path: Path | None = None, kind: str | None = None) -> list[Symbol]:
        """Search the in-memory symbol index without re-parsing any files.

        Matches on full qname, module-local qualname, or leaf name so that
        callers who pass a short name (e.g. 'load_csv') instead of the full
        qname ('load_data:load_csv') are resolved cheaply.
        """
        local = name.split(":")[-1] if ":" in name else name
        leaf = local.split(".")[-1] if "." in local else local
        results: list[Symbol] = []
        for sym in self.symbol_index.symbols.values():
            if kind is not None and sym.kind != kind:
                continue
            if path is not None and sym.file.resolve() != path.resolve():
                continue
            sym_local = sym.qname.split(":")[-1] if ":" in sym.qname else sym.qname
            sym_leaf = sym_local.split(".")[-1] if "." in sym_local else sym_local
            if sym.qname == name or sym_local == name or sym_local == local or sym_leaf == leaf:
                results.append(sym)
        return results

    def _did_you_mean_symbols(self, qname: str, limit: int = 3) -> list[str]:
        candidates = sorted(self.symbol_index.symbols)
        if not candidates:
            return []
        return difflib.get_close_matches(qname, candidates, n=limit, cutoff=0.0)

    def _qname_format_hint(self, qname: str, limit: int = 3) -> dict[str, Any] | None:
        if ":" in qname or "." not in qname:
            return None
        leaf = qname.split(".")[-1]
        search_matches = self.search(query=leaf).get("matches", [])
        candidates: list[str] = []
        for match in search_matches:
            if not isinstance(match, dict):
                continue
            candidate = match.get("qname")
            if isinstance(candidate, str) and candidate not in candidates:
                candidates.append(candidate)
            if len(candidates) >= limit:
                break
        if not candidates:
            candidates = self._did_you_mean_symbols(qname, limit=limit)
        return {
            "found": False,
            "reason": "invalid_qname_format",
            "qname": qname,
            "hint": "Use ':' between the module name and the qualified name, e.g. pkg.mod:Class.method.",
            "search_query": leaf,
            "did_you_mean": candidates,
            "error_detail": {
                "code": "invalid_qname_format",
                "message": "Qualified names must use ':' between module and symbol, not '.'.",
                "detail": {
                    "qname": qname,
                    "search_query": leaf,
                    "did_you_mean": candidates,
                },
            },
        }

    def _find_symbol_details(
        self,
        name: str,
        path: str | Path | None = None,
        qualname: str | None = None,
        kind: str | None = None,
    ) -> list[SymbolDetail]:
        self._ensure_symbol_index()
        matches: list[SymbolDetail] = []
        if path is not None:
            resolved = self._resolve_path(path)
            records = self._collect_symbol_details(resolved)
        else:
            records = self._collect_workspace_symbol_details()

        for record in records:
            if kind is not None and record.kind != kind:
                continue
            if record.name != name and record.qualname != name and (qualname is None or record.qualname != qualname):
                continue
            matches.append(record)
        return matches

    def _replace_source_span(self, source: str, start_line: int, end_line: int, new_source: str) -> str:
        return replace_source_span(source, start_line, end_line, new_source)

    def _extract_symbol_summary(self, detail: SymbolDetail, source: str) -> dict[str, Any]:
        node = detail.node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return extract_symbol_summary(detail, source)
        summary: dict[str, Any] = {
            "kind": detail.kind,
            "docstring": detail.docstring,
            "signature": detail.signature,
            "decorators": list(detail.decorators),
            "bases": list(detail.bases),
            "returns": detail.returns,
        }
        if detail.kind in {"function", "method"}:
            summary.update({"names_read": [], "names_written": [], "calls": [], "return_count": 0})
        elif detail.kind == "class":
            summary.update({"methods": [], "class_attributes": []})
        else:
            summary["source"] = detail.source
        return summary

    def _find_reference_hits_in_file(self, path: Path, name: str, include_source: bool = False) -> list[dict[str, Any]]:
        source = self._read_text(path)
        hits = find_reference_hits_in_file(path, name, source)
        if include_source:
            return hits
        return [
            {
                "file_path": hit["file_path"],
                "kind": hit["kind"],
                "name": hit["name"],
                "line": hit["line"],
                "end_line": hit["end_line"],
                "col": hit["col"],
                "end_col": hit["end_col"],
            }
            for hit in hits
        ]

    def _iter_workspace_source_files(self, langs: set[str] | None = None) -> list[Path]:
        allowed = {ext.lower() for ext in langs} if langs is not None else supported_extensions()
        ignored_files = {p.resolve() for p in self._ignored_paths if p.is_file()}
        ignored_dirs = {p.resolve() for p in self._ignored_paths if p.is_dir()}
        paths: list[Path] = []
        for dirpath_str, dirnames, filenames in os.walk(self.root):
            dirpath = Path(dirpath_str)
            # Prune in-place: os.walk will not descend into removed entries.
            dirnames[:] = [
                d for d in dirnames
                if d not in _PRUNE_DIRS
                and not d.startswith(".")
                and (dirpath / d).resolve() not in ignored_dirs
            ]
            for filename in filenames:
                if filename.endswith(".min.js") or filename.endswith(".bundle.js"):
                    continue
                p = dirpath / filename
                if p.suffix.lower() not in allowed:
                    continue
                if p.resolve() in ignored_files:
                    continue
                paths.append(p)
        return sorted(paths)

    def _iter_workspace_python_files(self) -> list[Path]:
        return self._iter_workspace_source_files({".py"})

    def _scope_source_files(self, files: list[str] | None = None, roots: list[str] | None = None, langs: set[str] | None = None) -> list[Path]:
        allowed = {ext.lower() for ext in langs} if langs is not None else None
        if files is not None:
            resolved: list[Path] = []
            for item in files:
                try:
                    path = self._resolve_path(item)
                except ValueError:
                    continue
                if path.exists() and (allowed is None or path.suffix.lower() in allowed):
                    resolved.append(path)
            return sorted(dict.fromkeys(resolved))

        if roots:
            selected: list[Path] = []
            for root_item in roots:
                try:
                    root_path = self._resolve_path(root_item)
                except ValueError:
                    continue
                if root_path.is_file() and (allowed is None or root_path.suffix.lower() in allowed):
                    selected.append(root_path)
                    continue
                if not root_path.exists() or not root_path.is_dir():
                    continue
                for path in sorted(root_path.rglob("*")):
                    if not path.is_file():
                        continue
                    if allowed is not None and path.suffix.lower() not in allowed:
                        continue
                    if any(part.startswith(".") and part not in {".", ".."} for part in path.relative_to(self.root).parts):
                        continue
                    if "__pycache__" in path.parts or "node_modules" in path.parts:
                        continue
                    if path.resolve() in {item.resolve() for item in self._ignored_paths}:
                        continue
                    if path.name.endswith(".min.js") or path.name.endswith(".bundle.js"):
                        continue
                    selected.append(path)
            return sorted(dict.fromkeys(selected))

        return self._iter_workspace_source_files(langs=allowed)

    def _scope_python_files(self, files: list[str] | None = None, roots: list[str] | None = None) -> list[Path]:
        return self._scope_source_files(files=files, roots=roots, langs={".py"})

    def _validate_single_python_unit(self, source: str) -> tuple[bool, str | None, int]:
        return validate_single_python_unit(source)

    def _check_missing_docstrings(self, source: str) -> list[str]:
        tree = self._parse_python(source)
        if tree is None:
            return []
        missing = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = node.body[0] if node.body else None
                has_docstring = (
                    first is not None
                    and isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                )
                if not has_docstring:
                    missing.append(node.name)
        return missing

    def _contains_notimplemented(self, source: str) -> bool:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return "NotImplementedError" in source
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
            if isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
                return True
        return "NotImplementedError" in source

    def _normalize_project_slug(self, name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        return slug or "project"

    def _project_files(self, project_name: str) -> list[Path]:
        slug = self._normalize_project_slug(project_name)
        return [
            self.root / "pyproject.toml",
            self.root / "requirements.txt",
            self.root / ".gitignore",
            self.root / "tests" / "conftest.py",
            self.root / "src" / slug / "__init__.py",
        ]

    def _rebuild_workspace_index_cache(self) -> list[dict[str, Any]]:
        previous_snapshot = self._workspace_index_snapshot()
        entries: list[dict[str, Any]] = []
        for path in self._iter_workspace_source_files():
            # use_cache=True: mtime check inside _index_source_file skips unchanged files
            file_entries = self._index_source_file(path, use_cache=True)
            entries.extend(file_entries)
        entries.sort(key=lambda item: (item["file_path"], item["start_line"], item["start_col"], item["qualname"]))
        self._workspace_index_cache = entries
        self._bump_workspace_index_snapshot(previous_snapshot, entries)
        return entries

    def _ensure_workspace_index_cache(self) -> list[dict[str, Any]]:
        if self._workspace_index_cache is None:
            return self._rebuild_workspace_index_cache()
        return self._workspace_index_cache

    def _update_workspace_index_for_file(self, path: Path) -> list[dict[str, Any]]:
        previous_snapshot = self._workspace_index_snapshot()
        path_key = str(path.resolve())
        file_entries = self._index_source_file(path, use_cache=False)
        # cache write is handled inside _index_source_file
        if self._workspace_index_cache is None:
            self._bump_workspace_index_snapshot(previous_snapshot, file_entries)
            return file_entries
        current = [entry for entry in self._workspace_index_cache if entry["file_path"] != path_key]
        current.extend(file_entries)
        current.sort(key=lambda item: (item["file_path"], item["start_line"], item["start_col"], item["qualname"]))
        self._workspace_index_cache = current
        self._bump_workspace_index_snapshot(previous_snapshot, current)
        return current

    def _workspace_index_snapshot(self) -> list[dict[str, Any]]:
        if self._workspace_index_cache is None:
            return self._workspace_index_snapshots.get(self._workspace_index_scan_id, [])
        return [dict(entry) for entry in self._workspace_index_cache]

    def _bump_workspace_index_snapshot(self, previous_snapshot: list[dict[str, Any]], current_entries: list[dict[str, Any]]) -> None:
        self._workspace_index_snapshots[self._workspace_index_scan_id] = [dict(entry) for entry in previous_snapshot]
        self._workspace_index_scan_id += 1
        self._workspace_index_snapshots[self._workspace_index_scan_id] = [dict(entry) for entry in current_entries]

    def _apply_python_snippet(self, source: str, snippet: str, anchor: str | None = None, position: str = "auto", source_path: Path | None = None) -> str:
        if anchor and source_path is not None:
            blocks = _split_snippet(snippet)
            anchor_symbol = self._anchor_symbol(anchor, source_path)
            for kind, block in blocks:
                tree = self._parse_python(source)
                if kind == "import":
                    source = _insert_imports(source, block, tree=tree)
                    continue
                if kind == "constant":
                    source = _insert_constants(source, block, tree=tree)
                    continue
                if anchor_symbol is not None:
                    indented = self._reindent_block(block, self._indent_for_line(source, anchor_symbol.line_start))
                    if position == "before":
                        source = self._insert_before_line(source, anchor_symbol.line_start, indented)
                    else:
                        source = insert_after_line(source, anchor_symbol.line_end, indented)
                else:
                    if kind == "definition":
                        source = _insert_definitions(source, block, tree=tree)
                    else:
                        source = _append_block(source, block)
            return source
        return apply_python_snippet(source, snippet, anchor=anchor, position=position)

    def _index_source_file(self, path: Path, use_cache: bool = True) -> list[dict[str, Any]]:
        path_key = str(path.resolve())
        if use_cache:
            cached = self._file_index_cache.get(path_key)
            if cached is not None:
                cached_mtime, cached_entries = cached
                try:
                    if path.stat().st_mtime == cached_mtime:
                        return cached_entries
                except OSError:
                    pass
        source = self._read_text(path)
        adapter = adapter_for(path)
        if adapter is None:
            return []
        tree = adapter.parse(source)
        if tree is None:
            return []
        entries = adapter.extract_symbols(tree, path)
        entries = [
            {
                "file_path": entry.file_path,
                "name": entry.name,
                "kind": entry.kind,
                "qualname": entry.qualname,
                "start_line": entry.start_line,
                "end_line": entry.end_line,
                "start_col": entry.start_col,
                "end_col": entry.end_col,
            }
            for entry in entries
        ]
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        self._file_index_cache[path_key] = (mtime, entries)
        return entries

    def _index_python_file(self, path: Path, use_cache: bool = True) -> list[dict[str, Any]]:
        return self._index_source_file(path, use_cache=use_cache)

    def _workspace_index_delta(self, since_scan_id: int, verbose: bool = False) -> dict[str, Any]:
        current_entries = self._ensure_workspace_index_cache()
        previous_entries = self._workspace_index_snapshots.get(since_scan_id)
        if previous_entries is None:
            return {
                "ok": False,
                "reason": "unknown_scan_id",
                "since_scan_id": since_scan_id,
                "scan_id": self._workspace_index_scan_id,
            }
        current_map = {
            qname_for_path(self.root, Path(entry["file_path"]), entry["qualname"]): entry
            for entry in current_entries
        }
        previous_map = {
            qname_for_path(self.root, Path(entry["file_path"]), entry["qualname"]): entry
            for entry in previous_entries
        }
        added = [self._public_index_entry(entry, verbose=verbose) for qname, entry in current_map.items() if qname not in previous_map]
        removed = [self._public_index_entry(entry, verbose=verbose) for qname, entry in previous_map.items() if qname not in current_map]
        changed = [
            {
                "qname": qname,
                "before": self._public_index_entry(previous_map[qname], verbose=verbose),
                "after": self._public_index_entry(current_map[qname], verbose=verbose),
            }
            for qname in sorted(current_map.keys() & previous_map.keys())
            if current_map[qname] != previous_map[qname]
        ]
        return {
            "ok": True,
            "since_scan_id": since_scan_id,
            "scan_id": self._workspace_index_scan_id,
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    def index_workspace(self, verbose: bool = False, since_scan_id: int | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        entries = self._ensure_workspace_index_cache()
        if since_scan_id is not None:
            return self._workspace_index_delta(since_scan_id, verbose=verbose)
        return {
            "ok": True,
            "scan_id": self._workspace_index_scan_id,
            "scope": "workspace",
            "symbols": [self._public_index_entry(entry, verbose=verbose) for entry in entries],
            "count": len(entries),
        }

    def insert_code(self, destination_file: str | Path, code: str, anchor: str | None = None, position: str = "auto", skip_diagnostics: bool = False) -> dict[str, Any]:
        with self._lock:
            path = self._resolve_path(destination_file)
            # Only ensure the target file is indexed — avoid a full workspace
            # scan which would parse every file including large external deps.
            self.symbol_index.ensure_file_scanned(path)
            invalid = self._reject_escaped_quotes(code, "code")
            if invalid is not None:
                invalid["destination_file"] = str(path)
                return invalid
            existing = self._read_text(path)
            warning = None
            missing_docstrings: list[str] = []
            top_level_definitions = 0
            adapter = adapter_for(path)
            if path.suffix.lower() == ".py":
                allowed, reason, top_level_definitions = self._validate_single_python_unit(code)
                if not allowed:
                    return {
                        "ok": False,
                        "accepted": False,
                        "reason": reason,
                        "retryable": True,
                        "top_level_definitions": top_level_definitions,
                        "hint": "Send exactly one function or class, max 50 lines. Use replace_symbol for larger edits.",
                        "destination_file": str(path),
                        "edited": [],
                        "introduced": [],
                        "fixed_count": 0,
                        "delta": {"introduced": 0, "fixed": 0},
                    }
                warning = reason if reason == "multiple_top_level_definitions" else None
                missing_docstrings = self._check_missing_docstrings(code)
                before_symbols = [symbol.qname for symbol in self.symbol_index.symbols_for_file(path)]
                updated = self._apply_python_snippet(existing, code, anchor=anchor, position=position, source_path=path)
            else:
                before_symbols = [symbol.qname for symbol in self.symbol_index.symbols_for_file(path)]
                if adapter is not None:
                    updated = adapter.insert_code(existing, code, anchor, position)
                else:
                    updated = self._read_text(path).rstrip("\n")
                    addition = code.strip("\n")
                    if addition:
                        if updated:
                            updated = updated + "\n\n" + addition + "\n"
                        else:
                            updated = addition + "\n"
                    else:
                        updated = existing
            self._write_text(path, updated)
            self.symbol_index.reindex_file(path)
            after_symbols = [symbol.qname for symbol in self.symbol_index.symbols_for_file(path)]
            edited_qnames = [qname for qname in after_symbols if qname not in before_symbols]
            if not edited_qnames:
                edited_qnames = [self.symbol_index._module_qname(path)]
            before = self.diagnostic_store.snapshot()
            if not skip_diagnostics:
                self.diagnostic_store.refresh([path])
            delta = self.diagnostic_store.delta(before)
            edited_symbols = [self.symbol_index.resolve(qname) for qname in edited_qnames if qname in self.symbol_index.symbols]
            self._log_action("insert", edited_symbols, delta)
            return {
                "ok": not any(d.severity == "error" for d in delta.introduced),
                "accepted": not any(d.severity == "error" for d in delta.introduced),
                "destination_file": str(path),
                "edited": [symbol.qname for symbol in edited_symbols],
                "warning": warning,
                "missing_docstrings": missing_docstrings,
                "top_level_definitions": top_level_definitions,
                "introduced": [
                    {
                        "qname": diagnostic.qname,
                        "code": diagnostic.code,
                        "severity": diagnostic.severity,
                        "message": diagnostic.message,
                    }
                    for diagnostic in delta.introduced
                ],
                "fixed_count": len(delta.fixed),
                "delta": {"introduced": len(delta.introduced), "fixed": len(delta.fixed)},
            }

    def index_file(self, path: str | Path, verbose: bool = False) -> list[dict[str, Any]]:
        resolved = self._resolve_path(path)
        if resolved.suffix.lower() not in supported_extensions():
            return []
        self.symbol_index.ensure_file_scanned(resolved)
        return [self._public_index_entry(entry, verbose=verbose) for entry in self._index_source_file(resolved)]

    def get_symbol_calls(self, name: str, path: str | Path | None = None, qualname: str | None = None, verbose: bool = False) -> dict[str, Any]:
        self._ensure_symbol_index()
        if name in self.symbol_index.symbols:
            symbol = self.symbol_index.resolve(name)
            if symbol.kind not in {"function", "method"}:
                return {"found": False, "function": None, "matches": [], "calls": []}
            source = self._extract_source_block(symbol.file, symbol.line_start, symbol.line_end)
            calls = self._extract_function_calls(source, str(symbol.file), symbol.line_start)
            payload = self._public_symbol(symbol, verbose=verbose)
            return {
                "found": True,
                "function": payload,
                "matches": [payload],
                "calls": [self._public_call_match(call, verbose=verbose) for call in calls],
            }

        short_name = name.split(":")[-1]
        short_qualname = qualname or name.split(":", 1)[-1]
        matches = self._find_symbol_details(name=short_name, path=path, qualname=short_qualname)
        matches = [match for match in matches if match.kind in {"function", "method"}]
        if not matches:
            return {"found": False, "function": None, "matches": [], "calls": []}
        if len(matches) > 1:
            return {
                "found": False,
                "reason": "ambiguous",
                "function": None,
                "matches": [self._public_function_match(self._symbol_details_to_index_entry(match), verbose=verbose) for match in matches],
                "calls": [],
            }

        function_info = matches[0]
        calls = self._extract_function_calls(function_info.source, function_info.file_path, function_info.start_line)
        return {
            "found": True,
            "function": self._public_function_match(self._symbol_details_to_index_entry(function_info), verbose=verbose),
            "matches": [self._public_function_match(self._symbol_details_to_index_entry(match), verbose=verbose) for match in matches],
            "calls": [self._public_call_match(call, verbose=verbose) for call in calls],
        }

    def _extract_source_block(self, path: Path, start_line: int, end_line: int) -> str:
        source = self._read_text(path)
        lines = source.splitlines(keepends=True)
        start_idx = max(start_line - 1, 0)
        end_idx = min(end_line, len(lines))
        return "".join(lines[start_idx:end_idx])

    def _extract_function_calls(self, source: str, file_path: str, base_line: int) -> list[dict[str, Any]]:
        path = Path(file_path)
        adapter = adapter_for(path)
        if adapter is not None:
            tree = adapter.parse(source)
            if tree is not None:
                return [
                    {
                        "file_path": call.caller_file,
                        "call": call.callee,
                        "line": call.line,
                        "col": call.col,
                        "end_line": call.end_line,
                        "end_col": call.end_col,
                        "source": call.source,
                    }
                    for call in adapter.extract_calls(tree, path, source)
                ]
        return extract_function_calls(source, file_path, base_line)

    def resolve(self, qname: str, verbose: bool = False) -> dict[str, Any]:
        self._ensure_symbol_index()
        symbol = self.symbol_index.resolve(qname)
        payload = self._public_symbol(symbol, verbose=verbose)
        return {"found": True, "symbol": payload}

    def locate(self, qname: str) -> str:
        self._ensure_symbol_index()
        return self.symbol_index.locate(qname)

    def children_of(self, qname: str) -> list[str]:
        self._ensure_symbol_index()
        return self.symbol_index.children_of(qname)

    def outline(self, path: str | Path, verbose: bool = False) -> dict[str, Any]:
        self._ensure_symbol_index()
        input_path = Path(path)
        module_qname = self.symbol_index._module_qname(self._resolve_path(input_path)) if input_path.exists() else str(path)
        module_qname = module_qname if module_qname.endswith(":") else f"{module_qname}:"
        module = self.symbol_index.resolve(module_qname)
        def build_tree(symbol: Symbol) -> dict[str, Any]:
            payload = self._public_symbol(symbol, verbose=verbose)
            payload["children"] = [build_tree(self.symbol_index.resolve(child)) for child in symbol.children]
            return payload
        return {
            "module": self._public_symbol(module, verbose=verbose),
            "symbols": [build_tree(self.symbol_index.resolve(child)) for child in module.children],
        }

    def get_symbol(
        self,
        qname: str,
        path: str | Path | None = None,
        kind: str | None = None,
        projection: str | None = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        self._ensure_symbol_index()
        if qname in self.symbol_index.symbols:
            symbol = self.symbol_index.resolve(qname)
            if kind is not None and symbol.kind != kind:
                return {"found": False, "symbol": None, "matches": [], "did_you_mean": self._did_you_mean_symbols(qname, limit=3)}
            if projection == "code":
                return {
                    "found": True,
                    "projection": projection,
                    "qname": symbol.qname,
                    "code": self._symbol_source_text(symbol),
                }
            if projection == "position":
                payload = self._symbol_position_payload(symbol)
                return {"found": True, "projection": projection, **payload}
            if projection == "callers":
                callers = self.find_callers(qname)
                return {
                    "found": True,
                    "projection": projection,
                    "qname": symbol.qname,
                    "callers": callers["callers"],
                    "count": callers["count"],
                }
            return {"found": True, "symbol": self._public_symbol(symbol, verbose=verbose), "matches": [self._public_symbol(symbol, verbose=verbose)]}
        matches = self._find_symbol_details(name=qname, path=path, qualname=qname, kind=kind)
        if not matches:
            qname_hint = self._qname_format_hint(qname)
            if qname_hint is not None:
                return qname_hint
            return {"found": False, "symbol": None, "matches": [], "did_you_mean": self._did_you_mean_symbols(qname, limit=3)}
        if len(matches) > 1:
            return {
                "found": False,
                "reason": "ambiguous",
                "symbol": None,
                "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=verbose) for match in matches],
        }
        match = matches[0]
        payload = self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=verbose)
        if projection == "code":
            return {
                "found": True,
                "projection": projection,
                "qname": qname_for_path(self.root, Path(match.file_path), match.qualname),
                "code": match.source,
            }
        if projection == "position":
            position = {
                "qname": qname_for_path(self.root, Path(match.file_path), match.qualname),
                "kind": match.kind,
                "file": match.file_path,
                "line_start": match.start_line,
                "line_end": match.end_line,
            }
            return {
                "found": True,
                "projection": projection,
                **position,
            }
        if projection == "callers":
            callers = self.find_callers(qname_for_path(self.root, Path(match.file_path), match.qualname))
            return {
                "found": True,
                "projection": projection,
                "qname": qname_for_path(self.root, Path(match.file_path), match.qualname),
                "callers": callers["callers"],
                "count": callers["count"],
            }
        return {"found": True, "symbol": payload, "matches": [payload]}

    def get_symbol_summary(self, qname: str, path: str | Path | None = None, kind: str | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        if qname in self.symbol_index.symbols:
            symbol = self.symbol_index.resolve(qname)
            summary = {
                "kind": symbol.kind,
                "signature": symbol.signature,
                "docstring": symbol.docstring,
                "decorators": list(symbol.decorators),
                "parent": symbol.parent,
                "children": list(symbol.children),
            }
            if symbol.kind in {"function", "method"}:
                node = self._function_node_for_symbol(symbol)
                if node is not None:
                    scanner = _FunctionSummaryVisitor()
                    for statement in node.body:
                        scanner.visit(statement)
                    summary.update(
                        {
                            "names_read": sorted(scanner.reads),
                            "names_written": sorted(scanner.writes),
                            "calls": sorted(scanner.calls),
                            "return_count": scanner.return_count,
                        }
                    )
            return {"found": True, "symbol": self._public_symbol(symbol, verbose=False), "summary": summary}
        matches = self._find_symbol_details(name=qname, path=path, qualname=qname, kind=kind)
        if not matches:
            qname_hint = self._qname_format_hint(qname)
            if qname_hint is not None:
                return qname_hint
            return {"found": False, "symbol": None, "summary": None, "did_you_mean": self._did_you_mean_symbols(qname, limit=3)}
        if len(matches) > 1:
            return {
                "found": False,
                "reason": "ambiguous",
                "symbol": None,
                "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=False) for match in matches],
            }
        match = matches[0]
        return {
            "found": True,
            "symbol": self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=False),
            "summary": self._extract_symbol_summary(match, match.source),
        }

    def find_callers(self, qname: str, limit: int = 20) -> dict[str, Any]:
        self._ensure_symbol_index()
        graph = build_call_graph(self.root)
        matches: list[dict[str, Any]] = []
        for item in graph["call_graph"]:
            filtered = []
            for call in item["calls"]:
                resolved = call.get("resolved")
                if resolved is not None and (resolved.get("qname") == qname or resolved.get("qualname") == qname):
                    filtered.append(
                        {
                            "callee": resolved.get("qname") or call.get("callee"),
                            "caller": item["caller"].get("qname") or item["caller"].get("qualname"),
                        }
                    )
                elif call.get("callee") == qname:
                    filtered.append(
                        {
                            "callee": call.get("callee"),
                            "caller": item["caller"].get("qname") or item["caller"].get("qualname"),
                        }
                    )
            if filtered:
                matches.append(
                    {
                        "caller": item["caller"].get("qname") or item["caller"].get("qualname"),
                        "calls": filtered[:limit],
                    }
                )
        return {
            "target": qname,
            "count": len(matches),
            "returned": min(len(matches), limit),
            "truncated": len(matches) > limit,
            "callers": matches[:limit],
        }

    def find_call_path(self, from_qname: str, to_qname: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        from_summary = self.get_symbol_summary(from_qname)
        to_summary = self.get_symbol_summary(to_qname)
        if not from_summary.get("found") or not to_summary.get("found"):
            return {
                "found": False,
                "from_qname": from_qname,
                "to_qname": to_qname,
                "path": [],
                "hops": None,
            }
        if from_qname == to_qname:
            signature = from_summary.get("summary", {}).get("signature")
            return {
                "found": True,
                "from_qname": from_qname,
                "to_qname": to_qname,
                "hops": 0,
                "path": [from_qname],
                "signatures": [signature],
            }

        graph = build_call_graph(self.root)
        adjacency: dict[str, set[str]] = {}
        for item in graph.get("call_graph", []):
            caller_qname = item.get("caller", {}).get("qname")
            if not isinstance(caller_qname, str):
                continue
            adjacency.setdefault(caller_qname, set())
            for call in item.get("calls", []):
                resolved = call.get("resolved")
                if isinstance(resolved, dict) and isinstance(resolved.get("qname"), str):
                    adjacency[caller_qname].add(resolved["qname"])
        queue: deque[tuple[str, list[str]]] = deque([(from_qname, [from_qname])])
        visited: set[str] = {from_qname}
        while queue:
            current, path = queue.popleft()
            for neighbor in sorted(adjacency.get(current, set())):
                if neighbor in visited:
                    continue
                next_path = [*path, neighbor]
                if neighbor == to_qname:
                    path_signatures: list[str | None] = []
                    for qname in next_path:
                        summary = self.get_symbol_summary(qname)
                        signature = summary.get("summary", {}).get("signature") if summary.get("found") else None
                        path_signatures.append(signature)
                    return {
                        "found": True,
                        "from_qname": from_qname,
                        "to_qname": to_qname,
                        "hops": len(next_path) - 1,
                        "path": next_path,
                        "signatures": path_signatures,
                    }
                visited.add(neighbor)
                queue.append((neighbor, next_path))
        return {"found": False, "from_qname": from_qname, "to_qname": to_qname, "path": [], "hops": None}

    def neighbors(self, qname: str, hops: int = 1) -> dict[str, Any]:
        self._ensure_symbol_index()
        target = self.get_symbol_summary(qname)
        if not target.get("found"):
            return {"found": False, "qname": qname, "hops": hops}
        graph = build_call_graph(self.root)
        outgoing: dict[str, set[str]] = {}
        incoming: dict[str, set[str]] = {}
        for item in graph.get("call_graph", []):
            caller_qname = item.get("caller", {}).get("qname")
            if not isinstance(caller_qname, str):
                continue
            outgoing.setdefault(caller_qname, set())
            for call in item.get("calls", []):
                resolved = call.get("resolved")
                if isinstance(resolved, dict) and isinstance(resolved.get("qname"), str):
                    callee_qname = resolved["qname"]
                    outgoing[caller_qname].add(callee_qname)
                    incoming.setdefault(callee_qname, set()).add(caller_qname)

        def _signature_for(symbol_qname: str) -> str | None:
            summary = self.get_symbol_summary(symbol_qname)
            if not summary.get("found"):
                return None
            return summary.get("summary", {}).get("signature")

        def _pair(symbol_qname: str) -> list[str | None]:
            return [symbol_qname, _signature_for(symbol_qname)]

        layers: list[dict[str, Any]] = []
        frontier: set[str] = {qname}
        seen: set[str] = {qname}
        for depth in range(1, max(hops, 1) + 1):
            next_frontier: set[str] = set()
            for current in frontier:
                next_frontier.update(outgoing.get(current, set()))
                next_frontier.update(incoming.get(current, set()))
            next_frontier -= seen
            seen.update(next_frontier)
            layers.append({"distance": depth, "neighbors": [_pair(symbol_qname) for symbol_qname in sorted(next_frontier)]})
            frontier = next_frontier

        return {
            "found": True,
            "qname": qname,
            "signature": target.get("summary", {}).get("signature"),
            "callers": [_pair(symbol_qname) for symbol_qname in sorted(incoming.get(qname, set()))],
            "callees": [_pair(symbol_qname) for symbol_qname in sorted(outgoing.get(qname, set()))],
            "layers": layers,
        }

    def get_module_consumers(self, path: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        target_path = self._resolve_path(path)
        if not target_path.exists():
            return {"found": False, "path": path, "reason": "not_found"}
        target_module = module_name_for_path(self.root, target_path)
        consumers: list[dict[str, Any]] = []
        for candidate in self._iter_workspace_source_files():
            source = self._read_text(candidate)
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module_expr = "." * node.level + node.module if node.module else "." * node.level
                    resolved_module = _resolve_local_import(self.root, candidate, module_expr) if module_expr else None
                    if target_path.name == "__init__.py" and module_expr == target_module:
                        for alias in node.names:
                            names.add(alias.asname or alias.name.split(".")[-1])
                        continue
                    if resolved_module is not None and resolved_module.resolve() == target_path.resolve():
                        for alias in node.names:
                            names.add(alias.asname or alias.name.split(".")[-1])
                        continue
                    for alias in node.names:
                        alias_module = f"{module_expr}.{alias.name}" if module_expr else alias.name
                        resolved_alias = _resolve_local_import(self.root, candidate, alias_module)
                        if resolved_alias is not None and resolved_alias.resolve() == target_path.resolve():
                            names.add(alias.asname or alias.name.split(".")[-1])
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        resolved = _resolve_local_import(self.root, candidate, alias.name)
                        if resolved is not None and resolved.resolve() == target_path.resolve():
                            names.add(alias.asname or alias.name.split(".")[-1])
            if names:
                consumers.append({"importer": str(candidate), "names_used": sorted(names)})
        consumers.sort(key=lambda item: item["importer"])
        return {
            "found": True,
            "path": str(target_path),
            "module": target_module,
            "count": len(consumers),
            "consumers": consumers,
        }

    def find_references(self, name: str, limit: int = 20, include_source: bool = False) -> dict[str, Any]:
        self._ensure_symbol_index()
        references: list[dict[str, Any]] = []
        for path in self._iter_workspace_source_files():
            for hit in self._find_reference_hits_in_file(path, name, include_source=include_source):
                symbol = self.symbol_index.symbol_at(path, hit["line"])
                payload = {
                    "file": str(path),
                    "line": hit["line"],
                    "kind": hit["kind"],
                }
                if include_source:
                    payload.update(
                        {
                            "qname": None if symbol is None else symbol.qname,
                            "name": name,
                            "source": hit["source"],
                        }
                    )
                references.append(payload)
        return {
            "name": name,
            "count": len(references),
            "returned": min(len(references), limit),
            "truncated": len(references) > limit,
            "references": references[:limit],
        }

    def search(
        self,
        query: str,
        kind: str | None = None,
        async_only: bool | None = None,
        base: str | None = None,
        missing_return_annotation: bool | None = None,
        raises: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_symbol_index()
        query_lower = query.lower()
        results: list[dict[str, Any]] = []
        ast_qnames: set[str] | None = None
        if any(value is not None for value in (async_only, base, missing_return_annotation, raises)):
            ast_result = self.search_ast(
                kind=kind,
                async_only=async_only,
                base=base,
                missing_return_annotation=missing_return_annotation,
                raises=raises,
            )
            ast_qnames = {item["qname"] for item in ast_result.get("matches", []) if isinstance(item.get("qname"), str)}
        for symbol in self.symbol_index.symbols.values():
            if kind is not None and symbol.kind != kind:
                continue
            if ast_qnames is not None and symbol.qname not in ast_qnames:
                continue
            haystack = " ".join(
                value
                for value in [
                    symbol.qname,
                    symbol.signature or "",
                    symbol.docstring or "",
                    " ".join(symbol.decorators),
                    " ".join(symbol.bases),
                    symbol.returns or "",
                ]
                if value
            ).lower()
            if query_lower in haystack:
                results.append(self._public_symbol(symbol, verbose=False))
        return {"query": query, "kind": kind, "count": len(results), "matches": results}

    def _estimate_token_count(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    def _resolve_symbol_or_path(self, qname_or_path: str) -> tuple[Symbol | None, Path | None]:
        if qname_or_path in self.symbol_index.symbols:
            return self.symbol_index.resolve(qname_or_path), None
        try:
            path = self._resolve_path(qname_or_path)
        except ValueError:
            return None, None
        if path.exists():
            return None, path
        return None, None

    def _public_detail(self, detail: SymbolDetail) -> dict[str, Any]:
        payload = self._public_detail_entry(self._symbol_details_to_index_entry(detail), verbose=False)
        payload["docstring"] = detail.docstring
        payload["signature"] = detail.signature
        payload["bases"] = list(detail.bases)
        payload["returns"] = detail.returns
        return payload

    def _public_detail_compact(self, detail: SymbolDetail) -> dict[str, Any]:
        """Lightweight version of _public_detail for API docs - excludes source and verbose metadata."""
        return {
            "name": detail.name,
            "kind": detail.kind,
            "qualname": detail.qualname,
            "signature": detail.signature,
            "docstring": detail.docstring,
            "returns": detail.returns,
            "decorators": list(detail.decorators),
        }

    def _is_public_name(self, name: str) -> bool:
        return not name.startswith("_")

    def _function_node(self, detail: SymbolDetail) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        if isinstance(detail.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return detail.node
        return None

    def _function_arg_annotations_missing(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        missing: list[str] = []
        for arg in [
            *getattr(node.args, "posonlyargs", []),
            *node.args.args,
            node.args.vararg,
            *node.args.kwonlyargs,
            node.args.kwarg,
        ]:
            if arg is None:
                continue
            if arg.arg in {"self", "cls"}:
                continue
            if getattr(arg, "annotation", None) is None:
                missing.append(arg.arg)
        return missing

    def _parse_signature_node(self, signature: str | None) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        if not signature:
            return None
        signature = signature.strip()
        if not (signature.startswith("def ") or signature.startswith("async def ")):
            return None
        try:
            tree = ast.parse(signature + ":\n    pass\n")
        except SyntaxError:
            return None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def _function_signature_metrics(self, signature: str | None) -> dict[str, Any]:
        node = self._parse_signature_node(signature)
        if node is None:
            return {
                "signature": signature,
                "required_positionals": 0,
                "total_positionals": 0,
                "has_varargs": False,
                "has_varkw": False,
                "kwonly_required": [],
                "params": [],
            }
        params = []
        required_positionals = 0
        total_positionals = 0
        for arg in [*getattr(node.args, "posonlyargs", []), *node.args.args]:
            params.append(arg.arg)
            total_positionals += 1
            if getattr(arg, "annotation", None) is None:
                pass
        defaults = len(node.args.defaults)
        required_positionals = max(0, total_positionals - defaults)
        kwonly_required = [arg.arg for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None]
        return {
            "signature": signature,
            "required_positionals": required_positionals,
            "total_positionals": total_positionals,
            "has_varargs": node.args.vararg is not None,
            "has_varkw": node.args.kwarg is not None,
            "kwonly_required": kwonly_required,
            "params": params,
        }

    def _call_signature_mismatch(self, call: dict[str, Any], metrics: dict[str, Any]) -> bool:
        source = call.get("source", "")
        try:
            expr = ast.parse(source, mode="eval").body
        except SyntaxError:
            return False
        if not isinstance(expr, ast.Call):
            return False
        pos_args = len(expr.args)
        kw_names = {kw.arg for kw in expr.keywords if kw.arg is not None}
        if pos_args < metrics.get("required_positionals", 0):
            return True
        if not metrics.get("has_varargs") and pos_args > metrics.get("total_positionals", 0):
            return True
        if not metrics.get("has_varkw"):
            unexpected_kw = kw_names - set(metrics.get("params", []))
            if unexpected_kw:
                return True
        return False

    def _symbol_public_methods(self, class_qname: str) -> list[dict[str, Any]]:
        details = self._collect_symbol_details(Path(self.symbol_index.resolve(class_qname).file))
        methods = [
            self._public_detail(detail)
            for detail in details
            if detail.parent_qualname == class_qname and detail.kind in {"function", "method"} and self._is_public_name(detail.name)
        ]
        methods.sort(key=lambda item: (item["signature"] or "", item["name"]))
        return methods

    def _resolve_local_class_base(self, base: str) -> str | None:
        if base in self.symbol_index.symbols and self.symbol_index.resolve(base).kind == "class":
            return base
        bare = base.split(".")[-1]
        matches = [
            symbol.qname
            for symbol in self.symbol_index.symbols.values()
            if symbol.kind == "class" and (symbol.qname.endswith(f":{bare}") or symbol.qname.endswith(f".{bare}") or symbol.qname.endswith(f":{base}"))
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _raise_name(self, node: ast.AST | None) -> str:
        if node is None:
            return "reraise"
        if isinstance(node, ast.Call):
            return self._raise_name(node.func)
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return ast.unparse(node)
        try:
            return ast.unparse(node)
        except Exception:
            return "unknown"

    def _collect_raises(self, node: ast.AST) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                results.append(
                    {
                        "name": self._raise_name(child.exc),
                        "source": ast.get_source_segment(self._read_text(Path(getattr(node, "file_path", ""))), child) if False else None,
                        "line": getattr(child, "lineno", 1),
                        "col": getattr(child, "col_offset", 0) + 1,
                    }
                )
        return results

    def _collect_direct_exceptions(self, detail: SymbolDetail) -> list[dict[str, Any]]:
        node = detail.node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return []
        exceptions: list[dict[str, Any]] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                exceptions.append(
                    {
                        "name": self._raise_name(child.exc),
                        "line": getattr(child, "lineno", 1),
                        "col": getattr(child, "col_offset", 0) + 1,
                        "source": ast.unparse(child),
                    }
                )
        return exceptions

    def _collect_symbol_calls(self, detail: SymbolDetail) -> set[str]:
        node = detail.node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return set()
        collector = FunctionCallCollector()
        calls: set[str] = set()
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    collector.collect(child)
        else:
            collector.collect(node)
        for call in collector.calls:
            calls.add(ast.unparse(call.func))
        return calls

    def _call_graph_for_scope(self, files: list[str] | None = None, roots: list[str] | None = None, include_sources: bool = False, include_unresolved: bool = False) -> dict[str, Any]:
        scope_files = self._scope_source_files(files=files, roots=roots) if (files is not None or roots is not None) else None
        return build_call_graph(self.root, files=scope_files, include_sources=include_sources, include_unresolved=include_unresolved)

    def _symbol_call_graph_item(self, qname: str, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any] | None:
        graph = self._call_graph_for_scope(files=files, roots=roots, include_sources=True)
        for item in graph.get("call_graph", []):
            caller = item.get("caller", {})
            if caller.get("qname") == qname:
                return item
        return None

    def size_hint(self, qname_or_path: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        symbol, path = self._resolve_symbol_or_path(qname_or_path)
        if symbol is not None:
            source = self._symbol_source_text(symbol)
            return {
                "target": qname_or_path,
                "kind": "symbol",
                "qname": symbol.qname,
                "file": str(symbol.file),
                "line_start": symbol.line_start,
                "line_end": symbol.line_end,
                "line_count": max(1, symbol.line_end - symbol.line_start + 1),
                "char_count": len(source),
                "token_estimate": self._estimate_token_count(source),
            }
        if path is not None:
            source = self._read_text(path)
            return {
                "target": qname_or_path,
                "kind": "file",
                "path": str(path),
                "line_count": len(source.splitlines()),
                "char_count": len(source),
                "token_estimate": self._estimate_token_count(source),
            }
        return {"target": qname_or_path, "found": False}

    def get_module_api(self, path: str) -> dict[str, Any]:
        resolved = self._resolve_path(path)
        source = self._read_text(resolved)
        details = self._collect_symbol_details(resolved, source)
        adapter = adapter_for(resolved)
        tree = adapter.parse(source) if adapter is not None else None
        module_docstring = None
        if tree is not None:
            if resolved.suffix.lower() == ".py":
                parsed = tree.get("tree") if isinstance(tree, dict) else None
                module_docstring = ast.get_docstring(parsed) if isinstance(parsed, ast.Module) else None
            else:
                module_docstring = str(tree.get("docstring") or "").strip() or None
        public_symbols: list[dict[str, Any]] = []
        for detail in details:
            if detail.parent_qualname is not None or not self._is_public_name(detail.name):
                continue
            payload = self._public_detail_compact(detail)
            if detail.kind == "class":
                payload["public_methods"] = [
                    self._public_detail_compact(method)
                    for method in details
                    if method.parent_qualname == detail.qualname
                    and method.kind in {"function", "method"}
                    and self._is_public_name(method.name)
                ]
            public_symbols.append(payload)
        public_symbols.sort(key=lambda item: (item["kind"], item["signature"] or "", item["name"]))
        return {
            "path": str(resolved),
            "module": f"{module_name_for_path(self.root, resolved)}:",
            "docstring": module_docstring,
            "public_symbols": public_symbols,
            "count": len(public_symbols),
        }

    def search_ast(
        self,
        files: list[str] | None = None,
        roots: list[str] | None = None,
        kind: str | None = None,
        async_only: bool | None = None,
        base: str | None = None,
        missing_return_annotation: bool | None = None,
        raises: str | None = None,
        calls: str | None = None,
        decorator: str | None = None,
        parameter_count_gt: int | None = None,
    ) -> dict[str, Any]:
        target_files = self._iter_workspace_source_files()
        if files is not None or roots is not None:
            target_files = self._scope_source_files(files=files, roots=roots)
        matches: list[dict[str, Any]] = []
        for path in target_files:
            details = self._collect_symbol_details(path)
            for detail in details:
                if kind is not None and detail.kind != kind:
                    continue
                node = detail.node
                if async_only is True and not isinstance(node, ast.AsyncFunctionDef):
                    continue
                if async_only is False and isinstance(node, ast.AsyncFunctionDef):
                    continue
                if base is not None:
                    if detail.kind != "class":
                        continue
                    if base not in detail.bases and not any(base in candidate for candidate in detail.bases):
                        continue
                if decorator is not None:
                    if decorator not in detail.decorators and not any(decorator in candidate for candidate in detail.decorators):
                        continue
                if missing_return_annotation is True and detail.kind in {"function", "method"} and detail.returns is not None:
                    continue
                if missing_return_annotation is False and detail.kind in {"function", "method"} and detail.returns is None:
                    continue
                if raises is not None and detail.kind in {"function", "method", "class"}:
                    direct = self._collect_direct_exceptions(detail)
                    if not any(raises == item["name"] or raises in item["name"] for item in direct):
                        continue
                if calls is not None and detail.kind in {"function", "method", "class"}:
                    call_names = sorted(self._collect_symbol_calls(detail))
                    if not any(calls == candidate or calls in candidate for candidate in call_names):
                        continue
                if parameter_count_gt is not None and detail.kind in {"function", "method"}:
                    args = getattr(detail.node, "args", None)
                    parameter_count = len(getattr(args, "args", [])) if args is not None else 0
                    if parameter_count <= parameter_count_gt:
                        continue
                matches.append(
                    {
                        "qname": qname_for_path(self.root, path, detail.qualname),
                        "file_path": detail.file_path,
                        "name": detail.name,
                        "kind": detail.kind,
                        "qualname": detail.qualname,
                        "signature": detail.signature,
                        "docstring": detail.docstring,
                        "bases": list(detail.bases),
                        "returns": detail.returns,
                        "line_start": detail.start_line,
                        "line_end": detail.end_line,
                    }
                )
        return {"count": len(matches), "matches": matches}

    def get_symbols(self, qnames: list[str], projection: str | None = "code", verbose: bool = False) -> dict[str, Any]:
        self._ensure_symbol_index()
        return {
            "count": len(qnames),
            "symbols": [
                self.get_symbol(qname, projection=projection, verbose=verbose)
                for qname in qnames
            ],
        }

    def get_symbol_with_deps(self, qname: str, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        symbol_payload = self.get_symbol(qname, projection="code")
        if not symbol_payload.get("found"):
            return symbol_payload
        graph = self._call_graph_for_scope(files=files, roots=roots)
        caller_item = None
        for item in graph.get("call_graph", []):
            if item.get("caller", {}).get("qname") == qname:
                caller_item = item
                break
        deps: list[dict[str, Any]] = []
        seen: set[str] = set()
        if caller_item is not None:
            for call in caller_item.get("calls", []):
                resolved = call.get("resolved")
                if not isinstance(resolved, dict):
                    continue
                dep_qname = resolved.get("qname")
                if not isinstance(dep_qname, str) or dep_qname in seen:
                    continue
                seen.add(dep_qname)
                dep_symbol = self.get_symbol(dep_qname, projection="code")
                deps.append(
                    {
                        "qname": dep_qname,
                        "call": call.get("callee"),
                        "definition": dep_symbol,
                    }
                )
        return {"symbol": symbol_payload, "dependencies": deps, "count": len(deps)}

    def impact_of_change(self, qname: str, new_signature: str | None = None, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        target = self.get_symbol_summary(qname)
        if not target.get("found"):
            return target
        graph = self._call_graph_for_scope(files=files, roots=roots)
        effective_signature = new_signature or target["summary"].get("signature")
        metrics = self._function_signature_metrics(effective_signature if isinstance(effective_signature, str) else None)
        callers: list[dict[str, Any]] = []
        direct_callers: set[str] = set()
        for item in graph.get("call_graph", []):
            caller_qname = item.get("caller", {}).get("qname")
            if not isinstance(caller_qname, str):
                continue
            matched_calls = []
            for call in item.get("calls", []):
                resolved = call.get("resolved")
                if not isinstance(resolved, dict):
                    continue
                if resolved.get("qname") != qname:
                    continue
                direct_callers.add(caller_qname)
                matched_calls.append(
                    {
                        "call": call.get("source"),
                        "line": call.get("line"),
                        "signature_mismatch": self._call_signature_mismatch(call, metrics),
                    }
                )
            if matched_calls:
                callers.append({"caller": caller_qname, "calls": matched_calls})
        return {
            "qname": qname,
            "current_signature": target["summary"].get("signature"),
            "effective_signature": effective_signature,
            "callers": callers,
            "caller_count": len(direct_callers),
        }

    def class_hierarchy(self, qname: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        if qname not in self.symbol_index.symbols:
            return {"found": False, "qname": qname}
        symbol = self.symbol_index.resolve(qname)
        if symbol.kind != "class":
            return {"found": False, "qname": qname, "reason": "not_a_class"}

        chain: list[dict[str, Any]] = []
        visited: set[str] = set()

        def _walk(class_qname: str) -> None:
            if class_qname in visited:
                return
            visited.add(class_qname)
            current = self.symbol_index.resolve(class_qname)
            methods = [
                self._public_symbol(child, verbose=False)
                for child in [self.symbol_index.resolve(child_qname) for child_qname in current.children if child_qname in self.symbol_index.symbols]
                if child.kind in {"function", "method"} and self._is_public_name(child.qname.split(":")[-1].split(".")[-1])
            ]
            methods.sort(key=lambda item: (item["signature"] or "", item["qname"]))
            bases = list(current.bases)
            resolved_bases = []
            for base in bases:
                resolved_base = self._resolve_local_class_base(base)
                resolved_bases.append({"base": base, "resolved_qname": resolved_base})
                if resolved_base is not None:
                    _walk(resolved_base)
            chain.append(
                {
                    "qname": current.qname,
                    "file": str(current.file),
                    "bases": bases,
                    "resolved_bases": resolved_bases,
                    "docstring": current.docstring,
                    "signature": current.signature,
                    "public_methods": methods,
                }
            )

        _walk(qname)
        return {"found": True, "qname": qname, "chain": list(reversed(chain))}

    def _dead_symbol_confidence(self, detail: SymbolDetail) -> str:
        if detail.name.startswith("visit_") or detail.name.startswith("__"):
            return "low"
        if any(decorator == "property" or decorator.endswith(".property") for decorator in detail.decorators):
            return "low"
        return "high"

    def dead_symbols(
        self,
        files: list[str] | None = None,
        roots: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        target_files = self._iter_workspace_source_files()
        if files is not None or roots is not None:
            target_files = self._scope_source_files(files=files, roots=roots)
        graph = self._call_graph_for_scope(files=files, roots=roots)
        fan_in: dict[str, int] = {}
        for item in graph.get("call_graph", []):
            for call in item.get("calls", []):
                resolved = call.get("resolved")
                if isinstance(resolved, dict) and isinstance(resolved.get("qname"), str):
                    fan_in[resolved["qname"]] = fan_in.get(resolved["qname"], 0) + 1
        dead: list[dict[str, Any]] = []
        for path in target_files:
            for detail in self._collect_symbol_details(path):
                if detail.kind not in {"function", "method", "class"}:
                    continue
                qname = qname_for_path(self.root, path, detail.qualname)
                if fan_in.get(qname, 0) > 0:
                    continue
                dead.append(
                    {
                        "qname": qname,
                        "file_path": detail.file_path,
                        "kind": detail.kind,
                        "name": detail.name,
                        "qualname": detail.qualname,
                        "line_start": detail.start_line,
                        "line_end": detail.end_line,
                        "confidence": self._dead_symbol_confidence(detail),
                    }
                )
        dead.sort(key=lambda item: (item["file_path"], item["line_start"], item["qname"]))
        total = len(dead)
        safe_offset = max(offset, 0)
        page = dead[safe_offset:] if limit is None else dead[safe_offset : safe_offset + max(limit, 0)]
        return {
            "count": total,
            "returned": len(page),
            "offset": safe_offset,
            "limit": limit,
            "truncated": safe_offset + len(page) < total,
            "matches": page,
        }

    def patch_symbol(
        self,
        qname: str,
        new_source: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        old_lines: list[str] | None = None,
        new_lines: list[str] | None = None,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        self._ensure_symbol_index()

        # Fast path 1: exact qname.
        if qname in self.symbol_index.symbols:
            symbol = self.symbol_index.resolve(qname)
            file_path = symbol.file
            source = self._read_text(file_path)
            symbol_start = symbol.line_start
            symbol_end = symbol.line_end
        else:
            # Fast path 2: name search within the in-memory index — no file I/O.
            resolved_path = self._resolve_path(path) if path is not None else None
            index_hits = self._find_symbols_in_index(qname, path=resolved_path)
            if index_hits:
                if len(index_hits) > 1:
                    return {
                        "accepted": False,
                        "reason": "ambiguous",
                        "matches": [self._public_symbol(s, verbose=False) for s in index_hits],
                    }
                symbol = index_hits[0]
                file_path = symbol.file
                source = self._read_text(file_path)
                symbol_start = symbol.line_start
                symbol_end = symbol.line_end
            else:
                # Slow fallback: re-parse workspace files.
                matches = self._find_symbol_details(name=qname, path=path, qualname=qname)
                if not matches:
                    return {"accepted": False, "reason": "not_found", "matches": []}
                if len(matches) > 1:
                    return {
                        "accepted": False,
                        "reason": "ambiguous",
                        "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=False) for match in matches],
                    }
                symbol = None
                detail = matches[0]
                file_path = Path(detail.file_path)
                source = self._read_text(file_path)
                symbol_start = detail.start_line
                symbol_end = detail.end_line

        lines = source.splitlines(keepends=True)
        body_start = symbol_start - 1
        body_end = symbol_end
        segment = "".join(lines[body_start:body_end])

        replacement: str | None = None
        matched_lines: list[str] | None = None
        matched_range: dict[str, int] | None = None
        if old_lines is not None and new_lines is not None:
            segment_lines = segment.splitlines()
            old_block_lines = [line.rstrip("\n") for line in old_lines]
            new_block_lines = [line.rstrip("\n") for line in new_lines]
            match_index = -1
            for index in range(0, len(segment_lines) - len(old_block_lines) + 1):
                if segment_lines[index:index + len(old_block_lines)] == old_block_lines:
                    match_index = index
                    break
            if match_index < 0:
                return {"accepted": False, "reason": "old_lines_not_found"}
            replacement_lines = segment_lines[:match_index] + new_block_lines + segment_lines[match_index + len(old_block_lines):]
            replacement = "\n".join(replacement_lines)
            if segment.endswith("\n"):
                replacement += "\n"
            matched_lines = segment_lines[match_index:match_index + len(old_block_lines)]
            matched_range = {
                "start_line": body_start + match_index + 1,
                "end_line": body_start + match_index + len(old_block_lines),
            }
        elif start_line is not None and end_line is not None and new_source is not None:
            relative_start = max(0, start_line - 1)
            relative_end = min(len(lines), end_line)
            segment_lines = lines[body_start:body_end]
            segment_lines[relative_start:relative_end] = [new_source if new_source.endswith("\n") else new_source + "\n"]
            replacement = "".join(segment_lines)
        elif new_source is not None:
            replacement = new_source
        else:
            return {"accepted": False, "reason": "missing_patch_arguments"}

        updated = "".join(lines[:body_start]) + replacement + "".join(lines[body_end:])
        self._write_text(file_path, updated)
        result = self._finalize_write("edit", [qname], [file_path])
        if matched_lines is not None and matched_range is not None:
            result["matched_lines"] = matched_lines
            result["matched_range"] = matched_range
        return result

    def exception_surface(self, qname: str, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        target = self.get_symbol_summary(qname)
        if not target.get("found"):
            return target
        symbol = self.symbol_index.resolve(qname)
        node = self._function_node_for_symbol(symbol)
        if node is None:
            return {"found": False, "reason": "not_a_function", "qname": qname}
        direct = self._collect_direct_exceptions(SymbolDetail(
            file_path=str(symbol.file),
            kind=symbol.kind,
            name=symbol.qname.split(":")[-1].split(".")[-1],
            qualname=symbol.qname.split(":", 1)[-1],
            parent_qualname=symbol.parent,
            start_line=symbol.line_start,
            end_line=symbol.line_end,
            start_col=0,
            end_col=0,
            signature=symbol.signature,
            docstring=symbol.docstring,
            decorators=list(symbol.decorators),
            bases=list(symbol.bases),
            returns=symbol.returns,
            source=self._symbol_source_text(symbol),
            node=node,
        ))
        graph = self._call_graph_for_scope(files=files, roots=roots)
        callees: dict[str, dict[str, Any]] = {}
        for item in graph.get("call_graph", []):
            if item.get("caller", {}).get("qname") != qname:
                continue
            for call in item.get("calls", []):
                resolved = call.get("resolved")
                if not isinstance(resolved, dict):
                    continue
                callee_qname = resolved.get("qname")
                if not isinstance(callee_qname, str) or callee_qname in callees:
                    continue
                dep = self.get_symbol(callee_qname, projection="code")
                callee_exceptions = []
                if dep.get("found"):
                    dep_symbol = self.symbol_index.resolve(callee_qname)
                    dep_node = self._function_node_for_symbol(dep_symbol)
                    if dep_node is not None:
                        dep_detail = SymbolDetail(
                            file_path=str(dep_symbol.file),
                            kind=dep_symbol.kind,
                            name=dep_symbol.qname.split(":")[-1].split(".")[-1],
                            qualname=dep_symbol.qname.split(":", 1)[-1],
                            parent_qualname=dep_symbol.parent,
                            start_line=dep_symbol.line_start,
                            end_line=dep_symbol.line_end,
                            start_col=0,
                            end_col=0,
                            signature=dep_symbol.signature,
                            docstring=dep_symbol.docstring,
                            decorators=list(dep_symbol.decorators),
                            bases=list(dep_symbol.bases),
                            returns=dep_symbol.returns,
                            source=self._symbol_source_text(dep_symbol),
                            node=dep_node,
                        )
                        callee_exceptions = self._collect_direct_exceptions(dep_detail)
                callees[callee_qname] = {"definition": dep, "exceptions": callee_exceptions}
        transitive = [item for item in direct]
        for item in callees.values():
            transitive.extend(item.get("exceptions", []))
        return {"qname": qname, "direct": direct, "transitive": transitive, "callees": callees}

    def missing_annotations(self, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        target_files = self._iter_workspace_source_files()
        if files is not None or roots is not None:
            target_files = self._scope_source_files(files=files, roots=roots)
        missing: list[dict[str, Any]] = []
        for path in target_files:
            for detail in self._collect_symbol_details(path):
                if detail.kind not in {"function", "method"}:
                    continue
                node = self._function_node(detail)
                if node is None:
                    continue
                missing_params = self._function_arg_annotations_missing(node)
                missing_return = detail.returns is None
                if not missing_params and not missing_return:
                    continue
                missing.append(
                    {
                        "qname": qname_for_path(self.root, path, detail.qualname),
                        "file_path": detail.file_path,
                        "kind": detail.kind,
                        "name": detail.name,
                        "missing_parameters": missing_params,
                        "missing_return": missing_return,
                    }
                )
        return {"count": len(missing), "matches": missing}

    def lint(self, qname: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        diagnostics = self.diagnostic_store.filter(qname)
        source_path = self.symbol_index.resolve(qname).file if qname in self.symbol_index.symbols else None
        return {
            "qname": qname,
            "count": len(diagnostics),
            "diagnostics": [
                {
                    "qname": diagnostic.qname,
                    "code": diagnostic.code,
                    "severity": diagnostic.severity,
                    "message": diagnostic.message,
                    "line": diagnostic.line,
                    "snippet": None if source_path is None else self._source_excerpt(source_path, diagnostic.line),
                }
                for diagnostic in diagnostics
            ],
        }

    def lint_file(self, path: str | Path) -> dict[str, Any]:
        self._ensure_symbol_index()
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"found": False, "path": str(resolved), "reason": "not_found", "symbols": [], "count": 0}
        self.diagnostic_store.refresh([resolved])
        symbols = self.symbol_index.symbols_for_file(resolved)
        symbol_results: list[dict[str, Any]] = []
        total = 0
        for symbol in symbols:
            diagnostics = self.diagnostic_store.filter(symbol.qname)
            total += len(diagnostics)
            symbol_results.append(
                {
                    "qname": symbol.qname,
                    "kind": symbol.kind,
                    "count": len(diagnostics),
                    "diagnostics": [
                        {
                            "qname": diagnostic.qname,
                            "code": diagnostic.code,
                            "severity": diagnostic.severity,
                            "message": diagnostic.message,
                        }
                        for diagnostic in diagnostics
                    ],
                }
            )
        return {
            "found": True,
            "path": str(resolved),
            "count": total,
            "symbols": symbol_results,
        }

    def planned_stub_symbols(self, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        target_files = self._iter_workspace_source_files()
        if files is not None or roots is not None:
            target_files = self._scope_source_files(files=files, roots=roots)
        matches: list[dict[str, Any]] = []
        for path in target_files:
            for symbol in self.symbol_index.symbols_for_file(path):
                if symbol.kind == "module":
                    continue
                source = self._symbol_source_text(symbol)
                if not self._contains_notimplemented(source):
                    continue
                matches.append(
                    {
                        "qname": symbol.qname,
                        "file_path": str(symbol.file),
                        "kind": symbol.kind,
                        "name": symbol.qname.split(":")[-1].split(".")[-1],
                        "line_start": symbol.line_start,
                        "line_end": symbol.line_end,
                    }
                )
        return {"count": len(matches), "matches": matches}

    def init_project(self, name: str, description: str, python_version: str, deps: list[str] | None = None) -> dict[str, Any]:
        """Register project metadata and return the file manifest without writing to disk.

        Calling code should use the returned manifest to create boilerplate files via
        scaffold_module / insert_code so that creation is tracked by the plan.
        The only file written here is src/{slug}/__init__.py when it does not yet exist,
        because the package directory must exist before plan_module_structure can reference it.
        """
        deps = [str(dep).strip() for dep in (deps or []) if str(dep).strip()]
        slug = self._normalize_project_slug(name)
        package_dir = self.root / "src" / slug

        manifest = {
            "pyproject.toml": (
                "[build-system]\n"
                "requires = [\"setuptools>=61\"]\n"
                "build-backend = \"setuptools.build_meta\"\n\n"
                "[project]\n"
                f"name = {json.dumps(slug)}\n"
                f"description = {json.dumps(description)}\n"
                'version = "0.1.0"\n'
                f"requires-python = {json.dumps(f'>={python_version}')}\n"
                f"dependencies = {json.dumps(deps)}\n\n"
                "[tool.setuptools]\n"
                'package-dir = {"" = "src"}\n\n'
                "[tool.setuptools.packages.find]\n"
                'where = ["src"]\n'
            ),
            "requirements.txt": (
                "# Managed by mini_coding_mcp\n"
                + "\n".join(deps)
                + ("\n" if deps else "")
            ),
            ".gitignore": (
                "__pycache__/\n"
                ".pytest_cache/\n"
                ".ruff_cache/\n"
                ".mypy_cache/\n"
                ".venv/\n"
                "venv/\n"
                "env/\n"
                "build/\n"
                "dist/\n"
                "*.pyc\n"
                ".coverage\n"
                "htmlcov/\n"
            ),
            "tests/conftest.py": (
                '"""Test configuration for the project."""\n\n'
                "from __future__ import annotations\n\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                "ROOT = Path(__file__).resolve().parents[1]\n"
                "SRC = ROOT / \"src\"\n"
                "if str(SRC) not in sys.path:\n"
                "    sys.path.insert(0, str(SRC))\n"
            ),
            f"src/{slug}/__init__.py": f'"""{description}."""\n',
        }

        # Only bootstrap the package __init__.py when it does not yet exist.
        # All other boilerplate should be created by the caller via scaffold_module.
        init_path = package_dir / "__init__.py"
        bootstrapped: list[str] = []
        if not init_path.exists():
            self._write_text(init_path, manifest[f"src/{slug}/__init__.py"])
            self.symbol_index.ensure_scanned()
            self.symbol_index.reindex_file(init_path)
            self._file_index_cache.pop(str(init_path.resolve()), None)
            self._workspace_index_cache = None
            bootstrapped.append(str(init_path))

        return {
            "ok": True,
            "project_name": name,
            "slug": slug,
            "description": description,
            "python_version": python_version,
            "dependencies": deps,
            "bootstrapped_files": bootstrapped,
            "manifest": manifest,
            "hint": (
                "Project registered. Use the manifest contents to create each file "
                "via insert_code or scaffold_module so creation is tracked. "
                "Call plan_module_structure next."
            ),
        }

    def type_check(self, qname: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        scope_qnames = list(dict.fromkeys([qname, *self._transitive_caller_qnames(qname)]))
        files = [self.symbol_index.resolve(name).file for name in scope_qnames if name in self.symbol_index.symbols]
        before = self.diagnostic_store.snapshot()
        self.diagnostic_store.refresh(files)
        delta = self.diagnostic_store.delta(before)
        return {
            "qname": qname,
            "scope": scope_qnames,
            "introduced": [
                {
                    "qname": diagnostic.qname,
                    "code": diagnostic.code,
                    "severity": diagnostic.severity,
                    "message": diagnostic.message,
                }
                for diagnostic in delta.introduced
            ],
            "fixed_count": len(delta.fixed),
        }

    def replace_symbol(self, qname: str, new_source: str, path: str | Path | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        invalid = self._reject_escaped_quotes(new_source, "new_source")
        if invalid is not None:
            return invalid

        # Fast path 1: exact qname hit in the symbol index.
        if qname in self.symbol_index.symbols:
            symbol = self.symbol_index.resolve(qname)
            existing = self._read_text(symbol.file)
            updated = self._replace_source_span(existing, symbol.line_start, symbol.line_end, new_source)
            self._write_text(symbol.file, updated)
            return self._finalize_write("edit", [qname], [symbol.file])

        # Fast path 2: name search within the in-memory index — no file I/O.
        resolved_path = self._resolve_path(path) if path is not None else None
        index_hits = self._find_symbols_in_index(qname, path=resolved_path)
        if index_hits:
            if len(index_hits) > 1:
                return {
                    "accepted": False,
                    "reason": "ambiguous",
                    "matches": [self._public_symbol(s, verbose=False) for s in index_hits],
                }
            symbol = index_hits[0]
            existing = self._read_text(symbol.file)
            updated = self._replace_source_span(existing, symbol.line_start, symbol.line_end, new_source)
            self._write_text(symbol.file, updated)
            return self._finalize_write("edit", [symbol.qname], [symbol.file])

        # Slow fallback: re-parse workspace files (only reached when symbol is absent from index).
        matches = self._find_symbol_details(name=qname, path=path, qualname=qname)
        if not matches:
            qname_hint = self._qname_format_hint(qname)
            if qname_hint is not None:
                return qname_hint
            return {"accepted": False, "reason": "not_found", "matches": []}
        if len(matches) > 1:
            return {
                "accepted": False,
                "reason": "ambiguous",
                "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=False) for match in matches],
            }
        match = matches[0]
        resolved = Path(match.file_path)
        existing = self._read_text(resolved)
        updated = self._replace_source_span(existing, match.start_line, match.end_line, new_source)
        self._write_text(resolved, updated)
        target_qname = qname_for_path(self.root, resolved, match.qualname)
        return self._finalize_write("edit", [target_qname], [resolved])

    def add_method(self, class_qname: str, code: str, path: str | Path | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        invalid = self._reject_escaped_quotes(code, "code")
        if invalid is not None:
            return invalid
        if class_qname not in self.symbol_index.symbols:
            matches = self._find_symbol_details(name=class_qname, path=path, qualname=class_qname, kind="class")
            if not matches:
                return {"accepted": False, "reason": "not_found", "matches": []}
            if len(matches) > 1:
                return {
                    "accepted": False,
                    "reason": "ambiguous",
                    "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=False) for match in matches],
                }
            match = matches[0]
            resolved = Path(match.file_path)
            source = self._read_text(resolved)
            lines = source.splitlines(keepends=True)
            class_indent = lines[match.start_line - 1][: len(lines[match.start_line - 1]) - len(lines[match.start_line - 1].lstrip())]
            body_indent = class_indent + "    "
            method_block = textwrap.dedent(code).rstrip("\n")
            method_block = "\n".join((body_indent + line if line.strip() else line) for line in method_block.splitlines())
            updated = insert_after_line(source, match.end_line, method_block + "\n")
            self._write_text(resolved, updated)
            self.symbol_index.reindex_file(resolved)
            after_symbols = [item.qname for item in self.symbol_index.symbols_for_file(resolved)]
            new_def = self._parse_python(code)
            method_name = None
            if new_def is not None:
                for node in new_def.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        method_name = node.name
                        break
            edited_qnames = []
            if method_name is not None:
                edited_qnames.append(qname_for_path(self.root, resolved, f"{match.qualname}.{method_name}"))
            edited_qnames.extend(qname for qname in after_symbols if qname not in edited_qnames)
            if not edited_qnames:
                edited_qnames = [qname_for_path(self.root, resolved, match.qualname)]
            before = self.diagnostic_store.snapshot()
            self.diagnostic_store.refresh([resolved])
            delta = self.diagnostic_store.delta(before)
            edited_symbols = [self.symbol_index.resolve(qname) for qname in edited_qnames if qname in self.symbol_index.symbols]
            self._log_action("edit", edited_symbols, delta)
            files = [str(resolved)]
            return {
                "ok": not any(d.severity == "error" for d in delta.introduced),
                "accepted": not any(d.severity == "error" for d in delta.introduced),
                "edited": [item.qname for item in edited_symbols],
                "files": files,
                "destination_file": files[0],
                "introduced": [
                    {
                        "qname": diagnostic.qname,
                        "code": diagnostic.code,
                        "severity": diagnostic.severity,
                        "message": diagnostic.message,
                    }
                    for diagnostic in delta.introduced
                ],
                "fixed_count": len(delta.fixed),
                "delta": {"introduced": len(delta.introduced), "fixed": len(delta.fixed)},
            }

        symbol = self.symbol_index.resolve(class_qname)
        if symbol.kind != "class":
            return {"accepted": False, "reason": "not_a_class", "matches": [self._public_symbol(symbol, verbose=False)]}
        source = self._read_text(symbol.file)
        before_symbols = [item.qname for item in self.symbol_index.symbols_for_file(symbol.file)]
        lines = source.splitlines(keepends=True)
        class_line = lines[symbol.line_start - 1]
        class_indent = class_line[: len(class_line) - len(class_line.lstrip())]
        body_indent = class_indent + "    "
        method_block = textwrap.dedent(code).rstrip("\n")
        method_block = "\n".join((body_indent + line if line.strip() else line) for line in method_block.splitlines())
        updated = insert_after_line(source, symbol.line_end, method_block + "\n")
        self._write_text(symbol.file, updated)
        self.symbol_index.reindex_file(symbol.file)
        after_symbols = [item.qname for item in self.symbol_index.symbols_for_file(symbol.file)]
        edited_qnames = [qname for qname in after_symbols if qname not in before_symbols] or [class_qname]
        before = self.diagnostic_store.snapshot()
        self.diagnostic_store.refresh([symbol.file])
        delta = self.diagnostic_store.delta(before)
        edited_symbols = [self.symbol_index.resolve(qname) for qname in edited_qnames if qname in self.symbol_index.symbols]
        self._log_action("edit", edited_symbols, delta)
        files = [str(symbol.file)]
        return {
            "ok": not any(d.severity == "error" for d in delta.introduced),
            "accepted": not any(d.severity == "error" for d in delta.introduced),
            "edited": [item.qname for item in edited_symbols],
            "files": files,
            "destination_file": files[0],
            "introduced": [
                {
                    "qname": diagnostic.qname,
                    "code": diagnostic.code,
                    "severity": diagnostic.severity,
                    "message": diagnostic.message,
                }
                for diagnostic in delta.introduced
            ],
            "fixed_count": len(delta.fixed),
            "delta": {"introduced": len(delta.introduced), "fixed": len(delta.fixed)},
        }

    def add_import(self, module: str, names: list[str] | None = None, path: str | Path | None = None) -> dict[str, Any]:
        self._ensure_symbol_index()
        if path is None:
            if self._focus_dir is not None:
                return {
                    "accepted": False,
                    "ok": False,
                    "reason": "path_required",
                    "hint": (
                        f"A workspace focus is active ('{self._focus_dir.relative_to(self.root)}'). "
                        "Pass path='<filename>.py' to specify the target file explicitly."
                    ),
                }
            target_path = self._resolve_path(self.root)
        else:
            target_path = self._resolve_path(path)
        if target_path.is_dir():
            target_path = target_path / "main.py"
        existing = self._read_text(target_path)
        names = names or []

        plan_payload = _load_plan_payload(self.root)
        plan_entry = plan_payload.get(target_path.name) if isinstance(plan_payload, dict) else None
        if isinstance(plan_entry, dict) and "depends_on" in plan_entry:
            local_candidate_name: str | None = None
            module_path = Path(module.replace(".", "/"))
            search_candidates = [
                self.root / module_path.with_suffix(".py"),
                self.root / module_path / "__init__.py",
                target_path.parent / f"{module.split('.')[-1]}.py",
                target_path.parent / module.split(".")[-1] / "__init__.py",
            ]
            for candidate in search_candidates:
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                try:
                    resolved.relative_to(self.root)
                except ValueError:
                    continue
                if resolved.exists() and resolved.is_file() or resolved.name in plan_payload:
                    local_candidate_name = resolved.name
                    break

            if local_candidate_name is not None:
                allowed = {
                    str(dep)
                    for dep in plan_entry.get("depends_on", [])
                    if isinstance(dep, str)
                }
                if local_candidate_name not in allowed:
                    return {
                        "accepted": False,
                        "ok": False,
                        "reason": "dependency_not_allowed",
                        "hint": (
                            f"{target_path.name} may only import from: "
                            f"{', '.join(sorted(allowed)) if allowed else '[nothing]'}"
                        ),
                        "file": str(target_path),
                        "import": module,
                        "resolved_dependency": local_candidate_name,
                        "allowed_dependencies": sorted(allowed),
                    }

        tree = self._parse_python(existing)
        if tree is not None:
            for node in tree.body:
                if isinstance(node, ast.Import) and not names:
                    for alias in node.names:
                        if alias.name == module:
                            return {
                                "accepted": True,
                                "destination_file": str(target_path),
                                "deduplicated": True,
                                "edited": [],
                                "introduced": [],
                                "fixed_count": 0,
                                "delta": {"introduced": 0, "fixed": 0},
                            }
                if isinstance(node, ast.ImportFrom) and node.module == module and names:
                    existing_names = {alias.asname or alias.name for alias in node.names}
                    requested_names = set(names)
                    if requested_names.issubset(existing_names):
                        return {
                            "accepted": True,
                            "destination_file": str(target_path),
                            "deduplicated": True,
                            "edited": [],
                            "introduced": [],
                            "fixed_count": 0,
                            "delta": {"introduced": 0, "fixed": 0},
                    }
                    names = sorted(requested_names - existing_names)
        import_line = f"import {module}" if not names else f"from {module} import {', '.join(sorted(set(names)))}"
        if module == "typing" and names:
            import_line += "  # noqa: F401"
        updated = self._apply_python_snippet(existing, import_line + "\n", position="auto")
        self._write_text(target_path, updated)
        return self._finalize_write("edit", [self.symbol_index._module_qname(target_path)], [target_path])

    def rename_symbol(self, old: str, new: str) -> dict[str, Any]:
        self._ensure_symbol_index()
        if old not in self.symbol_index.symbols:
            matches = self._find_symbol_details(name=old, qualname=old)
            if not matches:
                return {"accepted": False, "reason": "not_found", "matches": []}
            if len(matches) > 1:
                return {
                    "accepted": False,
                    "reason": "ambiguous",
                    "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(match), verbose=False) for match in matches],
                }
            target = matches[0]
            changed_files: list[str] = []
            for path in self._iter_workspace_source_files():
                source = self._read_text(path)
                tree = self._parse_python(source)
                if tree is None:
                    continue
                hits = self._find_reference_hits_in_file(path, old)
                if not hits:
                    continue
                lines = source.splitlines(keepends=True)
                by_line: dict[int, list[dict[str, Any]]] = {}
                for hit in hits:
                    by_line.setdefault(hit["line"], []).append(hit)
                updated_source = source
                offset = 0
                for line_no in sorted(by_line):
                    line_hits = sorted(by_line[line_no], key=lambda item: item["col"], reverse=True)
                    for hit in line_hits:
                        start = hit["col"] - 1
                        end = hit["end_col"] - 1
                        absolute_start = sum(len(line) for line in lines[: line_no - 1]) + start + offset
                        absolute_end = sum(len(line) for line in lines[: line_no - 1]) + end + offset
                        updated_source = updated_source[:absolute_start] + new + updated_source[absolute_end:]
                        offset += len(new) - (absolute_end - absolute_start)
                if updated_source != source:
                    self._write_text(path, updated_source)
                    changed_files.append(str(path))
                    self._update_workspace_index_for_file(path)
            return {
                "accepted": True,
                "old": old,
                "new": new,
                "matches": [self._public_detail_entry(self._symbol_details_to_index_entry(target), verbose=False)],
                "changed_files": changed_files,
            }

        target = self.symbol_index.resolve(old)
        terminal_old = old.split(":")[-1].split(".")[-1]
        if ":" in old:
            module_part, symbol_part = old.split(":", 1)
            if "." in symbol_part:
                parent_path = symbol_part.rsplit(".", 1)[0]
                new_qname = f"{module_part}:{parent_path}.{new}"
            else:
                new_qname = f"{module_part}:{new}"
        else:
            new_qname = new

        changed_files: list[Path] = []
        affected_files = self._affected_files_for_qnames([old])
        if target.file not in affected_files:
            affected_files.append(target.file)
        for path in affected_files:
            source = self._read_text(path)
            updated_source = re.sub(rf"\b{re.escape(terminal_old)}\b", new, source)
            if updated_source != source:
                self._write_text(path, updated_source)
                changed_files.append(path)
        result = self._finalize_write("edit", [new_qname], changed_files or [target.file])
        result.update({"old": old, "new": new, "changed_files": [str(path) for path in changed_files]})
        return result

    def _apply_unified_diff(self, patch_text: str) -> dict[str, Any]:
        """Pure-Python unified diff applier; no external binary required."""
        import re as _re

        lines = patch_text.splitlines(keepends=True)
        i = 0
        changed_files: list[Path] = []

        while i < len(lines):
            while i < len(lines) and not lines[i].startswith("--- "):
                i += 1
            if i >= len(lines):
                break
            i += 1  # skip --- line
            if i >= len(lines) or not lines[i].startswith("+++ "):
                break
            new_path_str = lines[i][4:].split("\t")[0].rstrip()
            i += 1

            # Resolve path — strip any a/ b/ prefix then try absolute then relative
            for strip in (0, 1, 2):
                candidate = Path(*Path(new_path_str.lstrip("/")).parts[strip:]) if strip else Path(new_path_str)
                if not candidate.is_absolute():
                    candidate = self.root / candidate
                try:
                    path = self._resolve_path(candidate)
                    if path.exists():
                        break
                except ValueError:
                    continue
            else:
                return {"accepted": False, "returncode": -1, "stdout": "", "stderr": f"cannot resolve path: {new_path_str}"}

            file_lines = self._read_text(path).splitlines(keepends=True)
            offset = 0

            while i < len(lines) and lines[i].startswith("@@"):
                m = _re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
                if not m:
                    i += 1
                    continue
                old_start = int(m.group(1))
                i += 1

                hunk_old: list[str] = []
                hunk_new: list[str] = []
                while i < len(lines):
                    raw = lines[i]
                    if not raw or raw.startswith("@@") or raw.startswith("--- "):
                        break
                    if raw.startswith("\\ "):
                        i += 1
                        continue
                    ch, content = raw[0], raw[1:]
                    if not content.endswith("\n"):
                        content += "\n"
                    if ch == "-":
                        hunk_old.append(content)
                    elif ch == "+":
                        hunk_new.append(content)
                    elif ch == " ":
                        hunk_old.append(content)
                        hunk_new.append(content)
                    else:
                        break
                    i += 1

                start_idx = old_start - 1 + offset
                file_lines[start_idx : start_idx + len(hunk_old)] = hunk_new
                offset += len(hunk_new) - len(hunk_old)

            self._write_text(path, "".join(file_lines))
            changed_files.append(path)

        if not changed_files:
            return {"accepted": False, "returncode": -1, "stdout": "", "stderr": "no hunks applied"}

        self._workspace_index_cache = None
        self._file_index_cache.clear()
        self.symbol_index.scan()
        self.diagnostic_store.refresh(list(self.symbol_index.by_file.keys()))
        return {"accepted": True, "stdout": f"applied to {len(changed_files)} file(s)", "stderr": ""}

    def _native_patch_commands(self, patch_path: Path) -> list[list[str]]:
        """Return ordered list of native commands to try, based on OS and available binaries."""
        cmds: list[list[str]] = []
        # patch(1) is reliable on Unix; skip on Windows where it is rarely available
        if sys.platform != "win32" and shutil.which("patch"):
            cmds.append(["patch", "-p1", "-i", str(patch_path)])
            cmds.append(["patch", "-p0", "-i", str(patch_path)])
        # git apply works on all platforms when git is present
        if shutil.which("git"):
            cmds.append(["git", "apply", "--whitespace=fix", str(patch_path)])
        return cmds

    def _apply_begin_patch_format(self, patch_text: str) -> dict[str, Any]:
        """Handle the *** Begin Patch / *** Add File / *** Delete File custom format."""
        changed_files: list[Path] = []
        errors: list[str] = []

        lines = patch_text.splitlines()
        i = 0
        # skip to first directive
        while i < len(lines) and not lines[i].startswith("*** "):
            i += 1

        current_op: str | None = None
        current_path: Path | None = None
        content_lines: list[str] = []

        def _flush() -> None:
            if current_op in ("Add File", "Update File") and current_path is not None:
                if current_op == "Update File" and current_path.exists():
                    original_lines = current_path.read_text(encoding="utf-8").splitlines()
                    if len(content_lines) > 20 and len(original_lines) > 0 and len(content_lines) >= len(original_lines) * 0.8:
                        errors.append(
                            f"apply_patch rejected for {current_path.name}: *** Update File supplies "
                            f"{len(content_lines)} lines ({len(original_lines)} original) — this is a "
                            "near-full file rewrite, not a patch. Use replace_symbol to replace a specific "
                            "function or class, or patch_symbol for a sub-range edit."
                        )
                        return
                current_path.parent.mkdir(parents=True, exist_ok=True)
                current_path.write_text("\n".join(content_lines) + ("\n" if content_lines else ""), encoding="utf-8")
                changed_files.append(current_path)

        while i < len(lines):
            line = lines[i]
            if line.startswith("*** End Patch"):
                _flush()
                break
            if line.startswith("*** Delete File:"):
                _flush()
                rel = line[len("*** Delete File:"):].strip()
                target = (self.root / rel).resolve()
                if target.exists():
                    target.unlink()
                    changed_files.append(target)
                current_op = None
                current_path = None
                content_lines = []
            elif line.startswith("*** Add File:") or line.startswith("*** Update File:"):
                _flush()
                prefix = "*** Add File:" if line.startswith("*** Add File:") else "*** Update File:"
                current_op = prefix[4:-1]
                rel = line[len(prefix):].strip()
                current_path = (self.root / rel).resolve()
                content_lines = []
            elif current_op in ("Add File", "Update File"):
                if line.startswith("+ "):
                    content_lines.append(line[2:])
                elif line.startswith("+"):
                    content_lines.append(line[1:])
                elif line.startswith("  ") or line.startswith(" "):
                    content_lines.append(line[1:])
            i += 1

        if not changed_files and errors:
            return {"accepted": False, "reason": "begin_patch_errors", "errors": errors}
        if not changed_files:
            return {"accepted": False, "reason": "begin_patch_no_changes", "patch_format": "begin_patch"}

        self._workspace_index_cache = None
        self._file_index_cache.clear()
        for path in changed_files:
            if path.exists():
                self.symbol_index.reindex_file(path)
            else:
                resolved = path.resolve()
                for qname in list(self.symbol_index.by_file.get(resolved, [])):
                    self.symbol_index.symbols.pop(qname, None)
                self.symbol_index.by_file.pop(resolved, None)
        self.diagnostic_store.refresh([p for p in changed_files if p.exists()])
        return {
            "accepted": True,
            "returncode": 0,
            "stdout": f"Applied begin-patch format: {len(changed_files)} file(s) changed.",
            "stderr": "",
            "changed_files": [str(p) for p in changed_files],
        }

    def apply_patch(self, patch_text: str) -> dict[str, Any]:
        patch_path = self.root / ".mini_coding_mcp.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        patch_succeeded = False
        stdout = stderr = ""
        try:
            for cmd in self._native_patch_commands(patch_path):
                try:
                    result = subprocess.run(
                        cmd, cwd=self.root, capture_output=True, text=True, check=False, timeout=30,
                    )
                    if result.returncode == 0:
                        patch_succeeded = True
                        stdout, stderr = result.stdout, result.stderr
                        break
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    continue
        finally:
            try:
                patch_path.unlink()
            except FileNotFoundError:
                pass

        if patch_succeeded:
            self._workspace_index_cache = None
            self._file_index_cache.clear()
            self.symbol_index.scan()
            self.diagnostic_store.refresh(self.symbol_index.by_file.keys())
            return {"accepted": True, "stdout": stdout, "stderr": stderr}

        if patch_text.lstrip().startswith("*** Begin Patch"):
            return self._apply_begin_patch_format(patch_text)

        return self._apply_unified_diff(patch_text)

    def rename_file(self, old_path: str | Path, new_path: str | Path) -> dict[str, Any]:
        old = self._resolve_path(old_path)
        new = self._resolve_path(new_path)
        if not old.exists():
            return {"accepted": False, "reason": "not_found", "old": str(old)}
        if new.exists():
            return {"accepted": False, "reason": "destination_exists", "new": str(new)}

        def _to_module(p: Path) -> str:
            return ".".join(p.relative_to(self.root).with_suffix("").parts)

        old_module = _to_module(old)
        new_module = _to_module(new)
        old_stem = old.stem
        new_stem = new.stem

        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)

        # Use the symbol index to find only files that import old_stem — avoid
        # reading every file in the workspace when only a few reference it.
        segment_re = re.compile(r"(?<![a-zA-Z0-9_])" + re.escape(old_stem) + r"(?![a-zA-Z0-9_])")
        candidate_files: list[Path] = []
        new_resolved = new.resolve()
        for sym in self.symbol_index.symbols.values():
            if sym.kind != "module":
                continue
            if sym.file.resolve() == new_resolved:
                continue
            if any(old_stem in imp.split(".")[-1] or old_stem == imp for imp in sym.imports):
                candidate_files.append(sym.file)
        # Fall back to full scan only when the index has no import info (cold start).
        if not candidate_files and not self.symbol_index._scanned:
            candidate_files = self._iter_workspace_source_files()

        changed_files: list[str] = []
        for path in candidate_files:
            if path.resolve() == new_resolved:
                continue
            source = self._read_text(path)
            lines = source.splitlines(keepends=True)
            new_lines = []
            changed = False
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    rewritten = segment_re.sub(new_stem, line)
                    if rewritten != line:
                        changed = True
                    new_lines.append(rewritten)
                else:
                    new_lines.append(line)
            if changed:
                self._write_text(path, "".join(new_lines))
                changed_files.append(str(path))

        # Reindex only the renamed file and the files whose imports changed.
        # Do NOT call symbol_index.scan() — that re-parses the entire workspace.
        self._workspace_index_cache = None
        affected: list[Path] = [new] + [Path(f) for f in changed_files]
        for p in affected:
            self._file_index_cache.pop(str(p.resolve()), None)
        self.symbol_index.reindex_file(new)
        for f in changed_files:
            self.symbol_index.reindex_file(Path(f))
        return {
            "accepted": True,
            "old": str(old),
            "new": str(new),
            "old_module": old_module,
            "new_module": new_module,
            "changed_files": changed_files,
        }
