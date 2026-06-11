from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lang.router import adapter_for, supported_extensions
from .file_suffix import category_names, flags_from_filename
from .stable_index import module_name_for_path


@dataclass(slots=True)
class FunctionSymbol:
    qname: str
    file_path: str
    name: str
    qualname: str
    start_line: int
    end_line: int
    start_col: int
    end_col: int
    class_path: tuple[str, ...]
    node: ast.AST


class FunctionCallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def collect(self, node: ast.AST) -> list[ast.Call]:
        body = getattr(node, "body", [])
        for statement in body:
            self.visit(statement)
        return self.calls

    def visit_Call(self, node: ast.Call) -> Any:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        return None


_CALL_GRAPH_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def clear_call_graph_cache() -> None:
    """Drop all cached call-graph results for every workspace root."""
    _CALL_GRAPH_CACHE.clear()


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


def _parse_source(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _call_text(source: str, node: ast.Call) -> str:
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


def _resolve_local_import(root: Path, current_path: Path, module: str) -> Path | None:
    wr = root.resolve()
    fp = current_path.resolve()

    def _check(candidate: Path) -> Path | None:
        try:
            resolved = candidate.resolve()
            if resolved == fp or not resolved.exists():
                return None
            resolved.relative_to(wr)
            return resolved
        except (ValueError, OSError):
            return None

    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        remainder = module.lstrip(".")
        base = current_path.parent
        for _ in range(max(level - 1, 0)):
            base = base.parent
        candidate = base / f"{remainder.replace('.', '/')}.py" if remainder else base / "__init__.py"
        return _check(candidate)

    parts = module.replace(".", "/")
    return _check(root / f"{parts}.py") or _check(current_path.parent / f"{module.split('.')[-1]}.py")


def _collect_local_imports(root: Path, path: Path, source: str) -> list[dict[str, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    deps: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = "." * node.level + node.module if node.level else node.module
            resolved = _resolve_local_import(root, path, module)
            if resolved is None:
                continue
            module_name = module_name_for_path(root, resolved)
            deps[str(resolved)] = {
                "kind": "import",
                "source": module_name_for_path(root, path),
                "source_file": str(path.resolve()),
                "target": module_name,
                    "target_file": str(resolved),
                }
        elif isinstance(node, ast.ImportFrom) and node.level and not node.module:
            for alias in node.names:
                module = "." * node.level + alias.name
                resolved = _resolve_local_import(root, path, module)
                if resolved is None:
                    continue
                module_name = module_name_for_path(root, resolved)
                deps[str(resolved)] = {
                    "kind": "import",
                    "source": module_name_for_path(root, path),
                    "source_file": str(path.resolve()),
                    "target": module_name,
                    "target_file": str(resolved),
                }
        elif isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_local_import(root, path, alias.name)
                if resolved is None:
                    continue
                module_name = module_name_for_path(root, resolved)
                deps[str(resolved)] = {
                    "kind": "import",
                    "source": module_name_for_path(root, path),
                    "source_file": str(path.resolve()),
                    "target": module_name,
                    "target_file": str(resolved),
                }
    return sorted(deps.values(), key=lambda item: (item["source"], item["target"], item["target_file"]))


def _collect_import_aliases(root: Path, path: Path, source: str) -> dict[str, list[str]]:
    """Return local import aliases mapped to candidate qnames or module qnames."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = "." * node.level + node.module if node.module else "." * node.level
            resolved = _resolve_local_import(root, path, module) if module else None
            if resolved is None:
                continue
            module_qname = module_name_for_path(root, resolved)
            for alias in node.names:
                alias_name = alias.asname or alias.name.split(".")[-1]
                imported_name = alias.name.split(".")[-1]
                aliases.setdefault(alias_name, set()).add(f"{module_qname}:{imported_name}")
                aliases[alias_name].add(module_qname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_local_import(root, path, alias.name)
                if resolved is None:
                    continue
                alias_name = alias.asname or alias.name.split(".")[-1]
                aliases.setdefault(alias_name, set()).add(module_name_for_path(root, resolved))
    return {name: sorted(targets) for name, targets in aliases.items()}


def _module_metadata(root: Path, path: Path, source: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
    except SyntaxError:
        docstring = None
    flags = flags_from_filename(path.name)
    module_name = module_name_for_path(root, path)
    return {
        "module": module_name,
        "file_path": str(path.resolve()),
        "docstring": docstring,
        "flags": int(flags),
        "categories": category_names(flags),
    }


def _build_reverse_dependencies(module_dependencies: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    reverse: dict[str, list[dict[str, str]]] = {}
    for edge in module_dependencies:
        reverse.setdefault(edge["target"], []).append(edge)
    for target, edges in reverse.items():
        edges.sort(key=lambda item: (item["source"], item["source_file"], item["target"]))
    return reverse


def _module_graph_adjacency(module_dependencies: list[dict[str, str]]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in module_dependencies:
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
        adjacency.setdefault(edge["target"], set())
    return adjacency


def _detect_cycles(adjacency: dict[str, set[str]]) -> list[dict[str, Any]]:
    cycles: dict[tuple[str, ...], dict[str, Any]] = {}
    visiting: list[str] = []
    seen: set[str] = set()

    def _canonical(path: list[str]) -> tuple[str, ...]:
        if not path:
            return tuple()
        body = path[:-1] if path[0] == path[-1] else path[:]
        if not body:
            return tuple(path)
        rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
        return min(rotations)

    def _dfs(node: str) -> None:
        visiting.append(node)
        for neighbor in sorted(adjacency.get(node, set())):
            if neighbor in visiting:
                start = visiting.index(neighbor)
                cycle_path = visiting[start:] + [neighbor]
                key = _canonical(cycle_path)
                if key not in cycles:
                    cycles[key] = {
                        "modules": cycle_path,
                        "length": len(cycle_path) - 1,
                    }
                continue
            if neighbor not in seen:
                _dfs(neighbor)
        visiting.pop()
        seen.add(node)

    for node in sorted(adjacency):
        if node not in seen:
            _dfs(node)
    return sorted(cycles.values(), key=lambda item: (item["length"], item["modules"]))


def _function_hotspots(function_fan_in: dict[str, int], functions: list[FunctionSymbol], threshold: int) -> list[dict[str, Any]]:
    by_qname = {fn.qname: fn for fn in functions}
    hotspots: list[dict[str, Any]] = []
    for qname, count in sorted(function_fan_in.items(), key=lambda item: (-item[1], item[0])):
        if count < threshold:
            continue
        fn = by_qname.get(qname)
        if fn is None:
            continue
        hotspots.append(
            {
                "qname": fn.qname,
                "module": fn.qname.split(":", 1)[0],
                "file_path": fn.file_path,
                "name": fn.name,
                "qualname": fn.qualname,
                "fan_in": count,
                "start_line": fn.start_line,
                "end_line": fn.end_line,
                "start_col": fn.start_col,
                "end_col": fn.end_col,
            }
        )
    return hotspots


def _source_files_for_call_graph(root: Path, files: list[Path] | None = None) -> list[Path]:
    if files is not None:
        return [path for path in sorted(files) if path.exists() and path.suffix.lower() in supported_extensions()]
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in supported_extensions():
            continue
        if any(part.startswith(".") and part not in {".", ".."} for part in path.relative_to(root).parts):
            continue
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        if path.name.endswith(".min.js") or path.name.endswith(".bundle.js"):
            continue
        paths.append(path)
    return paths


def _collect_functions_generic(root: Path, path: Path, source: str) -> list[FunctionSymbol]:
    adapter = adapter_for(path)
    if adapter is None:
        return []
    tree = adapter.parse(source)
    if tree is None:
        return []
    functions: list[FunctionSymbol] = []
    for detail in adapter.extract_symbols(tree, path):
        if detail.kind not in {"function", "method"}:
            continue
        class_path = tuple((detail.parent_qualname or "").split(".")) if detail.parent_qualname else tuple()
        functions.append(
            FunctionSymbol(
                qname=module_name_for_path(root, path) + ":" + detail.qualname,
                file_path=str(path.resolve()),
                name=detail.name,
                qualname=detail.qualname,
                start_line=detail.start_line,
                end_line=detail.end_line,
                start_col=detail.start_col,
                end_col=detail.end_col,
                class_path=class_path,
                node=detail.node,
            )
        )
    return functions


def _build_lookup_generic(functions: list[FunctionSymbol]) -> tuple[dict[str, list[FunctionSymbol]], dict[tuple[str, ...], dict[str, FunctionSymbol]]]:
    by_name: dict[str, list[FunctionSymbol]] = {}
    by_class_and_name: dict[tuple[str, ...], dict[str, FunctionSymbol]] = {}
    for fn in functions:
        by_name.setdefault(fn.name, []).append(fn)
        by_class_and_name.setdefault(fn.class_path, {})[fn.name] = fn
    return by_name, by_class_and_name


def _resolve_call_generic(
    caller: FunctionSymbol,
    callee: str,
    by_name: dict[str, list[FunctionSymbol]],
    by_class_and_name: dict[tuple[str, ...], dict[str, FunctionSymbol]],
    alias_targets: dict[str, list[str]] | None = None,
) -> FunctionSymbol | None:
    if alias_targets:
        alias_matches = alias_targets.get(callee)
        if alias_matches:
            for target in alias_matches:
                for fn in by_name.get(target.split(":")[-1], []):
                    if fn.qname == target or fn.qualname == target.split(":", 1)[-1]:
                        return fn
    exact = [fn for fn in by_name.get(callee, []) if fn.qname.endswith(f":{callee}") or fn.qualname == callee]
    if len(exact) == 1:
        return exact[0]
    if callee in by_name and len(by_name[callee]) == 1:
        return by_name[callee][0]

    if "." in callee:
        tail = callee.split(".")[-1]
        exact_tail = [fn for fn in by_name.get(tail, []) if fn.qualname.endswith(callee) or fn.qname.endswith(f":{callee}")]
        if len(exact_tail) == 1:
            return exact_tail[0]

    same_file = [fn for fn in by_name.get(callee, []) if fn.file_path == caller.file_path]
    if len(same_file) == 1:
        return same_file[0]

    if caller.class_path:
        for depth in range(len(caller.class_path), 0, -1):
            candidate = by_class_and_name.get(caller.class_path[:depth], {}).get(callee.split(".")[-1])
            if candidate is not None:
                return candidate

    for fn in by_name.get(callee.split(".")[-1], []):
        if fn.file_path == caller.file_path:
            return fn
    return None


def _call_resolution_reason(
    caller: FunctionSymbol,
    call: ast.Call,
    by_name: dict[str, list[FunctionSymbol]],
    alias_targets: dict[str, list[str]] | None = None,
) -> str:
    callee_name = _call_name(call.func)
    if alias_targets and callee_name in alias_targets:
        return "alias_unresolved"
    if isinstance(call.func, ast.Name):
        matches = by_name.get(call.func.id, [])
        if len(matches) > 1:
            return "ambiguous_name"
        if matches:
            return "unresolved_name"
    if isinstance(call.func, ast.Attribute):
        attr_name = call.func.attr
        matches = by_name.get(attr_name, [])
        if len(matches) > 1:
            return "ambiguous_attribute"
        if isinstance(call.func.value, ast.Name) and call.func.value.id in {"self", "cls", "super"}:
            return "unresolved_method"
        if matches:
            return "unresolved_attribute"
    return "unresolved_call"


def _call_graph_cache_key(
    root: Path,
    files: list[Path] | None,
    hotspot_threshold: int,
    include_sources: bool,
    include_unresolved: bool,
) -> tuple[Any, ...]:
    if files is None:
        paths = tuple(sorted(path for path in root.rglob("*.py")))
    else:
        paths = tuple(sorted(path.resolve() for path in files))
    file_state = tuple(
        (
            str(path),
            path.stat().st_mtime_ns if path.exists() else 0,
            path.stat().st_size if path.exists() else 0,
        )
        for path in paths
    )
    return (str(root.resolve()), file_state, hotspot_threshold, include_sources, include_unresolved)


def build_call_graph(
    root: Path,
    files: list[Path] | None = None,
    hotspot_threshold: int = 3,
    include_sources: bool = False,
    include_unresolved: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    source_files = _source_files_for_call_graph(root, files=files)
    cache_key = _call_graph_cache_key(root, source_files, hotspot_threshold, include_sources, include_unresolved)
    cached = _CALL_GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    functions: list[FunctionSymbol] = []
    per_file_source: dict[str, str] = {}
    module_dependencies: list[dict[str, str]] = []
    module_nodes_by_file: dict[str, dict[str, Any]] = {}
    module_nodes_by_name: dict[str, dict[str, Any]] = {}
    call_edges: list[dict[str, Any]] = []
    alias_targets_by_file: dict[str, dict[str, list[str]]] = {}
    function_lookup: dict[str, FunctionSymbol] = {}

    for path in source_files:
        adapter = adapter_for(path)
        if adapter is None:
            continue
        source = _read_source(path)
        tree = adapter.parse(source)
        if tree is None:
            continue
        per_file_source[str(path.resolve())] = source
        meta = _module_metadata(root, path, source)
        module_nodes_by_file[meta["file_path"]] = {**meta, "imports": []}
        module_nodes_by_name[meta["module"]] = module_nodes_by_file[meta["file_path"]]
        new_functions = _collect_functions_generic(root, path, source)
        functions.extend(new_functions)
        alias_targets_by_file[str(path.resolve())] = _collect_import_aliases(root, path, source) if path.suffix.lower() == ".py" else {}
        for fn in new_functions:
            function_lookup[fn.qname] = fn

        if path.suffix.lower() == ".py":
            module_dependencies.extend(_collect_local_imports(root, path, source))
        else:
            for edge in adapter.extract_imports(tree, path):
                module_dependencies.append(
                    {
                        "source": meta["module"],
                        "source_file": str(path.resolve()),
                        "target": edge.target,
                        "target_file": edge.target_file,
                        "kind": edge.kind,
                        "detail": edge.detail,
                    }
                )

        for call in adapter.extract_calls(tree, path, source):
            call_edges.append(
                {
                    "caller_qname": call.caller_qname,
                    "caller_file": call.caller_file,
                    "callee": call.callee,
                    "line": call.line,
                    "end_line": call.end_line,
                    "col": call.col,
                    "end_col": call.end_col,
                    "source": call.source,
                    "resolved_qname": call.resolved_qname,
                }
            )

    by_name, by_class_and_name = _build_lookup_generic(functions)
    callers: list[dict[str, Any]] = []
    function_fan_in: dict[str, int] = {}

    by_caller: dict[str, list[dict[str, Any]]] = {}
    for call in call_edges:
        by_caller.setdefault(call["caller_qname"], []).append(call)

    for fn in sorted(functions, key=lambda item: (item.file_path, item.start_line, item.start_col, item.qualname)):
        source = per_file_source[fn.file_path]
        calls: list[dict[str, Any]] = []
        for call in by_caller.get(fn.qname, []):
            resolved = None
            if call.get("resolved_qname") and call["resolved_qname"] in function_lookup:
                resolved = function_lookup[call["resolved_qname"]]
            if resolved is None:
                resolved = _resolve_call_generic(
                    fn,
                    call["callee"],
                    by_name,
                    by_class_and_name,
                    alias_targets=alias_targets_by_file.get(fn.file_path, {}),
                )
            if resolved is not None:
                function_fan_in[resolved.qname] = function_fan_in.get(resolved.qname, 0) + 1
            elif not include_unresolved:
                continue
            call_payload = {
                "callee": call["callee"],
                "resolved": None
                if resolved is None
                else {
                    "qname": resolved.qname,
                    "file_path": resolved.file_path,
                    "name": resolved.name,
                    "qualname": resolved.qualname,
                    "start_line": resolved.start_line,
                    "end_line": resolved.end_line,
                    "start_col": resolved.start_col,
                    "end_col": resolved.end_col,
                },
                "line": call["line"],
                "col": call["col"],
                "end_line": call["end_line"],
                "end_col": call["end_col"],
            }
            if resolved is None:
                try:
                    synthetic_func = ast.parse(call["callee"], mode="eval").body
                except SyntaxError:
                    synthetic_func = ast.Name(id=call["callee"], ctx=ast.Load())
                call_payload["reason"] = "alias_unresolved" if call["callee"] in alias_targets_by_file.get(fn.file_path, {}) else _call_resolution_reason(
                    fn,
                    ast.Call(func=synthetic_func, args=[], keywords=[]),
                    by_name,
                    alias_targets_by_file.get(fn.file_path, {}),
                )
            if include_sources:
                call_payload["source"] = call["source"]
            calls.append(call_payload)

        caller_imports = [dep for dep in module_dependencies if dep["source_file"] == fn.file_path]
        module_node = module_nodes_by_file.get(fn.file_path)
        if module_node is not None:
            module_node["imports"] = caller_imports
        callers.append(
            {
                "caller": {
                    "qname": fn.qname,
                    "file_path": fn.file_path,
                    "name": fn.name,
                    "qualname": fn.qualname,
                    "start_line": fn.start_line,
                    "end_line": fn.end_line,
                    "start_col": fn.start_col,
                    "end_col": fn.end_col,
                },
                "calls": sorted(calls, key=lambda item: (item["line"], item["col"], item["callee"])),
            }
        )

    enriched_module_dependencies: list[dict[str, Any]] = []
    for edge in module_dependencies:
        source_meta = module_nodes_by_file.get(edge["source_file"], {})
        target_meta = module_nodes_by_file.get(edge["target_file"], {})
        enriched_module_dependencies.append(
            {
                **edge,
                "source_docstring": source_meta.get("docstring"),
                "source_categories": source_meta.get("categories", []),
                "source_flags": source_meta.get("flags", 0),
                "target_docstring": target_meta.get("docstring"),
                "target_categories": target_meta.get("categories", []),
                "target_flags": target_meta.get("flags", 0),
            }
        )

    reverse_dependencies = _build_reverse_dependencies(enriched_module_dependencies)
    adjacency = _module_graph_adjacency(enriched_module_dependencies)
    cycles = _detect_cycles(adjacency)

    source_files_set = {edge["source_file"] for edge in enriched_module_dependencies}
    target_files_set = {edge["target_file"] for edge in enriched_module_dependencies if edge.get("target_file")}
    all_files = set(per_file_source.keys())
    entry_point_files = sorted(source_files_set - target_files_set)
    leaf_files = sorted(target_files_set - source_files_set)
    orphan_files = sorted(all_files - source_files_set - target_files_set)

    def _role_payload(file_path: str) -> dict[str, Any]:
        path = Path(file_path)
        flags = flags_from_filename(path.name)
        return {
            "file_path": file_path,
            "filename": path.name,
            "flags": int(flags),
            "categories": category_names(flags),
        }

    module_nodes = {
        module: {
            **meta,
            "inbound": len(reverse_dependencies.get(module, [])),
            "outbound": len(adjacency.get(module, set()) - {module}),
        }
        for module, meta in module_nodes_by_name.items()
    }
    entry_point_file_set = set(entry_point_files)
    leaf_file_set = set(leaf_files)
    orphan_file_set = set(orphan_files)
    for module, meta in module_nodes.items():
        file_path = meta["file_path"]
        meta["entry_point"] = file_path in entry_point_file_set
        meta["leaf"] = file_path in leaf_file_set
        meta["orphan"] = file_path in orphan_file_set
        reverse_dependencies.setdefault(module, [])

    hotspots = _function_hotspots(function_fan_in, functions, hotspot_threshold)

    result = {
        "root": str(root),
        "call_graph": callers,
        "module_nodes": module_nodes,
        "module_dependencies": sorted(enriched_module_dependencies, key=lambda item: (item["source"], item["target"], item["source_file"], item.get("target_file") or "")),
        "reverse_dependencies": reverse_dependencies,
        "sources": sorted({edge["source"] for edge in enriched_module_dependencies}),
        "targets": sorted({edge["target"] for edge in enriched_module_dependencies}),
        "source_files": sorted(source_files_set),
        "target_files": sorted(target_files_set),
        "entry_points": [_role_payload(file_path) for file_path in entry_point_files],
        "leaves": [_role_payload(file_path) for file_path in leaf_files],
        "orphans": [_role_payload(file_path) for file_path in orphan_files],
        "hotspots": hotspots,
        "cycles": cycles,
        "function_count": len(functions),
    }
    _CALL_GRAPH_CACHE[cache_key] = result
    return result
