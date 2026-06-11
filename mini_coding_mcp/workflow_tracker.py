"""Passive workflow progress tracker for the create_python_project checklist.

State is stored in module_plan["_workflow"] and therefore persisted automatically
by _save_module_plan into .mcp_plan.json. No file I/O is performed here directly.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_STUB_RE = re.compile(r'\braise\s+NotImplementedError\b')

_PHASE_NEXT_TOOL: dict[str, str | None] = {
    "none":      None,
    "plan":      "plan_module_structure",
    "scaffold":  "scaffold_module",
    "implement": "replace_symbol",
    "test":      "insert_code",
    "quality":   "lint",
    "done":      "finalize_file_names",
}

_CHECKLIST_STEPS = [
    ("0_plan",        "Plan module structure"),
    ("1_scaffold",    "Scaffold all files"),
    ("2_test",        "Generate and finish tests"),
    ("3_implement",   "Implement symbols"),
    ("4_validate",    "Validate quality"),
    ("5_rename",      "Rename files if needed (optional)"),
    ("last_finalize", "Finalize file names"),
]


class WorkflowTracker:
    """Passive checklist tracker — updated by app.py after each tool call."""

    def __init__(self, state: dict[str, Any]) -> None:
        # state is the live module_plan["_workflow"] dict; mutations here persist on next _persist_module_plan()
        self._s = state

    @classmethod
    def from_plan(cls, module_plan: dict[str, Any]) -> "WorkflowTracker":
        if "_workflow" not in module_plan or not isinstance(module_plan["_workflow"], dict):
            module_plan["_workflow"] = {
                "plan_accepted": False,
                "finalize_called": False,
                "files": {},
                "tests": {},
            }
        return cls(module_plan["_workflow"])

    # ------------------------------------------------------------------
    # Update hooks — called by app.py after each tool returns
    # ------------------------------------------------------------------

    def on_plan(self, file_names: list[str], ok: bool) -> None:
        self._s["plan_accepted"] = ok
        self._s["finalize_called"] = False
        existing: dict[str, Any] = self._s.get("files", {})
        fresh: dict[str, Any] = {}
        for name in file_names:
            fresh[name] = existing.get(name, _blank_file_state())
        self._s["files"] = fresh
        self._s["tests"] = self._s.get("tests", {})

    def on_scaffold(self, file_name: str) -> None:
        _file(self._s, file_name)["scaffolded"] = True

    def on_test_generated(self, file_path: Path, workspace_root: Path) -> None:
        """Register a generated test file in the workflow state."""
        _test_file(self._s, _relative_label(file_path, workspace_root))["generated"] = True

    def on_edit(self, file_path: Path, workspace_root: Path) -> None:
        """After insert_code / replace_symbol / patch_symbol — detect if all stubs are gone."""
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        has_stubs = bool(_STUB_RE.search(source))
        name = _relative_label(file_path, workspace_root)
        if _is_test_file(file_path):
            tests: dict[str, Any] = self._s.setdefault("tests", {})
            entry = tests.get(name)
            if entry is not None:
                entry["implemented"] = not has_stubs
                entry["generated"] = True
            else:
                tests[name] = {"generated": True, "implemented": not has_stubs}
            return
        files: dict[str, Any] = self._s.setdefault("files", {})
        entry = files.get(name)
        if entry is not None:
            entry["implemented"] = not has_stubs

    def on_quality_check(self, file_names: list[str]) -> None:
        for name in file_names:
            entry = self._s.get("files", {}).get(name)
            if entry is not None:
                entry["quality_run"] = True

    def on_quality_check_qname(self, qname: str) -> None:
        """Variant for lint(qname) — resolves the file stem from the qname."""
        module_part = qname.split(":")[0]
        stem = module_part.split(".")[-1]
        self.on_quality_check([f"{stem}.py"])

    def on_rename(self, old_name: str, new_name: str) -> None:
        files: dict[str, Any] = self._s.setdefault("files", {})
        if old_name in files:
            files[new_name] = files.pop(old_name)
        files.setdefault(new_name, _blank_file_state())["final_name"] = new_name

    def on_finalize(self, rename_results: list[dict[str, Any]]) -> None:
        self._s["finalize_called"] = True
        files: dict[str, Any] = self._s.setdefault("files", {})
        for r in rename_results:
            if r.get("skipped"):
                continue
            old = Path(r.get("file", "")).name
            new_path = r.get("new_name") or r.get("new_path") or r.get("file", "")
            new = Path(str(new_path)).name
            if old in files:
                files[old]["final_name"] = new

    # ------------------------------------------------------------------
    # Snapshot — merged into every tool response
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        files: dict[str, Any] = self._s.get("files", {})
        tests: dict[str, Any] = self._s.get("tests", {})
        plan_ok: bool = self._s.get("plan_accepted", False)
        finalized: bool = self._s.get("finalize_called", False)

        total = len(files)
        n_scaffold = sum(1 for f in files.values() if f.get("scaffolded"))
        n_impl = sum(1 for f in files.values() if f.get("implemented"))
        n_qual = sum(1 for f in files.values() if f.get("quality_run"))
        total_tests = len(tests)
        n_test_generated = sum(1 for f in tests.values() if f.get("generated"))
        n_test_done = sum(1 for f in tests.values() if f.get("implemented"))

        pending_scaffold = [n for n, f in files.items() if not f.get("scaffolded")]
        pending_impl = [n for n, f in files.items() if f.get("scaffolded") and not f.get("implemented")]
        pending_qual = [n for n, f in files.items() if f.get("implemented") and not f.get("quality_run")]
        pending_tests = [n for n, f in tests.items() if not f.get("implemented")]

        checklist = [
            {
                "step": "0_plan",
                "label": "Plan module structure",
                "done": plan_ok,
                "pending": [] if plan_ok else ["call plan_module_structure"],
            },
            {
                "step": "1_scaffold",
                "label": f"Scaffold all files ({n_scaffold}/{total})",
                "done": total > 0 and n_scaffold == total,
                "pending": pending_scaffold,
            },
            {
                "step": "2_test",
                "label": f"Generate and finish tests ({n_test_done}/{total_tests})",
                "done": total_tests == 0 or n_test_done == total_tests,
                "pending": pending_tests,
            },
            {
                "step": "3_implement",
                "label": f"Implement symbols ({n_impl}/{total})",
                "done": total > 0 and n_impl == total,
                "pending": pending_impl,
            },
            {
                "step": "4_validate",
                "label": f"Validate quality ({n_qual}/{total})",
                "done": total > 0 and n_qual == total,
                "pending": pending_qual,
            },
            {
                "step": "5_rename",
                "label": "Rename files if needed (optional)",
                "done": None,
            },
            {
                "step": "last_finalize",
                "label": "Finalize file names",
                "done": finalized,
            },
        ]

        # determine current phase
        if not plan_ok or total == 0:
            phase = "plan" if plan_ok or total > 0 else "none"
            next_target = None
        elif n_scaffold < total:
            phase = "scaffold"
            next_target = pending_scaffold[0] if pending_scaffold else None
        elif total_tests > 0 and n_test_done < total_tests:
            phase = "test"
            next_target = pending_tests[0] if pending_tests else None
        elif n_impl < total:
            phase = "implement"
            next_target = pending_impl[0] if pending_impl else None
        elif n_qual < total:
            phase = "quality"
            next_target = pending_qual[0] if pending_qual else None
        else:
            phase = "done"
            next_target = None

        return {
            "checklist": checklist,
            "next_tool": _PHASE_NEXT_TOOL.get(phase),
            "next_target": next_target,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _blank_file_state() -> dict[str, Any]:
    return {"scaffolded": False, "implemented": False, "quality_run": False, "final_name": None}


def _blank_test_state() -> dict[str, Any]:
    return {"generated": False, "implemented": False}


def _file(state: dict[str, Any], name: str) -> dict[str, Any]:
    files: dict[str, Any] = state.setdefault("files", {})
    if name not in files:
        files[name] = _blank_file_state()
    return files[name]


def _relative_label(file_path: Path, workspace_root: Path) -> str:
    try:
        return str(file_path.relative_to(workspace_root))
    except ValueError:
        return file_path.name


def _is_test_file(file_path: Path) -> bool:
    return file_path.name.startswith("test_") or "tests" in file_path.parts


def _test_file(state: dict[str, Any], name: str) -> dict[str, Any]:
    tests: dict[str, Any] = state.setdefault("tests", {})
    if name not in tests:
        tests[name] = _blank_test_state()
    return tests[name]
