# Rule: Create a Python Project with mini_coding_mcp

## Overview

Use the `mini_coding_mcp` MCP server to scaffold, implement, and finalize Python projects.
Follow the steps in order. The server passively tracks progress through a built-in checklist
returned in every response — use it to know where you are and what to do next.

---

## Workflow checklist

Every tool response includes a `workflow` key. Read it after each call.

```json
{
  "workflow": {
    "checklist": [
      {"step": "0_plan",        "label": "Plan module structure",       "done": true,  "pending": []},
      {"step": "1_scaffold",    "label": "Scaffold all files (1/3)",    "done": false, "pending": ["process_data.py", "run_pipeline.py"]},
      {"step": "2_test",        "label": "Generate and finish tests (0/1)", "done": false, "pending": ["tests/test_process_data.py"]},
      {"step": "3_implement",   "label": "Implement symbols (0/3)",     "done": false, "pending": ["load_data.py"]},
      {"step": "4_validate",    "label": "Validate quality (0/3)",      "done": false, "pending": ["load_data.py"]},
      {"step": "5_rename",      "label": "Rename files if needed (optional)", "done": null},
      {"step": "last_finalize", "label": "Finalize file names",         "done": false}
    ],
    "next_tool": "scaffold_module",
    "next_target": "process_data.py"
  }
}
```

- `done: true` — step complete.
- `done: false` — step pending; `pending` lists the specific files still needed.
- `done: null` — optional step, no tracking.
- `next_tool` / `next_target` — suggested next call. Follow this unless you have a reason not to.

**Implemented** is detected automatically: a file is marked implemented when it contains no
remaining `raise NotImplementedError`. You do not need to signal this manually.

---

## Step -2 — Focus the workspace (when project is in a subfolder)

If the target project lives inside a subdirectory of the workspace root (e.g. `test_login_backend/`),
call `set_workspace_focus` **before any other step**. Without it, bare filenames like
`hash_password.py` resolve to the workspace root instead of the project folder.

```
set_workspace_focus(path="test_login_backend")
```

After this call:
- All bare filenames resolve inside `test_login_backend/`.
- `workspace_index`, `workspace_tree`, and all file-iteration tools scope to that folder.
- The focus is persisted in `.mcp_plan.json` and restored automatically on server restart.

The response includes `source_files_in_focus` — the files already present in the folder.

To restore full workspace scope: `clear_workspace_focus()`.

> **Skip this step** when working directly at the workspace root.

---

## Step -1 — Register project metadata

Call `init_project` to register the project name, Python version, and dependencies.
It does **not** create files — it returns a `manifest` dict with the recommended boilerplate
content for each file (`pyproject.toml`, `requirements.txt`, `.gitignore`, `tests/conftest.py`,
`src/{slug}/__init__.py`).

```
init_project(
    name="login_backend",
    description="JWT authentication service.",
    python_version="3.11",
    deps=["fastapi", "passlib", "python-jose"],
)
```

The only file written automatically is `src/{slug}/__init__.py` when it does not yet exist
(the package entry point must exist before `plan_module_structure` can reference it).

Use the returned `manifest` content to create each boilerplate file via `insert_code` or
`scaffold_module` so that creation is tracked by the plan.

---

## Step 0 — Plan the module structure

Call `plan_module_structure` before creating any source file.

- List every file you intend to create.
- Each entry needs: `name` (verb_noun.py), `purpose` (one sentence), `depends_on` (other files in this plan that it imports from).
- Do **not** create any file until `naming_issues` and `missing_deps` are both empty in the response.

```
plan_module_structure(files=[
    {"name": "load_data.py",    "purpose": "Load input data from disk.",     "depends_on": []},
    {"name": "process_data.py", "purpose": "Transform raw data.",            "depends_on": ["load_data.py"]},
    {"name": "run_pipeline.py", "purpose": "Entry-point orchestrator.",      "depends_on": ["load_data.py", "process_data.py"]},
])
```

The checklist step `0_plan` flips to `done: true` once the plan is accepted.

---

## Step 1 — Scaffold every file

Call `scaffold_module` for **each** file in the plan before writing any real implementation.

- The module docstring is taken from the `purpose` declared in the plan — do not pass it again.
- Provide stubs for all planned symbols (functions, classes).
- Every stub must have a `docstring`.
- Stubs raise `NotImplementedError` so the symbol index is populated immediately.

```
scaffold_module(
    destination_file="load_data.py",
    stubs=[
        {"name": "load_csv",      "kind": "function",
         "args": "path: str",     "returns": "list[dict]",
         "docstring": "Read a CSV file and return rows as dicts."},
        {"name": "validate_rows", "kind": "function",
         "args": "rows: list[dict]", "returns": "None",
         "docstring": "Raise ValueError on malformed rows."},
    ]
)
```

After scaffolding, the server may append a numeric category suffix to the filename
(e.g. `load_data.py` → `load_data_10.py`). Use the name from `destination_file` in the
response for all subsequent calls — never the original name you passed in.

The checklist tracks `1_scaffold` per file. Once all files are scaffolded it moves to `implement`, and
generated `tests/test_*.py` files are tracked separately in `2_test` until they no longer contain
`raise NotImplementedError`.

---

## Step 2 — Inspect the workspace (any time)

Use these read-only endpoints freely at any point to orient yourself:

| Endpoint | When to use |
|---|---|
| `workspace_index` | All symbols in the focused workspace. Cached — fast on repeat calls. |
| `workspace_tree` | Per-file metadata, categories, and entry/leaf flags. |
| `get_module_api` | Public API of a module without reading its body. |
| `get_symbol` | A symbol's code, callers, or position. |
| `get_symbol_with_deps` | A symbol plus everything it directly calls. |
| `get_relevant_context` | Transitive dep subgraph for a target file. |
| `size_hint` | Line/token count before reading a large file. |
| `search` | Symbols by name or text query. |
| `search_ast` | Symbols by AST shape (e.g. missing return annotations, async functions). |

These do not update the checklist.

> **Excluded from scanning by default:** `tests/`, `vendor/`, `docs/`, `examples/`,
> `node_modules/`, `.venv/`, `build/`, `dist/`, and all hidden directories.
> Files inside those directories are not indexed unless you pass their paths explicitly.

---

## Step 3 — Implement symbols

### Insert new code

Use `insert_code` to add functions, classes, or imports.

- Prefer `qname` anchors (e.g. `load_data:load_csv`) over line numbers.
- Every inserted function or class **must** have a docstring.
- After each call check `missing_docstrings` in the response and add any listed docstrings before continuing.

### Add imports

Use `add_import` for any `import` or `from … import` with automatic deduplication.

### Replace or patch existing symbols

- `replace_symbol(qname, new_source)` — swap an entire function or class body. Accepts a short name (e.g. `"load_csv"`) or a full qname (e.g. `"load_data:load_csv"`).
- `patch_symbol(qname, old_lines, new_lines)` — edit a sub-range inside a symbol.

The checklist `3_implement` flips to `done: true` per file when the file no longer contains
`raise NotImplementedError`. Replace all stubs in both the module code and the generated tests before
moving to validation.

---

## Step 4 — Validate quality

Run at least one quality check per file. Pass `files=["filename.py"]` to scope the check to a
single file — this is required for the checklist to mark that file as validated.

| Endpoint | What it catches | Scope param |
|---|---|---|
| `lint` | Syntax errors and diagnostics for a symbol. | `qname` (file resolved automatically) |
| `missing_annotations` | Functions missing type hints. | `files=` |
| `analyze_static_code` | Overly complex functions (loops, branches, lines). | `files=` |
| `dead_symbols` | Defined symbols that are never called. | `files=` |
| `exception_surface` | Unhandled exceptions propagating from a symbol. | `qname` |
| `impact_of_change` | Callers broken by a signature change. | `qname` |
| `class_hierarchy` | Verify inheritance chains are correct. | `qname` |

Fix all reported issues before finalizing.

---

## Step 5 — Rename files if needed

If a file's name no longer matches its abstraction after implementation, call `rename_file`.
It rewrites every import referencing the old name across the workspace (only files that
actually import the renamed module are touched — unrelated files are skipped).

```
rename_file(old_path="load_data.py", new_path="read_csv_rows.py")
```

Call this **before** `finalize_file_names` — rename while imports are still untagged.

---

## Step Last — Finalize file names

Call `finalize_file_names` to append dep-tags to every planned file.

```
finalize_file_names(files=None)   # processes all planned files
```

The tag is the sorted initials of all intra-workspace modules the file imports from
(e.g. `run_pipeline__ldpd.py`). Use `decode_file_tag` to reverse any tagged name.

The checklist step `last_finalize` flips to `done: true` after this call.

---

## Supplementary endpoints

| Endpoint | Purpose |
|---|---|
| `set_workspace_focus` | Scope all path resolution and file iteration to a subdirectory. |
| `clear_workspace_focus` | Restore full workspace scope after `set_workspace_focus`. |
| `find_usages` | Where a symbol is referenced (references / callers / all). |
| `get_call_graph` | Which function calls which across the workspace. |
| `rename_symbol` | Rename a symbol across the entire workspace. |
| `ignore_files` | Exclude specific files from indexing and iteration. |
| `unignore_files` | Re-include previously ignored files. |
| `list_ignored_files` | Audit the current ignore set. |
| `find_files_by_flag` | Files matching a category flag (e.g. `NETWORK_HTTP_API_CALLS`). |
| `file_suffix` | Infer numeric category flags for a single file. |
| `generate_description` | Auto-generate `description.md` from the live AST. Never write `.md` files manually. |

---

## Key invariants

1. **Focus first** — call `set_workspace_focus` before anything else when the project is in a subfolder. Without it, bare filenames resolve to the workspace root.
2. **`plan_module_structure` before creating files** — never create a file without a clean plan.
3. **`scaffold_module` before any implementation** — stubs must exist so the symbol index is populated.
4. **Use `destination_file` from the response** — the server may rename the file with a numeric suffix after scaffolding; the original name you passed may no longer be valid.
5. **Docstrings on every symbol** — enforced by `insert_code`; check `missing_docstrings`.
6. **Pass `files=` to quality tools** — required for the checklist to mark that file as validated.
7. **`rename_file` before `finalize_file_names`** — rename while imports are still untagged.
8. **`finalize_file_names` last** — dep-tags are the final step, not an intermediate one.
