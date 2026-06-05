from __future__ import annotations

import argparse
import ast as _ast
import json
import os
import py_compile
import re as _re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .call_graph import build_call_graph, _collect_local_imports as _collect_local_imports
from .file_suffix import (
    apply_numeric_suffix,
    category_names,
    category_from_name,
    decode_filename_suffix as _decode_filename_suffix,
    infer_file_suffix_result,
    flags_from_filename,
    suffix_number,
)
from .lang.router import adapter_for, supported_extensions
from .static_analysis import analyze_workspace
from .stable_index import module_name_for_path
from .working_set import WorkingSet
from .workflow_tracker import WorkflowTracker
from .tools.regex_rules import generate_regex_rule as _generate_regex_rule

_PLAN_FILE = ".mcp_plan.json"
_DEP_TAG_MARKER = "__deps_"
_SOURCE_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "node_modules",
    "tests",
    "test",
    "testing",
    "fixtures",
    "migrations",
    "migration",
    "docs",
    "doc",
    "examples",
    "example",
    "vendor",
    "vendors",
}
_SCAFFOLD_TYPING_IMPORTS = {
    "Any",
    "Callable",
    "ClassVar",
    "Concatenate",
    "Final",
    "ForwardRef",
    "Generator",
    "Generic",
    "Iterable",
    "Iterator",
    "Literal",
    "Mapping",
    "MutableMapping",
    "MutableSequence",
    "Optional",
    "ParamSpec",
    "Protocol",
    "Sequence",
    "Self",
    "Set",
    "TypedDict",
    "TypeAlias",
    "TypeGuard",
    "TypeVar",
    "TypeVarTuple",
    "Union",
}


def _load_module_plan(root: Path) -> dict[str, Any]:
    plan_path = root / _PLAN_FILE
    if not plan_path.exists():
        return {"_ignored": set()}
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_ignored": set()}
    if not isinstance(payload, dict):
        return {"_ignored": set()}
    plan: dict[str, Any] = {"_ignored": set()}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key == "_ignored":
            if isinstance(value, list):
                plan["_ignored"] = {str(item) for item in value if isinstance(item, str)}
            elif isinstance(value, set):
                plan["_ignored"] = {str(item) for item in value}
            continue
        # Store dicts (file entries) and plain scalar metadata (e.g. _focus, _workflow).
        if isinstance(value, (dict, str, int, float, bool)) or value is None:
            plan[key] = value
    return plan


def _save_module_plan(root: Path, module_plan: dict[str, Any]) -> None:
    plan_path = root / _PLAN_FILE
    payload: dict[str, Any] = {}
    for key, value in module_plan.items():
        if key == "_ignored":
            if isinstance(value, set):
                payload[key] = sorted(str(item) for item in value)
            elif isinstance(value, list):
                payload[key] = sorted(str(item) for item in value)
            else:
                payload[key] = []
            continue
        payload[key] = value
    plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _module_plan_file_names(module_plan: dict[str, Any]) -> list[str]:
    return [name for name in module_plan if isinstance(name, str) and not name.startswith("_")]


def _module_plan_ignored_paths(workspace: Any, module_plan: dict[str, Any]) -> set[Path]:
    ignored: set[Path] = set()
    for item in module_plan.get("_ignored", set()) or set():
        try:
            ignored.add(workspace._resolve_path(str(item)))
        except ValueError:
            continue
    return ignored


def _set_module_plan_ignored_paths(workspace: Any, module_plan: dict[str, Any], ignored_paths: set[Path]) -> None:
    resolved_paths = {workspace._resolve_path(path) for path in ignored_paths}
    module_plan["_ignored"] = {str(path) for path in resolved_paths}
    if hasattr(workspace, "_ignored_paths"):
        workspace._ignored_paths = resolved_paths


def _sync_ignored_path_rename(workspace: Any, module_plan: dict[str, Any], old_path: Path, new_path: Path) -> None:
    ignored = _module_plan_ignored_paths(workspace, module_plan)
    if old_path.resolve() not in ignored:
        return
    ignored.discard(old_path.resolve())
    ignored.add(new_path.resolve())
    _set_module_plan_ignored_paths(workspace, module_plan, ignored)


def _planned_workspace_files(workspace: Any, module_plan: dict[str, Any]) -> list[Path]:
    planned_paths: list[Path] = []
    ignored_paths = _module_plan_ignored_paths(workspace, module_plan)
    for name in _module_plan_file_names(module_plan):
        try:
            p = workspace._resolve_path(name)
            if p.exists() and p not in ignored_paths:
                planned_paths.append(p)
        except ValueError:
            pass
    return planned_paths


def _file_is_source_candidate(path: Path, langs: set[str] | None = None) -> bool:
    parts = path.relative_to(path.anchor).parts if path.is_absolute() else path.parts
    if any(part.startswith(".") and part not in {".", ".."} for part in parts):
        return False
    if "__pycache__" in parts:
        return False
    if any(part in _SOURCE_EXCLUDED_DIRS for part in parts[:-1]):
        return False
    allowed = {ext.lower() for ext in langs} if langs is not None else supported_extensions()
    return path.suffix.lower() in allowed


def _workspace_source_files(
    workspace: Any,
    module_plan: dict[str, Any],
    files: list[str] | None = None,
    roots: list[str] | None = None,
    langs: set[str] | None = None,
) -> list[Path]:
    ignored_paths = _module_plan_ignored_paths(workspace, module_plan)
    allowed = {ext.lower() for ext in langs} if langs is not None else None

    def _not_ignored(path: Path) -> bool:
        return path.resolve() not in ignored_paths

    if files is not None:
        resolved: list[Path] = []
        for item in files:
            try:
                path = workspace._resolve_path(item)
            except ValueError:
                continue
            if path.exists() and _file_is_source_candidate(path, allowed) and _not_ignored(path):
                resolved.append(path)
        return sorted(dict.fromkeys(resolved))

    if roots:
        selected: list[Path] = []
        for root_item in roots:
            try:
                root_path = workspace._resolve_path(root_item)
            except ValueError:
                continue
            if root_path.is_file() and _file_is_source_candidate(root_path, allowed):
                if _not_ignored(root_path):
                    selected.append(root_path)
                continue
            if not root_path.exists() or not root_path.is_dir():
                continue
            for path in sorted(root_path.rglob("*")):
                if path.is_file() and _file_is_source_candidate(path, allowed) and _not_ignored(path):
                    selected.append(path)
        return sorted(dict.fromkeys(selected))

    if module_plan:
        planned = _planned_workspace_files(workspace, module_plan)
        if planned:
            return [path for path in planned if _file_is_source_candidate(path, allowed) and _not_ignored(path)]

    # When a focus directory is active, scope iteration to it by default.
    focus = getattr(workspace, "_focus_dir", None)
    scan_root = focus if focus is not None else None

    scan_root_str = str(scan_root) + os.sep if scan_root is not None else None
    candidates = [
        path for path in workspace._iter_workspace_source_files(langs=allowed)
        if _file_is_source_candidate(path, allowed)
        and (scan_root_str is None or str(path.resolve()).startswith(scan_root_str))
    ]
    if candidates:
        return candidates
    return workspace._iter_workspace_source_files(langs=allowed)


def _workspace_python_files(workspace: Any, module_plan: dict[str, Any], files: list[str] | None = None, roots: list[str] | None = None) -> list[Path]:
    return _workspace_source_files(workspace, module_plan, files=files, roots=roots, langs={".py"})


def _split_dep_tag(stem: str) -> tuple[str, str]:
    if _DEP_TAG_MARKER in stem:
        base, tag = stem.split(_DEP_TAG_MARKER, 1)
        return base, tag
    if "__" in stem:
        base, tag = stem.rsplit("__", 1)
        if base and tag and tag.isalnum():
            return base, tag
    return stem, ""


def _annotation_type_names(source: str) -> set[str]:
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name) and node.id in _SCAFFOLD_TYPING_IMPORTS:
            names.add(node.id)
    return names


def _scaffold_typing_imports(stubs: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for stub in stubs:
        kind = stub.get("kind", "function")
        if kind == "class":
            bases_value = stub.get("bases", "")
            if isinstance(bases_value, (list, tuple)):
                bases = ", ".join(str(base).strip() for base in bases_value if str(base).strip())
            else:
                bases = str(bases_value).strip()
            if bases:
                names.update(_annotation_type_names(f"class _Probe({bases}):\n    pass\n"))
        else:
            args = str(stub.get("args", "")).strip()
            returns = str(stub.get("returns", "")).strip() or "None"
            try:
                probe = f"def _probe({args}) -> {returns}:\n    pass\n" if args else f"def _probe() -> {returns}:\n    pass\n"
                names.update(_annotation_type_names(probe))
            except SyntaxError:
                continue
    return sorted(names)


def _test_module_code(module_stem: str, stubs: list[dict[str, Any]], description: str) -> str:
    test_names: list[str] = []
    for stub in stubs:
        if stub.get("kind", "function") not in {"function", "async_function"}:
            continue
        name = str(stub.get("name", "")).strip()
        if not name:
            continue
        test_names.append(_re.sub(r"[^a-zA-Z0-9_]+", "_", name))

    lines = [
        f'"""Failing tests for {module_stem}."""',
        "from __future__ import annotations",
        "",
    ]
    if description:
        lines.append(f"# {description}")
        lines.append("")
    for test_name in test_names:
        lines.extend(
            [
                f"def test_{test_name}() -> None:",
                f"    \"\"\"Fail until `{test_name}` is implemented.\"\"\"",
                "    raise NotImplementedError",
                "",
            ]
        )
    if len(lines) == 3:
        lines.extend(
            [
                "def test_placeholder() -> None:",
                '    """Fail until the module gains public functions."""',
                "    raise NotImplementedError",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _sync_numeric_suffix(workspace: Any, module_plan: dict[str, dict[str, Any]], path: Path) -> tuple[Path, dict[str, Any] | None]:
    """Rename a newly created Python file to include its numeric category suffix, if any.

    The numeric suffix is inserted before any dep-tag so the final form is:
        verb_noun_<flags>__deps_<tag>.py
    not:
        verb_noun__deps_<tag>_<flags>.py
    """
    if path.suffix.lower() != ".py" or not path.exists():
        return path, None

    stem = path.stem
    # Preserve any existing dep-tag so the numeric suffix lands before it.
    if _DEP_TAG_MARKER in stem:
        base_stem, dep_tag_rest = stem.split(_DEP_TAG_MARKER, 1)
        dep_tag = _DEP_TAG_MARKER + dep_tag_rest
    else:
        base_stem, dep_tag = stem, ""

    suffix_result = infer_file_suffix_result(workspace._read_text(path))
    desired_name = apply_numeric_suffix(base_stem, suffix_result.flags) + dep_tag + path.suffix
    if desired_name == path.name:
        return path, {
            "value": suffix_number(suffix_result.flags),
            "modules": suffix_result.modules,
            "calls": suffix_result.calls,
        }

    renamed_path = path.with_name(desired_name)
    rename_result = workspace.rename_file(path, renamed_path)
    if not rename_result.get("accepted"):
        return path, {
            "value": suffix_number(suffix_result.flags),
            "modules": suffix_result.modules,
            "calls": suffix_result.calls,
            "rename_failed": rename_result,
        }

    if path.name in module_plan:
        module_plan[desired_name] = module_plan.pop(path.name)
        _save_module_plan(workspace.root, module_plan)
    _sync_ignored_path_rename(workspace, module_plan, path, renamed_path)

    return renamed_path, {
        "value": suffix_number(suffix_result.flags),
        "modules": suffix_result.modules,
        "calls": suffix_result.calls,
    }


def _resync_numeric_suffix(workspace: Any, module_plan: dict[str, dict[str, Any]], path: Path) -> tuple[Path, dict[str, Any] | None]:
    """Refresh a Python file's numeric suffix after any successful write."""
    if path.suffix.lower() != ".py" or not path.exists():
        return path, None
    return _sync_numeric_suffix(workspace, module_plan, path)


def _module_initials(stem: str) -> str:
    """Return first char of each underscore-separated word, ignoring numeric segments.

    Examples:
        load_captions          → lc
        manage_token_4096      → mt   (4096 is a category suffix, not a word)
        store_user_2097152     → su
    """
    return "".join(p[0] for p in stem.split("_") if p and not p.isdigit())


def _compute_local_imports(file_path: Path, workspace_root: Path) -> list[str]:
    """Return sorted stems of workspace-local modules imported by file_path, excluding external packages."""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
    except (OSError, SyntaxError):
        return []

    wr = workspace_root.resolve()
    fp = file_path.resolve()
    seen: set[str] = set()

    def _check(candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
            if resolved == fp or not resolved.exists():
                return
            resolved.relative_to(wr)  # raises ValueError if outside workspace
            seen.add(resolved.stem)
        except (ValueError, OSError):
            pass

    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            leaf = node.module.split(".")[-1]
            if node.level > 0:
                # relative import — resolve relative to the file's package directory
                _check(file_path.parent / f"{leaf}.py")
            else:
                # absolute import — try workspace root and same directory
                parts = node.module.replace(".", "/")
                _check(workspace_root / f"{parts}.py")
                _check(file_path.parent / f"{leaf}.py")
        elif isinstance(node, _ast.Import):
            for alias in node.names:
                leaf = alias.name.split(".")[-1]
                parts = alias.name.replace(".", "/")
                _check(workspace_root / f"{parts}.py")
                _check(file_path.parent / f"{leaf}.py")

    return sorted(seen)


def _resolve_graph_target_module(workspace: Any, target: str) -> str:
    candidate = Path(target)
    if ":" in target and not candidate.exists():
        target = target.split(":", 1)[0]
    try:
        resolved = workspace._resolve_path(target)
        if resolved.exists():
            return module_name_for_path(workspace.root, resolved)
    except ValueError:
        pass
    if target.endswith(".py"):
        try:
            resolved = workspace._resolve_path(target)
            if resolved.exists():
                return module_name_for_path(workspace.root, resolved)
        except ValueError:
            pass
    return target


def _compile_python_file(path: Path) -> dict[str, Any]:
    """Run a Python compile check on a file and return a structured result."""
    if path.suffix.lower() != ".py" or not path.exists():
        return {
            "ok": True,
            "skipped": True,
            "path": str(path),
            "reason": "not_a_python_file",
        }
    try:
        py_compile.compile(str(path), doraise=True)
        return {
            "ok": True,
            "path": str(path),
            "compiled": True,
            "error": None,
        }
    except py_compile.PyCompileError as exc:
        return {
            "ok": False,
            "path": str(path),
            "compiled": False,
            "error": str(exc),
        }


def _induced_dependency_subgraph(graph: dict[str, Any], target_module: str, include_dependents: bool = False) -> dict[str, Any]:
    module_nodes: dict[str, dict[str, Any]] = graph.get("module_nodes", {})
    module_dependencies: list[dict[str, Any]] = graph.get("module_dependencies", [])
    reverse_dependencies: dict[str, list[dict[str, Any]]] = graph.get("reverse_dependencies", {})

    if target_module not in module_nodes:
        return {
            "ok": False,
            "reason": "target_not_found",
            "target_module": target_module,
        }

    outgoing: dict[str, set[str]] = {}
    incoming: dict[str, set[str]] = {}
    for edge in module_dependencies:
        outgoing.setdefault(edge["source"], set()).add(edge["target"])
        incoming.setdefault(edge["target"], set()).add(edge["source"])

    relevant: set[str] = set()
    forward_seen: set[str] = set()
    stack = [target_module]
    while stack:
        module = stack.pop()
        if module in forward_seen:
            continue
        forward_seen.add(module)
        relevant.add(module)
        for neighbor in outgoing.get(module, set()):
            if neighbor not in forward_seen:
                stack.append(neighbor)

    dependents: set[str] = set()
    if include_dependents:
        reverse_seen: set[str] = set()
        stack = [target_module]
        while stack:
            module = stack.pop()
            if module in reverse_seen:
                continue
            reverse_seen.add(module)
            relevant.add(module)
            dependents.add(module)
            for neighbor in incoming.get(module, set()):
                if neighbor not in reverse_seen:
                    stack.append(neighbor)

    filtered_module_dependencies = [
        edge for edge in module_dependencies if edge["source"] in relevant and edge["target"] in relevant
    ]
    filtered_outgoing: dict[str, set[str]] = {}
    filtered_incoming: dict[str, set[str]] = {}
    for edge in filtered_module_dependencies:
        filtered_outgoing.setdefault(edge["source"], set()).add(edge["target"])
        filtered_incoming.setdefault(edge["target"], set()).add(edge["source"])
    filtered_reverse_dependencies = {
        target: [edge for edge in edges if edge["source"] in relevant and edge["target"] in relevant]
        for target, edges in reverse_dependencies.items()
        if target in relevant
    }
    for module in relevant:
        filtered_reverse_dependencies.setdefault(module, [])
    filtered_module_nodes = {
        module: {
            **node,
            "inbound": len(filtered_incoming.get(module, set())),
            "outbound": len(filtered_outgoing.get(module, set()) - {module}),
        }
        for module, node in module_nodes.items()
        if module in relevant
    }
    for node in filtered_module_nodes.values():
        has_inbound = node["inbound"] > 0
        has_outbound = node["outbound"] > 0
        node["entry_point"] = (not has_inbound) and has_outbound
        node["leaf"] = not has_outbound
        node["orphan"] = (not has_inbound) and (not has_outbound)
    filtered_call_graph = []
    for item in graph.get("call_graph", []):
        caller = item.get("caller", {})
        caller_qname = caller.get("qname", "")
        caller_module = caller_qname.split(":", 1)[0] if isinstance(caller_qname, str) else ""
        if caller_module not in relevant:
            continue
        filtered_calls = []
        for call in item.get("calls", []):
            resolved = call.get("resolved")
            resolved_module = ""
            if isinstance(resolved, dict):
                resolved_qname = resolved.get("qname", "")
                if isinstance(resolved_qname, str):
                    resolved_module = resolved_qname.split(":", 1)[0]
            if resolved_module and resolved_module not in relevant:
                continue
            filtered_calls.append(call)
        filtered_item = dict(item)
        filtered_item["calls"] = filtered_calls
        filtered_call_graph.append(filtered_item)

    filtered_hotspots = [item for item in graph.get("hotspots", []) if item.get("module") in relevant]

    entries = [node for node in filtered_module_nodes.values() if node.get("entry_point")]
    leaves = [node for node in filtered_module_nodes.values() if node.get("leaf")]
    orphans = [node for node in filtered_module_nodes.values() if node.get("orphan")]

    return {
        "ok": True,
        "target_module": target_module,
        "include_dependents": include_dependents,
        "relevant_modules": sorted(relevant),
        "module_nodes": filtered_module_nodes,
        "module_dependencies": sorted(filtered_module_dependencies, key=lambda item: (item["source"], item["target"], item["source_file"], item["target_file"])),
        "reverse_dependencies": filtered_reverse_dependencies,
        "call_graph": filtered_call_graph,
        "entry_points": sorted(entries, key=lambda item: item["module"]),
        "leaves": sorted(leaves, key=lambda item: item["module"]),
        "orphans": sorted(orphans, key=lambda item: item["module"]),
        "hotspots": filtered_hotspots,
        "cycles": [
            cycle for cycle in graph.get("cycles", [])
            if all(module in relevant for module in cycle.get("modules", []))
        ],
    }


def create_app(root: Path | None = None) -> FastMCP:
    from .workspace import MiniWorkspace
    import os

    # Priority order: explicit root > env var > cwd
    if root is None:
        root = os.environ.get("MCP_WORKSPACE_ROOT")
        if root:
            root = Path(root)
        else:
            root = Path.cwd()

    resolved_root = root
    workspace = MiniWorkspace(resolved_root)
    working_set = WorkingSet()
    module_plan: dict[str, Any] = _load_module_plan(workspace.root)
    workspace._ignored_paths = _module_plan_ignored_paths(workspace, module_plan)
    tracker = WorkflowTracker.from_plan(module_plan)
    last_workflow_snapshot: dict[str, Any] | None = None
    last_workflow_next_tool: str | None = None

    # Restore focus directory from plan if previously set.
    _focus_rel: str | None = module_plan.get("_focus")
    if _focus_rel:
        _focus_abs = (workspace.root / _focus_rel).resolve()
        if _focus_abs.is_dir():
            workspace._focus_dir = _focus_abs

    def _active_focus() -> Path | None:
        return workspace._focus_dir

    def _initialize_workspace(new_root: Path) -> tuple[MiniWorkspace, dict[str, Any]]:
        """Initialize or reinitialize workspace with a new root."""
        new_workspace = MiniWorkspace(new_root)
        new_module_plan = _load_module_plan(new_workspace.root)
        new_workspace._ignored_paths = _module_plan_ignored_paths(new_workspace, new_module_plan)

        # Restore focus directory from plan if previously set
        _focus_rel: str | None = new_module_plan.get("_focus")
        if _focus_rel:
            _focus_abs = (new_workspace.root / _focus_rel).resolve()
            if _focus_abs.is_dir():
                new_workspace._focus_dir = _focus_abs

        return new_workspace, new_module_plan

    def _persist_module_plan() -> None:
        _save_module_plan(workspace.root, module_plan)

    def _with_workflow(result: dict[str, Any]) -> dict[str, Any]:
        nonlocal last_workflow_snapshot, last_workflow_next_tool
        workflow = tracker.snapshot()
        next_tool = result.get("next_suggested_tool")
        if next_tool is None:
            next_tool = workflow.get("next_tool")
        if workflow == last_workflow_snapshot and next_tool == last_workflow_next_tool:
            result["workflow"] = None
            if next_tool == last_workflow_next_tool:
                result.pop("next_suggested_tool", None)
            return result
        result["workflow"] = workflow
        if "next_suggested_tool" not in result and workflow.get("next_tool") is not None:
            result["next_suggested_tool"] = workflow.get("next_tool")
        last_workflow_snapshot = workflow
        last_workflow_next_tool = result.get("next_suggested_tool")
        return result

    def _suggest(result: dict[str, Any], next_tool: str | None) -> dict[str, Any]:
        if next_tool is not None:
            result["next_suggested_tool"] = next_tool
        return result

    def _apply_code_working_set(result: dict[str, Any], refetch_label: str) -> dict[str, Any]:
        def _apply_payload(payload: dict[str, Any], qname: str, field: str) -> dict[str, Any]:
            body = payload.get(field)
            if not isinstance(body, str) or not body:
                return payload
            mode, note = working_set.check(qname, body)
            if mode == "stub":
                updated = dict(payload)
                updated[field] = working_set.stub_text(qname, refetch=refetch_label)
                updated["stub"] = True
                return updated
            working_set.record(qname, body)
            if note:
                updated = dict(payload)
                updated["note"] = note
                return updated
            return payload

        if not isinstance(result, dict):
            return result
        if result.get("found") and isinstance(result.get("code"), str):
            qname = result.get("qname")
            if isinstance(qname, str):
                return _apply_payload(result, qname, "code")
        function = result.get("function")
        if isinstance(function, dict):
            qname = function.get("qname")
            if isinstance(qname, str):
                updated = _apply_payload(function, qname, "source")
                if updated is not function:
                    result = dict(result)
                    result["function"] = updated
                return result
        return result

    def _truncate_graph_payload(payload: dict[str, Any], limit: int) -> dict[str, Any]:
        result = dict(payload)
        truncated = bool(result.get("truncated", False))

        def _clip(value: Any) -> Any:
            nonlocal truncated
            if isinstance(value, list):
                if len(value) > limit:
                    truncated = True
                return value[:limit]
            return value

        for key in ("call_graph", "module_dependencies", "entry_points", "leaves", "orphans", "hotspots", "cycles", "sources", "targets", "source_files", "target_files"):
            if key in result:
                result[key] = _clip(result[key])

        call_graph = result.get("call_graph")
        if isinstance(call_graph, list):
            clipped_call_graph: list[dict[str, Any]] = []
            for item in call_graph:
                if not isinstance(item, dict):
                    clipped_call_graph.append(item)
                    continue
                clipped_item = dict(item)
                if isinstance(clipped_item.get("calls"), list):
                    if len(clipped_item["calls"]) > limit:
                        truncated = True
                    clipped_item["calls"] = clipped_item["calls"][:limit]
                clipped_call_graph.append(clipped_item)
            result["call_graph"] = clipped_call_graph

        module_nodes = result.get("module_nodes")
        if isinstance(module_nodes, dict):
            clipped_modules: dict[str, Any] = {}
            for module, meta in module_nodes.items():
                if not isinstance(meta, dict):
                    clipped_modules[module] = meta
                    continue
                clipped_meta = dict(meta)
                if isinstance(clipped_meta.get("imports"), list):
                    if len(clipped_meta["imports"]) > limit:
                        truncated = True
                    clipped_meta["imports"] = clipped_meta["imports"][:limit]
                clipped_modules[module] = clipped_meta
            result["module_nodes"] = clipped_modules

        reverse_dependencies = result.get("reverse_dependencies")
        if isinstance(reverse_dependencies, dict):
            clipped_reverse: dict[str, Any] = {}
            for module, edges in reverse_dependencies.items():
                if isinstance(edges, list):
                    if len(edges) > limit:
                        truncated = True
                    clipped_reverse[module] = edges[:limit]
                else:
                    clipped_reverse[module] = edges
            result["reverse_dependencies"] = clipped_reverse

        result["limit"] = limit
        result["truncated"] = truncated
        return result

    def _ro(result: dict[str, Any]) -> dict[str, Any]:
        """Return result with no workflow metadata — used by all read-only tools."""
        return result

    app = FastMCP(
        name="mini-coding-mcp",
        instructions=(
            "WORKSPACE ROOT: Call set_workspace_root(path) to work with a different project directory. "
            "The server starts with the current working directory. Use get_workspace_root() to check the current workspace. "
            "Use this server to insert code into destination files. "
            "Prefer the provided insert tools instead of editing files directly. "
            "Prefer stable symbol IDs like pkg.mod:Class.method. The default write response omits line numbers. "
            "STEP -1 — INIT: call init_project first to create pyproject.toml, requirements.txt, tests/conftest.py, "
            ".gitignore, and a src package before planning modules. "
            "STEP 0 — PLAN: before creating any file, call plan_module_structure with every file you intend to "
            "create, its purpose, and which other files it imports from. The server will validate names and flag "
            "dependency issues. Do not create any file until this call succeeds with no naming_issues. "
            "FILE NAMING: name files as verb_noun.py describing the module's primary action and subject "
            "(e.g. train_lora.py, load_captions.py, save_checkpoint.py). Names without an underscore are rejected. "
            "STEP LAST — RENAME then FINALIZE: after implementation is complete, if any file's name no longer "
            "matches its abstraction call rename_file first. Then call finalize_file_names to append a compact "
            "dep-tag (initials of all intra-workspace imports) to every filename, e.g. train_lora__lcsc.py. "
            "Use dry_run=True first to preview tags and catch remaining stubs before renaming. "
            "External packages are excluded from the tag. Use decode_file_tag to reverse a tagged name back to "
            "its dependency list. The server may also append a numeric category suffix after scaffolding, "
            "derived from imports and call sites. "
            "DOCSTRINGS: every function and class you insert must have a one-line docstring. The server returns "
            "missing_docstrings in the response — fix any names listed there before continuing. "
            "SCAFFOLD FIRST: when creating a new module, call scaffold_module first to reserve stubs for ALL "
            "foreseeable functions, then fill in each implementation one at a time. Pass with_tests=True to create "
            "a parallel tests/test_<name>.py file with failing stubs; the workflow exposes a dedicated 2_test "
            "phase for those generated test files. "
            "QUALITY: use lint_file(path) for file-level validation, lint(qname) for symbol-level validation, "
            "type_check(qname) for targeted post-edit diagnostics, and check_plan_complete before "
            "finalize_file_names to ensure no scaffold stubs remain in either the module code or generated tests. "
            "Use apply_patch(patch_text) to apply a unified diff and rescan the workspace when patch-based edits "
            "are more convenient than symbol-level updates. "
            "NEVER CREATE .MD FILES MANUALLY: never write README, description, or documentation files by hand. "
            "Call generate_description to produce description.md from the live AST — it includes module docstrings, "
            "signatures, and the full call graph automatically."
        ),
    )

    @app.tool(
        description=(
            "Insert code into a destination file. Prefer qname anchors like pkg.mod:Class.method. "
            "For Python files, imports are placed with import blocks, constants stay near the top, and "
            "definitions are inserted relative to the anchor symbol when provided. The default response uses "
            "stable symbol IDs and omits line numbers. "
            "Every inserted function or class MUST have a docstring. Check missing_docstrings in the response "
            "and add docstrings to any listed names before proceeding."
        )
    )
    def insert_code(destination_file: str, code: str, anchor: str | None = None, position: str = "auto") -> dict[str, Any]:
        destination_path = workspace._resolve_path(destination_file)
        result = workspace.insert_code(destination_file, code, anchor=anchor, position=position)
        if result.get("accepted", result.get("ok", True)):
            working_set.evict_many(result.get("edited", []))
            if destination_path.exists():
                final_path, suffix_info = _sync_numeric_suffix(workspace, module_plan, destination_path)
                result["destination_file"] = str(final_path)
                if suffix_info is not None:
                    result["suffix"] = suffix_info
                result["compile_check"] = _compile_python_file(final_path)
                result["ok"] = bool(result.get("ok", True)) and bool(result["compile_check"].get("ok", True))
                tracker.on_edit(final_path, workspace.root)
                _persist_module_plan()
        next_tool = "lint_file" if result.get("ok", True) else "patch_symbol"
        return _with_workflow(_suggest(result, next_tool))

    @app.tool(
        description=(
            "Step -1: create a runnable Python project layout before planning modules. "
            "Writes pyproject.toml, requirements.txt, .gitignore, tests/conftest.py, and a src package "
            "named after the project."
        )
    )
    def init_project(name: str, description: str, python_version: str, deps: list[str] | None = None) -> dict[str, Any]:
        result = workspace.init_project(name=name, description=description, python_version=python_version, deps=deps)
        tracker.on_plan([], False)
        _persist_module_plan()
        return _with_workflow(_suggest(result, "plan_module_structure"))

    @app.tool(
        description=(
            "Scope all relative path resolution and workspace iteration to a subdirectory. "
            "Call this when the target project lives inside a subfolder of the workspace root "
            "(e.g. 'test_login_backend'). After setting, bare filenames like 'hash_password.py' "
            "resolve to '<focus>/hash_password.py' and workspace_index/workspace_tree only show "
            "files inside that folder. Persisted in .mcp_plan.json. "
            "Use clear_workspace_focus to restore the full workspace scope."
        )
    )
    def set_workspace_focus(path: str) -> dict[str, Any]:
        try:
            focus = workspace._resolve_path(path)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        if not focus.is_dir():
            return {"ok": False, "reason": "not_a_directory", "path": str(focus)}
        workspace._focus_dir = focus
        rel = str(focus.relative_to(workspace.root))
        module_plan["_focus"] = rel
        _persist_module_plan()
        source_files = [str(p.relative_to(focus)) for p in workspace._iter_workspace_source_files()
                        if p.resolve().is_relative_to(focus)]
        return {
            "ok": True,
            "focus": str(focus),
            "source_files": source_files,
        }

    @app.tool(
        description="Remove the workspace focus set by set_workspace_focus, restoring full workspace scope."
    )
    def clear_workspace_focus() -> dict[str, Any]:
        workspace._focus_dir = None
        module_plan.pop("_focus", None)
        _persist_module_plan()
        return {"ok": True, "focus": None, "hint": "Workspace scope restored to root."}

    @app.tool(
        description=(
            "Dynamically change the workspace root to a different directory. This allows the MCP to work with "
            "different projects during a single session. The new root must be a valid directory. "
            "All subsequent tool calls will use this new root. Call with the target directory path."
        )
    )
    def set_workspace_root(path: str) -> dict[str, Any]:
        nonlocal workspace, module_plan, tracker
        previous_root = workspace.root  # Save the previous root before changing
        try:
            new_root = Path(path).resolve()
        except (ValueError, OSError) as exc:
            return {"ok": False, "reason": "path_error", "error": str(exc), "path": path}

        if not new_root.exists():
            return {"ok": False, "reason": "not_found", "path": str(new_root)}

        if not new_root.is_dir():
            return {"ok": False, "reason": "not_a_directory", "path": str(new_root)}

        try:
            # Initialize new workspace
            workspace, module_plan = _initialize_workspace(new_root)
            working_set.clear()
            tracker = WorkflowTracker.from_plan(module_plan)

            # Scan the new workspace
            workspace.symbol_index.ensure_scanned()

            source_files = [str(p.relative_to(new_root)) for p in workspace._iter_workspace_source_files()]
            return {
                "ok": True,
                "previous_root": str(previous_root),
                "new_root": str(new_root),
                "source_files_count": len(source_files),
                "source_files": source_files[:20] if source_files else [],
                "hint": f"Workspace root changed to {new_root}. All subsequent operations will use this directory.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "reason": "initialization_failed",
                "error": str(exc),
                "path": str(new_root),
            }

    @app.tool(
        description=(
            "Return information about the currently active workspace root, including source file count and focus directory if set."
        )
    )
    def get_workspace_root() -> dict[str, Any]:
        source_files = [str(p.relative_to(workspace.root)) for p in workspace._iter_workspace_source_files()]
        return {
            "ok": True,
            "root": str(workspace.root),
            "exists": workspace.root.exists(),
            "is_dir": workspace.root.is_dir(),
            "source_files_count": len(source_files),
            "focus_dir": str(workspace._focus_dir) if workspace._focus_dir else None,
            "has_plan": (workspace.root / ".mcp_plan.json").exists(),
        }

    @app.tool(
        description=(
            "Reserve space in a new or existing module by writing stub definitions for all planned symbols "
            "before implementing them. Each stub gets a docstring and raises NotImplementedError so the "
            "symbol index knows what the module will contain. The server may append a numeric category "
            "suffix to the filename based on the imports and function calls present after scaffolding. "
            "Call this first when creating any new file. "
            "stubs is a list of objects with fields: "
            "name (required), "
            "kind ('function' | 'async_function' | 'class', default 'function'), "
            "args (argument string, e.g. 'path: str, model: nn.Module'), "
            "returns (return annotation, e.g. 'None'), "
            "docstring (one-line description), "
            "bases (base classes for kind='class', e.g. 'Dataset'). "
            "Pass with_tests=True to also generate a parallel failing test stub file for public functions."
        )
    )
    def scaffold_module(
        destination_file: str,
        stubs: list[dict],
        language: str = "python",
        with_tests: bool = False,
    ) -> dict[str, Any]:
        initial_path = workspace._resolve_path(destination_file)
        file_name = initial_path.name
        plan_entry = module_plan.get(file_name)
        purpose = (plan_entry or {}).get("purpose", "").strip()
        depends_on = (plan_entry or {}).get("depends_on", [])
        language_key = (language or "python").strip().lower()

        if not purpose:
            return {
                "ok": False,
                "reason": "not_in_plan",
                "hint": f"Call plan_module_structure first with '{file_name}' and a purpose before scaffolding.",
            }

        all_edited: list[str] = []
        scaffolded: list[dict] = []
        failed: list[str] = []

        imports_note = f" Imports from: {', '.join(depends_on)}." if depends_on else ""
        if language_key in {"javascript", "typescript"}:
            header = f"/** {purpose}{imports_note} */\n"
        else:
            header = f'"""{purpose}{imports_note}"""\n'
        r = workspace.insert_code(destination_file, header)
        if not r.get("ok"):
            return {"ok": False, "reason": "header_write_failed", "detail": r}

        if language_key == "python":
            type_imports = _scaffold_typing_imports(stubs)
            if type_imports:
                import_result = workspace.add_import("typing", names=type_imports, path=destination_file)
                if not import_result.get("accepted", import_result.get("ok", False)):
                    return {"ok": False, "reason": "type_import_write_failed", "detail": import_result}

        for stub in stubs:
            kind = stub.get("kind", "function")
            name = stub.get("name", "").strip()
            if not name:
                continue
            args = stub.get("args", "")
            returns = stub.get("returns", "")
            docstring = stub.get("docstring", f"TODO: implement {name}.")

            if language_key == "python":
                if kind == "class":
                    bases = stub.get("bases", "")
                    header = f"class {name}({bases}):" if bases else f"class {name}:"
                    code = f'{header}\n    """{docstring}"""\n\n    def __init__(self) -> None:\n        raise NotImplementedError\n'
                else:
                    prefix = "async def" if kind == "async_function" else "def"
                    ret = f" -> {returns}" if returns else ""
                    code = f'{prefix} {name}({args}){ret}:\n    """{docstring}"""\n    raise NotImplementedError\n'
            else:
                if kind == "class":
                    bases = stub.get("bases", "")
                    extends = ""
                    if isinstance(bases, (list, tuple)):
                        base_list = [str(base).strip() for base in bases if str(base).strip()]
                        if base_list:
                            extends = f" extends {base_list[0]}"
                    elif str(bases).strip():
                        extends = f" extends {str(bases).strip()}"
                    code = (
                        f"export class {name}{extends} {{\n"
                        f"    constructor() {{\n"
                        f"        throw new Error(\"NotImplemented\");\n"
                        f"    }}\n"
                        f"}}\n"
                    )
                else:
                    async_prefix = "async " if kind == "async_function" else ""
                    return_type = ""
                    if language_key == "typescript":
                        if returns:
                            return_type = f": Promise<{returns}>"
                        elif kind == "async_function":
                            return_type = ": Promise<void>"
                    code = (
                        f"export {async_prefix}function {name}({args}){return_type} {{\n"
                        f"    /** {docstring} */\n"
                        f"    throw new Error(\"NotImplemented\");\n"
                        f"}}\n"
                    )

            r = workspace.insert_code(destination_file, code)
            all_edited.extend(r.get("edited", []))
            ok = r.get("ok", False)
            qname = r.get("edited", [None])[0]
            scaffolded.append({"name": name, "qname": qname, "ok": ok})
            if not ok:
                failed.append(name)

        working_set.evict_many(all_edited)
        final_path, suffix_info = _sync_numeric_suffix(workspace, module_plan, initial_path)
        if final_path.name != initial_path.name:
            tracker.on_rename(initial_path.name, final_path.name)
        if suffix_info and suffix_info.get("rename_failed") is not None:
            return _with_workflow(_suggest({
                "ok": False,
                "reason": "suffix_rename_failed",
                "detail": suffix_info["rename_failed"],
                "destination_file": str(final_path),
            }, "insert_code"))

        test_file = None
        test_result: dict[str, Any] | None = None
        if with_tests and language_key == "python":
            test_file = workspace.root / "tests" / f"test_{final_path.stem}.py"
            test_code = _test_module_code(final_path.stem, stubs, str(plan_entry.get("purpose", "")).strip())
            test_result = workspace.insert_code(test_file, test_code)
            if test_result.get("accepted", test_result.get("ok", True)):
                working_set.evict_many(test_result.get("edited", []))
                test_result["destination_file"] = str(test_file)
                test_result["compile_check"] = _compile_python_file(test_file)
                test_result["ok"] = bool(test_result.get("ok", True)) and bool(test_result["compile_check"].get("ok", True))
                tracker.on_test_generated(test_file, workspace.root)
                tracker.on_edit(test_file, workspace.root)
            else:
                test_result["destination_file"] = str(test_file)

        tracker.on_scaffold(final_path.name)
        _persist_module_plan()
        payload = {
            "ok": len(failed) == 0,
            "destination_file": str(final_path),
            "scaffolded": [s for s in scaffolded if s["ok"]],
            "failed": failed,
            "edited": all_edited,
            "suffix": suffix_info or {"value": 0, "modules": [], "calls": []},
        }
        if test_file is not None:
            payload["test_file"] = str(test_file)
        if test_result is not None:
            payload["test_result"] = test_result
            payload["ok"] = bool(payload["ok"]) and bool(test_result.get("ok", False))
        return _with_workflow(_suggest(payload, "insert_code"))

    @app.tool(
        description=(
            "Return the workspace-wide symbol index or the index for a single file. "
            "Default mode is an outline with qname, name, kind, and signature only; set verbose=True for full details."
        )
    )
    def workspace_index(path: str | None = None, verbose: bool = False, since_scan_id: int | None = None) -> dict[str, Any]:
        if path is None:
            return _ro({"scope": "workspace", **workspace.index_workspace(verbose=verbose, since_scan_id=since_scan_id)})
        resolved = workspace._resolve_path(path)
        return _ro({"scope": "file", "path": str(resolved), "symbols": workspace.index_file(path, verbose=verbose)})

    @app.tool(
        description=(
            "Search the workspace for a symbol by qualified name or name. "
            "Use detail=summary, code, calls, position, or children. "
            "When detail=code or detail=calls, symbols already served this session are replaced with a stub comment "
            "to save context. Pass force_show=True to override and receive the full body again."
        )
    )
    def get_symbol(
        qname: str,
        path: str | None = None,
        kind: str | None = None,
        detail: str = "summary",
        force_show: bool = False,
        verbose: bool = False,
    ) -> dict[str, Any]:
        detail = (detail or "summary").lower()
        if detail == "summary":
            return _ro(workspace.get_symbol_summary(qname=qname, path=path, kind=kind))
        if detail == "position":
            return _ro(workspace.get_symbol(qname=qname, path=path, kind=kind, projection="position"))
        if detail == "children":
            summary = workspace.get_symbol_summary(qname=qname, path=path, kind=kind)
            if not summary.get("found"):
                return _ro(summary)
            actual_qname = summary["symbol"]["qname"]
            return _ro({
                "found": True,
                "detail": "children",
                "qname": actual_qname,
                "children": workspace.children_of(actual_qname),
            })
        if detail == "calls":
            result = workspace.get_symbol_calls(name=qname, path=path, qualname=qname, verbose=verbose)
            return _ro(_apply_function_result_working_set(result, qname, "get_symbol(..., detail='calls')"))
        if detail != "code":
            return _ro({"found": False, "reason": "invalid_detail", "detail": detail})

        result = workspace.get_symbol(qname=qname, path=path, kind=kind, projection="code")
        if result.get("found") and not force_show:
            result = _apply_code_working_set(result, "get_symbol(..., detail='code')")
        if result.get("found"):
            result["detail"] = "code"
        return _ro(result)

    def _apply_function_result_working_set(result: dict[str, Any], qname: str, refetch_label: str) -> dict[str, Any]:
        function = result.get("function")
        if not isinstance(function, dict):
            return result
        qname = function.get("qname")
        source = function.get("source")
        if not isinstance(qname, str) or not isinstance(source, str) or not source:
            return result

        mode, note = working_set.check(qname, source)
        if mode == "stub":
            updated = dict(result)
            updated_function = dict(function)
            updated_function["source"] = working_set.stub_text(qname, refetch=refetch_label)
            updated["function"] = updated_function
            updated["stub"] = True
            return updated

        working_set.record(qname, source)
        if note:
            updated = dict(result)
            updated["note"] = note
            return updated
        return result

    @app.tool(
        description=(
            "Search the workspace for symbols by query text and optional kind filter. "
            "Supports AST filters directly: async_only, base, missing_return_annotation, and raises."
        )
    )
    def search(
        query: str,
        kind: str | None = None,
        async_only: bool | None = None,
        base: str | None = None,
        missing_return_annotation: bool | None = None,
        raises: str | None = None,
    ) -> dict[str, Any]:
        return _ro(workspace.search(
            query=query,
            kind=kind,
            async_only=async_only,
            base=base,
            missing_return_annotation=missing_return_annotation,
            raises=raises,
        ))

    @app.tool(
        description=(
            "Fetch several symbols in one round trip. Applies working-set stubs to repeated code bodies."
        )
    )
    def get_symbols(qnames: list[str], force_show: bool = False) -> dict[str, Any]:
        result = workspace.get_symbols(qnames=qnames, projection="code")
        symbols: list[dict[str, Any]] = []
        for item in result.get("symbols", []):
            if force_show:
                symbols.append(item)
            else:
                symbols.append(_apply_code_working_set(item, "get_symbols(..., projection='code')"))
        result["symbols"] = symbols
        return _ro(result)

    @app.tool(
        description=(
            "Return a size hint for a file or symbol before reading it. "
            "The result includes line count, character count, and an estimated token count."
        )
    )
    def size_hint(qname_or_path: str) -> dict[str, Any]:
        return _ro(workspace.size_hint(qname_or_path))

    @app.tool(
        description=(
            "Return the public API of a module: public top-level classes, functions, and variables "
            "with signatures and docstrings, but without bodies."
        )
    )
    def get_module_api(path: str) -> dict[str, Any]:
        return _ro(workspace.get_module_api(path))

    @app.tool(
        description=(
            "Search for AST shapes instead of text. "
            "Supports filters such as async_only, base, missing_return_annotation, raises, calls, decorator, and parameter_count_gt."
        )
    )
    def search_ast(
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
        return _ro(workspace.search_ast(
            files=files,
            roots=roots,
            kind=kind,
            async_only=async_only,
            base=base,
            missing_return_annotation=missing_return_annotation,
            raises=raises,
            calls=calls,
            decorator=decorator,
            parameter_count_gt=parameter_count_gt,
        ))

    @app.tool(
        description=(
            "Return a symbol together with the definitions of everything it directly calls within the workspace."
        )
    )
    def get_symbol_with_deps(qname: str, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        result = workspace.get_symbol_with_deps(qname=qname, files=files, roots=roots)
        if isinstance(result.get("symbol"), dict):
            result["symbol"] = _apply_code_working_set(result["symbol"], "get_symbol_with_deps(..., symbol)")
        for dep in result.get("dependencies", []):
            if isinstance(dep, dict) and isinstance(dep.get("definition"), dict):
                dep["definition"] = _apply_code_working_set(dep["definition"], "get_symbol_with_deps(..., dependency)")
        return _with_workflow(result)

    @app.tool(
        description=(
            "Find the shortest call path from one symbol to another. "
            "Returns a compact qname/signature chain."
        )
    )
    def find_call_path(from_qname: str, to_qname: str) -> dict[str, Any]:
        return _with_workflow(workspace.find_call_path(from_qname=from_qname, to_qname=to_qname))

    @app.tool(
        description=(
            "Return the direct caller/callee neighborhood for a symbol, optionally expanded by hops. "
            "The response is compact: qname/signature pairs only."
        )
    )
    def neighbors(qname: str, hops: int = 1) -> dict[str, Any]:
        return _with_workflow(workspace.neighbors(qname=qname, hops=hops))

    @app.tool(
        description=(
            "Return which workspace files consume a module and which imported names they use."
        )
    )
    def get_module_consumers(path: str) -> dict[str, Any]:
        return _with_workflow(workspace.get_module_consumers(path))

    @app.tool(
        description=(
            "Analyze the impact of changing a symbol. Returns callers and signature-mismatch flags, "
            "optionally comparing against a proposed new signature."
        )
    )
    def impact_of_change(
        qname: str,
        new_signature: str | None = None,
        files: list[str] | None = None,
        roots: list[str] | None = None,
    ) -> dict[str, Any]:
        return _with_workflow(workspace.impact_of_change(qname=qname, new_signature=new_signature, files=files, roots=roots))

    @app.tool(
        description=(
            "Return the inheritance chain for a class, including each ancestor's public methods."
        )
    )
    def class_hierarchy(qname: str) -> dict[str, Any]:
        return _with_workflow(workspace.class_hierarchy(qname))

    @app.tool(
        description=(
            "Find functions, methods, and classes defined in the scoped workspace files that are never called anywhere."
        )
    )
    def dead_symbols(files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        result = workspace.dead_symbols(files=files, roots=roots)
        if files:
            tracker.on_quality_check([Path(f).name for f in files])
            _persist_module_plan()
        return _with_workflow(_suggest(result, "lint_file"))

    @app.tool(
        description=(
            "Return direct and transitive exception surfaces for a symbol, including exceptions raised by callees."
        )
    )
    def exception_surface(qname: str, files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        result = workspace.exception_surface(qname=qname, files=files, roots=roots)
        if isinstance(result.get("callees"), dict):
            for item in result["callees"].values():
                if isinstance(item, dict) and isinstance(item.get("definition"), dict):
                    item["definition"] = _apply_code_working_set(item["definition"], "exception_surface(..., callee)")
        return _with_workflow(result)

    @app.tool(
        description=(
            "List functions and methods missing parameter annotations or return annotations in the scoped workspace."
        )
    )
    def missing_annotations(files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        result = workspace.missing_annotations(files=files, roots=roots)
        if files:
            tracker.on_quality_check([Path(f).name for f in files])
            _persist_module_plan()
        return _with_workflow(_suggest(result, "lint_file"))

    @app.tool(description="Return current diagnostics filtered to a single stable symbol ID.")
    def lint(qname: str) -> dict[str, Any]:
        result = workspace.lint(qname)
        tracker.on_quality_check_qname(qname)
        _persist_module_plan()
        next_tool = "patch_symbol" if result.get("count", 0) else "check_plan_complete"
        return _with_workflow(_suggest(result, next_tool))

    @app.tool(
        description=(
            "Lint every symbol in a file after enumerating it through the index. "
            "This is the file-level shortcut for per-symbol linting."
        )
    )
    def lint_file(path: str) -> dict[str, Any]:
        result = workspace.lint_file(path)
        tracker.on_quality_check([Path(path).name])
        _persist_module_plan()
        next_tool = "patch_symbol" if result.get("count", 0) else "check_plan_complete"
        return _with_workflow(_suggest(result, next_tool))

    @app.tool(
        description=(
            "Run a differential diagnostic refresh scoped to the transitive callers of a symbol. "
            "It surfaces newly introduced type errors after an edit."
        )
    )
    def type_check(qname: str) -> dict[str, Any]:
        result = workspace.type_check(qname)
        tracker.on_quality_check_qname(qname)
        _persist_module_plan()
        next_tool = "patch_symbol" if result.get("introduced") else "lint_file"
        return _with_workflow(_suggest(result, next_tool))

    @app.tool(
        description=(
            "Verify that the plan is fully implemented before finalizing file names. "
            "Returns every planned symbol still raising NotImplementedError, plus any planned files not yet on disk."
        )
    )
    def check_plan_complete(files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        planned_files = files if files is not None else _module_plan_file_names(module_plan)
        stubbed = workspace.planned_stub_symbols(files=planned_files, roots=roots)
        missing_files: list[dict[str, Any]] = []
        for file_name in planned_files:
            try:
                resolved = workspace._resolve_path(file_name)
            except ValueError:
                missing_files.append({"file": file_name, "reason": "path_error"})
                continue
            if not resolved.exists():
                missing_files.append({"file": file_name, "reason": "not_found", "path": str(resolved)})
        result = {
            "ok": not missing_files and stubbed["count"] == 0,
            "planned_files": planned_files,
            "missing_files": missing_files,
            "count": stubbed["count"],
            "matches": stubbed["matches"],
        }
        tracker.on_quality_check(planned_files)
        _persist_module_plan()
        return _with_workflow(_suggest(result, "finalize_file_names" if result["ok"] else "patch_symbol"))

    @app.tool(
        description=(
            "Find where a symbol is used. Set kind=references, callers, or all."
            " Use limit to cap the returned list sizes and avoid large payloads."
        )
    )
    def find_usages(qname: str, kind: str = "all", limit: int = 20, include_source: bool = False) -> dict[str, Any]:
        kind = kind.lower()
        if kind not in {"references", "callers", "all"}:
            return _with_workflow({"found": False, "reason": "invalid_kind", "kind": kind})
        payload: dict[str, Any] = {"qname": qname, "kind": kind, "limit": limit, "include_source": include_source}
        if kind in {"references", "all"}:
            name = qname.split(":")[-1].split(".")[-1]
            payload["references"] = workspace.find_references(name=name, limit=limit, include_source=include_source)
        if kind in {"callers", "all"}:
            payload["callers"] = workspace.find_callers(qname=qname, limit=limit)
        payload["truncated"] = any(
            isinstance(payload.get(key), dict) and bool(payload[key].get("truncated"))
            for key in ("references", "callers")
        )
        return _with_workflow(payload)

    @app.tool(description="Replace a symbol in place using its qualified name or name.")
    def replace_symbol(qname: str, new_source: str, path: str | None = None) -> dict[str, Any]:
        result = workspace.replace_symbol(qname=qname, new_source=new_source, path=path)
        if result.get("accepted", result.get("ok", True)):
            working_set.evict(qname)
            working_set.evict_many(result.get("edited", []))
            destination = result.get("destination_file")
            if isinstance(destination, str):
                try:
                    destination_path = workspace._resolve_path(destination)
                    final_path, suffix_info = _resync_numeric_suffix(workspace, module_plan, destination_path)
                    result["destination_file"] = str(final_path)
                    if suffix_info is not None:
                        result["suffix"] = suffix_info
                    result["compile_check"] = _compile_python_file(final_path)
                    tracker.on_edit(final_path, workspace.root)
                    _persist_module_plan()
                except ValueError:
                    result["compile_check"] = {
                        "ok": False,
                        "compiled": False,
                        "path": destination,
                        "error": "path_resolution_failed",
                    }
            else:
                result["compile_check"] = {
                    "ok": False,
                    "compiled": False,
                    "path": None,
                    "error": "destination_missing",
                }
            if isinstance(result.get("compile_check"), dict):
                result["ok"] = bool(result.get("ok", True)) and bool(result["compile_check"].get("ok", True))
        return _with_workflow(_suggest(result, "lint_file"))

    @app.tool(
        description=(
            "Patch a sub-range of a symbol instead of replacing the entire body. "
            "Supports either a line-range patch or an old_lines/new_lines text replacement inside the symbol."
        )
    )
    def patch_symbol(
        qname: str,
        new_source: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        old_lines: list[str] | None = None,
        new_lines: list[str] | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        result = workspace.patch_symbol(
            qname=qname,
            new_source=new_source,
            start_line=start_line,
            end_line=end_line,
            old_lines=old_lines,
            new_lines=new_lines,
            path=path,
        )
        if result.get("accepted", result.get("ok", True)):
            working_set.evict(qname)
            working_set.evict_many(result.get("edited", []))
            destination = result.get("destination_file")
            if isinstance(destination, str):
                try:
                    destination_path = workspace._resolve_path(destination)
                    final_path, suffix_info = _resync_numeric_suffix(workspace, module_plan, destination_path)
                    result["destination_file"] = str(final_path)
                    if suffix_info is not None:
                        result["suffix"] = suffix_info
                    result["compile_check"] = _compile_python_file(final_path)
                    result["ok"] = bool(result.get("ok", True)) and bool(result["compile_check"].get("ok", True))
                    tracker.on_edit(final_path, workspace.root)
                    _persist_module_plan()
                except ValueError:
                    result["compile_check"] = {
                        "ok": False,
                        "compiled": False,
                        "path": destination,
                        "error": "path_resolution_failed",
                    }
        return _with_workflow(result)

    @app.tool(
        description=(
            "Apply a unified diff via native patch or Python fallback, then rescan the workspace."
        )
    )
    def apply_patch(patch_text: str) -> dict[str, Any]:
        result = workspace.apply_patch(patch_text)
        if result.get("accepted"):
            working_set.clear()
            _persist_module_plan()
        return _with_workflow(result)

    @app.tool(description="Add an import with deduplication.")
    def add_import(module: str, names: list[str] | None = None, path: str | None = None) -> dict[str, Any]:
        result = workspace.add_import(module=module, names=names, path=path)
        if result.get("accepted", result.get("ok", True)):
            working_set.evict_many(result.get("edited", []))
            destination = result.get("destination_file")
            if isinstance(destination, str):
                try:
                    destination_path = workspace._resolve_path(destination)
                    final_path, suffix_info = _resync_numeric_suffix(workspace, module_plan, destination_path)
                    result["destination_file"] = str(final_path)
                    if suffix_info is not None:
                        result["suffix"] = suffix_info
                    result["compile_check"] = _compile_python_file(final_path)
                    result["ok"] = bool(result.get("ok", True)) and bool(result["compile_check"].get("ok", True))
                except ValueError:
                    pass
        return _with_workflow(_suggest(result, "lint_file"))

    @app.tool(description="Rename a symbol across the workspace in a best-effort way.")
    def rename_symbol(old: str, new: str) -> dict[str, Any]:
        result = workspace.rename_symbol(old=old, new=new)
        if result.get("accepted", result.get("ok", True)):
            working_set.evict(old)
            if isinstance(result.get("new"), str):
                working_set.evict(result["new"])
            working_set.evict_many(result.get("edited", []))
        return _with_workflow(_suggest(result, "lint_file"))

    @app.tool(
        description=(
            "Declare the full set of files you intend to create, their purposes, and their import dependencies "
            "before writing any code. Call this as STEP 0. "
            "Each entry in 'files' must have: name (verb_noun.py), purpose (one sentence), "
            "depends_on (list of other names in this plan that this file imports from). "
            "Returns naming_issues for files violating the verb_noun convention and "
            "missing_deps for depends_on references not present in the plan. "
            "Do not create any file until both lists are empty."
        )
    )
    def plan_module_structure(files: list[dict]) -> dict[str, Any]:
        _GENERIC_STEMS = {
            # single-concern nouns that say nothing about what the file *does*
            "train_model", "run_model", "load_model", "base_model", "test_model",
            "load_data", "save_data", "data_loader", "data_utils", "data_handler",
            "run_script", "main_script", "run_training", "train_script",
            "my_module", "new_module", "helper_module", "utils_module",
        }
        metadata = {key: value for key, value in module_plan.items() if key.startswith("_")}
        module_plan.clear()
        for key, value in metadata.items():
            if key == "_ignored":
                module_plan[key] = set(value) if isinstance(value, (set, list, tuple)) else set()
            else:
                module_plan[key] = value
        for entry in files:
            name = entry.get("name", "").strip()
            if name:
                module_plan[name] = {
                    "purpose": entry.get("purpose", ""),
                    "depends_on": entry.get("depends_on", []),
                }

        naming_issues: list[dict] = []
        missing_deps: list[dict] = []
        for name, info in module_plan.items():
            if name.startswith("_"):
                continue
            stem = Path(name).stem
            if not _re.match(r"^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$", stem):
                naming_issues.append({"file": name, "reason": "not_verb_noun", "hint": "Use verb_noun.py form, e.g. train_lora.py"})
            elif stem in _GENERIC_STEMS:
                naming_issues.append({"file": name, "reason": "too_generic", "hint": f"Replace generic noun in '{stem}' with a domain-specific term (e.g. train_lora not train_model)"})
            elif not info.get("purpose", "").strip():
                naming_issues.append({"file": name, "reason": "missing_purpose", "hint": "Provide a one-sentence purpose so the module header can be written"})
            for dep in info["depends_on"]:
                if dep not in module_plan:
                    missing_deps.append({"file": name, "missing": dep})

        dependency_graph = [
            {"file": name, "purpose": info["purpose"], "imports_from": info["depends_on"]}
            for name, info in module_plan.items()
            if not name.startswith("_")
        ]
        ok = not naming_issues and not missing_deps
        tracker.on_plan(_module_plan_file_names(module_plan), ok)
        _persist_module_plan()
        return _with_workflow(_suggest({
            "ok": ok,
            "naming_issues": naming_issues,
            "missing_deps": missing_deps,
            "dependency_graph": dependency_graph,
            "hint": (
                "Fix naming_issues before creating any file."
                if naming_issues else
                "Plan accepted. Call scaffold_module for each file next."
            ),
        }, "scaffold_module"))

    @app.tool(
        description=(
            "Rename a file and rewrite every import that references it across the workspace. "
            "Use this as the LAST step if a file's name no longer matches its abstraction after implementation. "
            "new_path must follow the verb_noun.py convention."
        )
    )
    def rename_file(old_path: str, new_path: str) -> dict[str, Any]:
        result = workspace.rename_file(old_path, new_path)
        if result.get("accepted"):
            working_set.clear()
            old_name = Path(old_path).name
            new_name = Path(new_path).name
            if old_name in module_plan:
                module_plan[new_name] = module_plan.pop(old_name)
            _sync_ignored_path_rename(workspace, module_plan, workspace._resolve_path(old_path), workspace._resolve_path(new_path))
            tracker.on_rename(old_name, new_name)
            _persist_module_plan()
        return _with_workflow(_suggest(result, "finalize_file_names"))

    @app.tool(
        description=(
            "STEP LAST (after rename_file) — append a compact dep-tag to every finalized file. "
            "The tag is the sorted initials of all intra-workspace modules the file imports from "
            "(external packages are excluded). Example: train_lora.py importing load_captions and "
            "save_checkpoint becomes train_lora__lcsc.py. "
            "Pass files=None to process all planned files, or supply a specific list of filenames. "
            "Use decode_file_tag to reverse any tagged name back to its full dependency list. "
            "Pass dry_run=True to preview the tags without writing any files."
        )
    )
    def finalize_file_names(files: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        targets = files if files is not None else _module_plan_file_names(module_plan)
        results: list[dict[str, Any]] = []

        for file_name in targets:
            try:
                old_path = workspace._resolve_path(file_name)
            except ValueError:
                results.append({"file": file_name, "skipped": True, "reason": "path_error"})
                continue

            if not old_path.exists():
                results.append({"file": file_name, "skipped": True, "reason": "not_found"})
                continue

            pending = workspace.planned_stub_symbols(files=[file_name], roots=None)
            local_imports = _compute_local_imports(old_path, workspace.root)
            if pending["matches"] and not dry_run:
                results.append({
                    "file": file_name,
                    "tag": "",
                    "renamed": False,
                    "reason": "stubs_remaining",
                    "pending_stubs": pending["matches"],
                    "local_imports": local_imports,
                    "dry_run": False,
                })
                continue
            if not local_imports:
                results.append({
                    "file": file_name,
                    "tag": "",
                    "renamed": False,
                    "local_imports": [],
                    "pending_stubs": pending["matches"],
                    "dry_run": dry_run,
                })
                continue

            tag = "".join(_module_initials(stem) for stem in local_imports)
            stem = old_path.stem
            base_stem, _ = _split_dep_tag(stem)
            new_stem = f"{base_stem}{_DEP_TAG_MARKER}{tag}"

            if new_stem == stem:
                # Tag unchanged — just record it
                if file_name in module_plan and not dry_run:
                    module_plan[file_name]["dep_tag"] = tag
                    module_plan[file_name]["local_imports"] = local_imports
                    _persist_module_plan()
                results.append({
                    "file": file_name,
                    "tag": tag,
                    "renamed": False,
                    "local_imports": local_imports,
                    "pending_stubs": pending["matches"],
                    "dry_run": dry_run,
                })
                continue

            new_name = new_stem + old_path.suffix
            new_path_str = str(old_path.parent / new_name)
            if dry_run:
                results.append({
                    "file": file_name,
                    "new_name": new_name,
                    "tag": tag,
                    "renamed": False,
                    "would_rename": True,
                    "local_imports": local_imports,
                    "pending_stubs": pending["matches"],
                    "dry_run": True,
                })
            else:
                rename_result = workspace.rename_file(old_path, new_path_str)

                if rename_result.get("accepted"):
                    working_set.clear()
                    if file_name in module_plan:
                        entry = module_plan.pop(file_name)
                        entry["dep_tag"] = tag
                        entry["local_imports"] = local_imports
                        module_plan[new_name] = entry
                    _sync_ignored_path_rename(workspace, module_plan, old_path, Path(new_path_str))
                    _persist_module_plan()
                    results.append({
                        "file": file_name,
                        "new_name": new_name,
                        "tag": tag,
                        "renamed": True,
                        "local_imports": local_imports,
                        "pending_stubs": pending["matches"],
                        "dry_run": False,
                    })
                else:
                    results.append({
                        "file": file_name,
                        "tag": tag,
                        "renamed": False,
                        "reason": "rename_failed",
                        "detail": rename_result,
                        "local_imports": local_imports,
                        "pending_stubs": pending["matches"],
                        "dry_run": False,
                    })

        tracker.on_finalize(results)
        _persist_module_plan()
        ok = all(
            r.get("renamed") or r.get("dry_run") or not r.get("local_imports") or r.get("skipped")
            for r in results
        )
        return _with_workflow(_suggest({
            "ok": ok and all(not r.get("pending_stubs") for r in results),
            "results": results,
        }, None if dry_run else "done"))

    @app.tool(
        description=(
            "Decode a dep-tagged filename (e.g. train_lora__lcsc.py) back to its local dependency list. "
            "Checks the plan registry first; falls back to re-deriving from the actual file's imports. "
            "The tag is the sorted initials of each intra-workspace imported module stem."
        )
    )
    def decode_file_tag(encoded_name: str) -> dict[str, Any]:
        name = Path(encoded_name).name
        stem = Path(name).stem
        _, parsed_tag = _split_dep_tag(stem)

        # Registry path — fastest and most accurate
        if name in module_plan and "local_imports" in module_plan[name]:
            return _with_workflow({
                "encoded_name": encoded_name,
                "tag": module_plan[name].get("dep_tag", ""),
                "local_imports": module_plan[name]["local_imports"],
                "source": "registry",
            })

        # Re-derive from the actual file on disk
        try:
            file_path = workspace._resolve_path(encoded_name)
            if file_path.exists():
                local_imports = _compute_local_imports(file_path, workspace.root)
                tag = parsed_tag
                return _with_workflow({
                    "encoded_name": encoded_name,
                    "tag": tag,
                    "local_imports": local_imports,
                    "source": "derived",
                })
        except (ValueError, OSError):
            pass

        # Tag-only fallback — file not found
        tag = parsed_tag
        return _with_workflow({
            "encoded_name": encoded_name,
            "tag": tag,
            "local_imports": [],
            "source": "tag_only",
            "hint": f"File not found in workspace; tag '{tag}' encodes module initials.",
        })

    @app.tool(
        description=(
            "Infer the numeric suffix flags for one Python file by reading its source. "
            "Returns both the numeric bitmask and the active category names."
        )
    )
    def file_suffix(path: str) -> dict[str, Any]:
        file_path = workspace._resolve_path(path)
        flags = flags_from_filename(file_path.name)
        if int(flags) > 0:
            return _ro({
                "filename": file_path.name,
                "flags": int(flags),
                "categories": category_names(flags),
                "inferred_from": "filename",
            })

        source = workspace._read_text(file_path)
        adapter = adapter_for(file_path)
        if adapter is not None:
            tree = adapter.parse(source)
            if tree is not None:
                modules = [edge.target for edge in adapter.extract_imports(tree, file_path)]
                calls = [call.callee for call in adapter.extract_calls(tree, file_path, source)]
                result = infer_file_suffix_result(source, modules=modules, calls=calls)
            else:
                result = infer_file_suffix_result(source)
        else:
            result = infer_file_suffix_result(source)
        return _ro({
            "filename": file_path.name,
            "flags": suffix_number(result.flags),
            "categories": category_names(result.flags),
            "inferred_from": "source",
        })

    @app.tool(
        description=(
            "Auto-create a regex rule from target strings using a trie-based generator. "
            "The result includes the anchored pattern and a compile check."
        )
    )
    def generate_regex_rule(strings: list[str], anchored: bool = True, generalize: bool = False) -> dict[str, Any]:
        result = _generate_regex_rule(strings, anchored=anchored, generalize=generalize)
        next_tool = "insert_code" if result.get("ok") else "patch_symbol"
        return _with_workflow(_suggest(result, next_tool))

    @app.tool(
        description=(
            "Find all Python files whose inferred suffix flags include a given category, "
            "such as NETWORK_HTTP_API_CALLS. Pass files=[...] to scope the search explicitly; "
            "when omitted, the server prefers planned files and falls back to the full workspace."
        )
    )
    def find_files_by_flag(flag: str, files: list[str] | None = None) -> dict[str, Any]:
        wanted = category_from_name(flag)
        planned_paths = _planned_workspace_files(workspace, module_plan)
        if files is not None:
            target_files = [workspace._resolve_path(path) for path in files]
        elif planned_paths:
            target_files = planned_paths
        else:
            target_files = workspace._iter_workspace_source_files()

        matches: list[dict[str, Any]] = []
        for file_path in target_files:
            file_flags = flags_from_filename(file_path.name)
            if file_flags & wanted:
                matches.append(
                    {
                        "path": str(file_path),
                        "filename": file_path.name,
                        "flags": int(file_flags),
                        "categories": category_names(file_flags),
                        "suffix": _decode_filename_suffix(file_path.name),
                    }
                )

        return _with_workflow({
            "flag": flag,
            "value": suffix_number(wanted),
            "matches": matches,
            "count": len(matches),
        })

    @app.tool(
        description=(
            "Ignore one or more files so they are excluded from workspace iteration, call graphs, indexing, "
            "and other source-scoped tools. Paths are resolved against the workspace root and persisted in "
            ".mcp_plan.json."
        )
    )
    def ignore_files(files: list[str]) -> dict[str, Any]:
        ignored = _module_plan_ignored_paths(workspace, module_plan)
        confirmed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in files:
            try:
                path = workspace._resolve_path(item)
            except ValueError:
                skipped.append({"file": item, "reason": "path_error"})
                continue
            ignored.add(path)
            file_flags = flags_from_filename(path.name)
            confirmed.append(
                {
                    "file": item,
                    "path": str(path),
                    "filename": path.name,
                    "flags": int(file_flags),
                    "categories": category_names(file_flags),
                    "suffix": _decode_filename_suffix(path.name),
                }
            )
        _set_module_plan_ignored_paths(workspace, module_plan, ignored)
        _persist_module_plan()
        return _with_workflow({
            "ok": True,
            "confirmed": confirmed,
            "skipped": skipped,
            "count": len(confirmed),
        })

    @app.tool(
        description=(
            "Remove one or more files from the ignore list so workspace iteration includes them again. "
            "Paths are resolved against the workspace root and the updated list is persisted."
        )
    )
    def unignore_files(files: list[str]) -> dict[str, Any]:
        ignored = _module_plan_ignored_paths(workspace, module_plan)
        removed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in files:
            try:
                path = workspace._resolve_path(item)
            except ValueError:
                skipped.append({"file": item, "reason": "path_error"})
                continue
            if path in ignored:
                ignored.remove(path)
                removed.append({"file": item, "path": str(path), "filename": path.name})
            else:
                skipped.append({"file": item, "reason": "not_ignored", "path": str(path)})
        _set_module_plan_ignored_paths(workspace, module_plan, ignored)
        _persist_module_plan()
        return _with_workflow({
            "ok": True,
            "removed": removed,
            "skipped": skipped,
            "count": len(removed),
        })

    @app.tool(
        description=(
            "List the current ignore set with filename suffix metadata only. This does not read file contents; "
            "it decodes the numeric suffix from the filename and returns the active category names for audit."
        )
    )
    def list_ignored_files() -> dict[str, Any]:
        ignored = sorted(_module_plan_ignored_paths(workspace, module_plan), key=lambda path: str(path))
        files: list[dict[str, Any]] = []
        for path in ignored:
            flags = flags_from_filename(path.name)
            files.append(
                {
                    "path": str(path),
                    "filename": path.name,
                    "exists": path.exists(),
                    "flags": int(flags),
                    "categories": category_names(flags),
                    "suffix": _decode_filename_suffix(path.name),
                }
            )
        return _with_workflow({
            "ok": True,
            "count": len(files),
            "files": files,
        })

    @app.tool(
        description=(
            "Return a function call graph showing which function calls which other function. "
            "Pass files=[...] to scope it explicitly; when omitted, the server prefers planned files and "
            "falls back to the full workspace. The result also includes module_dependencies, reverse_dependencies, "
            "entry_points, leaves, hotspots, and cycles. Use limit to cap large lists. "
            "Set include_unresolved=True to surface unresolved call edges."
        )
    )
    def get_call_graph(
        files: list[str] | None = None,
        roots: list[str] | None = None,
        hotspot_threshold: int = 3,
        limit: int = 20,
        include_sources: bool = False,
        include_unresolved: bool = False,
    ) -> dict[str, Any]:
        target_files = _workspace_source_files(workspace, module_plan, files=files, roots=roots)
        graph = build_call_graph(
            workspace.root,
            files=target_files,
            hotspot_threshold=hotspot_threshold,
            include_sources=include_sources,
            include_unresolved=include_unresolved,
        )
        return _with_workflow(_truncate_graph_payload(graph, limit))

    @app.tool(
        description=(
            "Return a compact context bundle for one symbol: signature, docstring, direct callers, and direct callees."
        )
    )
    def get_relevant_context(
        qname: str,
        files: list[str] | None = None,
        roots: list[str] | None = None,
    ) -> dict[str, Any]:
        target_files = _workspace_source_files(workspace, module_plan, files=files, roots=roots)
        target_file_set = {str(path.resolve()) for path in target_files}
        target_summary = workspace.get_symbol_summary(qname)
        if not target_summary.get("found"):
            return _with_workflow(target_summary)
        symbol = target_summary.get("symbol", {})
        actual_qname = symbol.get("qname") if isinstance(symbol, dict) else qname
        symbol_summary = target_summary.get("summary", {})
        callers: list[dict[str, Any]] = []
        callees: list[dict[str, Any]] = []
        caller_summary = workspace.find_callers(actual_qname)
        for item in caller_summary.get("callers", []):
            caller_qname = item.get("caller")
            if isinstance(caller_qname, str):
                caller_symbol = workspace.get_symbol_summary(caller_qname)
                caller_file = caller_symbol.get("symbol", {}).get("file") if caller_symbol.get("found") else None
                if target_file_set and caller_file not in target_file_set:
                    continue
                callers.append({"qname": caller_qname})

        symbol_calls = workspace.get_symbol_calls(actual_qname)
        callee_seen: set[str] = set()
        for call in symbol_calls.get("calls", []):
            resolved = call.get("resolved")
            if not isinstance(resolved, dict):
                continue
            resolved_qname = resolved.get("qname")
            if not isinstance(resolved_qname, str) or resolved_qname in callee_seen:
                continue
            callee_seen.add(resolved_qname)
            callee_symbol = workspace.get_symbol_summary(resolved_qname)
            if callee_symbol.get("found"):
                callee_file = callee_symbol.get("symbol", {}).get("file")
                if target_file_set and callee_file not in target_file_set:
                    continue
                callee = callee_symbol.get("symbol", {})
                callee_summary = callee_symbol.get("summary", {})
                callees.append(
                    {
                        "qname": resolved_qname,
                        "kind": callee.get("kind"),
                        "signature": callee_summary.get("signature") or callee.get("signature"),
                    }
                )
        target_file: str | None = None
        try:
            resolved_target = workspace._resolve_path(qname)
        except ValueError:
            resolved_target = None
        if resolved_target is not None and resolved_target.exists():
            target_file = str(resolved_target)
        elif isinstance(symbol.get("file"), str):
            target_file = symbol["file"]
        context = {
            "found": True,
            "qname": actual_qname,
            "symbol": {
                "qname": symbol.get("qname"),
                "kind": symbol.get("kind"),
                "signature": symbol_summary.get("signature"),
                "docstring": symbol_summary.get("docstring"),
                "file": symbol.get("file"),
                "line_start": symbol.get("line_start"),
                "line_end": symbol.get("line_end"),
            },
            "callers": callers,
            "caller_count": len(callers),
            "callees": callees,
            "callee_count": len(callees),
            "target_file": target_file,
        }
        return _with_workflow(context)

    @app.tool(
        description=(
            "Run static analysis and return functions that are considered complex. "
            "Pass files=[...] to scope it explicitly; when omitted, the server prefers planned files and "
            "falls back to the full workspace. "
            "A function is flagged when it exceeds the configured loop, branch, line, or repetition limits. "
            "Use this as an internal splitting signal."
        )
    )
    def analyze_static_code(files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        target_files = _workspace_python_files(workspace, module_plan, files=files, roots=roots)
        result = analyze_workspace(workspace.root, files=target_files)
        if files:
            tracker.on_quality_check([Path(f).name for f in files])
            _persist_module_plan()
        return _with_workflow(_suggest(result, "check_plan_complete" if not result.get("complex_functions") else "patch_symbol"))

    @app.tool(
        description=(
            "Auto-generate description.md for the workspace from the live AST. "
            "Includes module docstrings, every function/class signature and docstring, "
            "and what each function calls and is called by. "
            "Always call this instead of writing .md files manually. "
            "Re-call after implementing stubs to keep the description current."
        )
    )
    def generate_description(files: list[str] | None = None, roots: list[str] | None = None) -> dict[str, Any]:
        from .stable_index import qname_for_path as _qname

        target_files = _workspace_source_files(workspace, module_plan, files=files, roots=roots)

        graph = build_call_graph(workspace.root, files=target_files)
        callers_map: dict[str, list[str]] = {}
        calls_map: dict[str, list[str]] = {}
        for item in graph["call_graph"]:
            caller = item["caller"].get("qname") or item["caller"].get("qualname")
            if caller:
                calls_map.setdefault(caller, [])
            for call in item["calls"]:
                resolved = call.get("resolved")
                if resolved and caller:
                    callee = resolved.get("qname") or resolved.get("qualname")
                    if callee:
                        callers_map.setdefault(callee, []).append(caller)
                        calls_map.setdefault(caller, []).append(callee)

        sections: list[str] = ["# Module Index\n\n*Auto-generated from AST — do not edit manually.*\n\n"]
        file_count = 0

        for path in target_files:
            source = workspace._read_text(path)
            adapter = adapter_for(path)
            if adapter is None:
                continue
            tree = adapter.parse(source)
            if tree is None:
                continue

            rel = path.relative_to(workspace.root)

            module_doc = ""
            if path.suffix.lower() == ".py":
                import ast as _ast

                parsed = tree.get("tree") if isinstance(tree, dict) else None
                if isinstance(parsed, _ast.Module):
                    module_doc = _ast.get_docstring(parsed) or ""
            else:
                module_doc = str(tree.get("docstring") or "").strip()

            sections.append(f"## `{rel}`\n")
            sections.append(f"{module_doc or '*(no module docstring)*'}\n\n")

            details = workspace._collect_symbol_details(path, source)
            for detail in details:
                if detail.kind not in ("function", "method", "class"):
                    continue
                qname = _qname(workspace.root, path, detail.qualname)
                summary = workspace._extract_symbol_summary(detail, source)
                doc = (detail.docstring or "").strip() or "*(no docstring)*"
                calls = calls_map.get(qname, [])
                called_by = callers_map.get(qname, [])

                sections.append(f"### `{qname}`\n")
                sections.append(f"`{detail.signature}`\n\n{doc}\n")
                if calls:
                    sections.append(f"\n**calls:** {', '.join(f'`{c}`' for c in calls)}")
                if called_by:
                    sections.append(f"\n**called by:** {', '.join(f'`{c}`' for c in called_by[:6])}")
                if calls or called_by:
                    sections.append("\n")
                sections.append("\n")

            file_count += 1

        content = "".join(sections)
        desc_path = workspace.root / "description.md"
        desc_path.write_text(content, encoding="utf-8")
        return _with_workflow({
            "path": str(desc_path),
            "files_documented": file_count,
            "size_bytes": desc_path.stat().st_size,
        })

    @app.tool(
        description=(
            "Return a workspace tree with per-file metadata: size, symbol counts, import edges, categories, "
            "module docstring, entry-point and leaf flags, dependency tag, and source purpose if present. "
            "Pass files=[...] or roots=[...] to scope the view; when omitted, the server uses the same "
            "relevant-source heuristic as generate_description."
        )
    )
    def workspace_tree(
        files: list[str] | None = None,
        roots: list[str] | None = None,
    ) -> dict[str, Any]:
        target_files = _workspace_source_files(workspace, module_plan, files=files, roots=roots)
        import_edges: list[dict[str, Any]] = []
        inbound: dict[str, int] = {}
        outbound: dict[str, int] = {}
        module_names: dict[str, str] = {}
        sources_by_path: dict[str, str] = {}
        file_meta_items: list[dict[str, Any]] = []
        for path in target_files:
            module_name = module_name_for_path(workspace.root, path)
            module_names[str(path.resolve())] = module_name
        for path in target_files:
            source = workspace._read_text(path)
            sources_by_path[str(path.resolve())] = source
            module_name = module_names[str(path.resolve())]
            for edge in _collect_local_imports(workspace.root, path, source):
                if edge.get("target_file") not in module_names:
                    continue
                import_edges.append(
                    {
                        "source": edge.get("source"),
                        "target": edge.get("target"),
                        "source_file": edge.get("source_file"),
                        "target_file": edge.get("target_file"),
                    }
                )
                outbound[module_name] = outbound.get(module_name, 0) + 1
                target_module = module_names.get(str(Path(edge["target_file"]).resolve()))
                if target_module is not None:
                    inbound[target_module] = inbound.get(target_module, 0) + 1
        for path in target_files:
            source = sources_by_path[str(path.resolve())]
            details = workspace._collect_symbol_details(path, source)
            module_name = module_names[str(path.resolve())]
            file_flags = flags_from_filename(path.name)
            suffix = _decode_filename_suffix(path.name)
            if int(file_flags) == 0:
                try:
                    inferred = infer_file_suffix_result(source)
                    file_flags = inferred.flags
                    suffix = {
                        "name": path.name,
                        "base_name": path.stem + path.suffix,
                        "stem": path.stem,
                        "value": int(file_flags),
                        "flags": int(file_flags),
                        "categories": category_names(file_flags),
                    }
                except OSError:
                    pass
            docstring: str | None = None
            _adapter = adapter_for(path)
            if _adapter is not None:
                _tree = _adapter.parse(source)
                if _tree is not None:
                    if path.suffix.lower() == ".py":
                        _parsed = _tree.get("tree") if isinstance(_tree, dict) else None
                        if isinstance(_parsed, _ast.Module):
                            docstring = _ast.get_docstring(_parsed)
                    else:
                        docstring = str(_tree.get("docstring") or "").strip() or None
            first_line = docstring.strip().splitlines()[0].strip() if docstring else None
            file_meta_items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "module": module_name,
                    "kind": "file",
                    "size": path.stat().st_size if path.exists() else 0,
                    "lines": len(source.splitlines()),
                    "flags": int(file_flags),
                    "categories": category_names(file_flags),
                    "suffix": suffix,
                    "docstring": docstring,
                    "purpose": first_line or None,
                    "symbol_count": len(details),
                    "entry_point": outbound.get(module_name, 0) > 0 and inbound.get(module_name, 0) == 0,
                    "leaf": inbound.get(module_name, 0) > 0 and outbound.get(module_name, 0) == 0,
                    "orphan": inbound.get(module_name, 0) == 0 and outbound.get(module_name, 0) == 0,
                }
            )

        return _with_workflow({
            "root": str(workspace.root),
            "files": file_meta_items,
            "import_edges": import_edges,
            "counts": {
                "files": len(target_files),
                "symbols": sum(item["symbol_count"] for item in file_meta_items),
                "entry_points": sum(1 for item in file_meta_items if item["entry_point"]),
                "leaves": sum(1 for item in file_meta_items if item["leaf"]),
                "orphans": sum(1 for item in file_meta_items if item["orphan"]),
            },
        })

    return app


def main() -> None:
    import os
    parser = argparse.ArgumentParser(description="Run the mini coding MCP server")
    # Check environment variable first, then fall back to cwd
    env_root = os.environ.get("MCP_WORKSPACE_ROOT")
    default_root = Path(env_root) if env_root else Path.cwd()
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    args = parser.parse_args()
    create_app(args.root).run(transport=args.transport)
