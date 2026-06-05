---
name: mini-coding-mcp
description: >
  Use this skill whenever the user asks to create a new Python project, add modules
  to an existing project, explore or understand existing Python code, refactor symbols,
  rename files, or run quality checks — and the mini_coding_mcp (python_coding_mcp)
  MCP server is available. Activates on phrases like "create a project", "scaffold",
  "add a module", "explore the codebase", "what does X do", "find usages of",
  "lint", "check quality", or any task involving Python source files in the workspace.
version: 1.0.0
---

# mini-coding-mcp Skill

Use the `python_coding_mcp` (or `mini_coding_mcp`) MCP server for all Python project
work. Prefer its tools over direct file reads and edits — it maintains a live symbol
index, enforces naming conventions, and tracks workflow state automatically.

---

## Determine the mode first

| User intent | Mode |
|---|---|
| Create files, add functions, implement features | **Build mode** |
| Understand structure, trace calls, find symbols | **Explore mode** |
| Fix a bug, replace/patch a symbol | **Targeted edit** (build tools, no plan needed) |

---

## Build Mode — ordered workflow

Follow these steps in order. The `workflow.checklist` in every response shows where
you are. Read `next_tool` and `next_target` after each call.

### Step -2 — Focus (when project is in a subfolder)
```
set_workspace_focus(path="my_project/")
```
Do this before anything else when the target lives inside a subdirectory.
All bare filenames then resolve inside that folder. Persisted across restarts.

### Step -1 — Init
```
init_project(name=..., description=..., python_version=..., deps=[...])
```
Returns a `manifest` with boilerplate content. Write those files with `insert_code`.

### Step 0 — Plan
```
plan_module_structure(files=[
    {"name": "verb_noun.py", "purpose": "one sentence", "depends_on": [...]},
    ...
])
```
- Names must be `verb_noun.py` — no file until `naming_issues` and `missing_deps` are both empty.
- Every file the plan references in `depends_on` must also be in the plan.

### Step 1 — Scaffold each file
```
scaffold_module(
    destination_file="verb_noun.py",
    stubs=[
        {"name": "fn", "kind": "function", "args": "x: int", "returns": "str",
         "docstring": "one-line description"},
    ],
    with_tests=True,   # generates tests/test_<name>.py with failing stubs
)
```
- **Use `destination_file` from the response for all subsequent calls** — the server
  may rename the file with a numeric category suffix (e.g. `load_data_10.py`).
  Never use the name you passed in if the response shows a different one.
- All stubs must have a `docstring`.

### Step 2 — Inspect freely (any time)
See **Explore mode escalation ladder** below — these are always safe to call.

### Step 3 — Implement symbols
```
replace_symbol(qname="module:func", new_source="def func(...):\n    ...")
patch_symbol(qname="module:func", old_lines=[...], new_lines=[...])
insert_code(destination_file="...", code="...", anchor="module:Class")
add_import(module="pathlib", names=["Path"], path="load_data.py")
```
- Prefer `qname` anchors over line numbers — they survive edits.
- Check `missing_docstrings` in the response after every `insert_code`.
- A file is marked implemented automatically when it contains no `raise NotImplementedError`.

### Step 4 — Validate quality (required per file)
Pass `files=["filename.py"]` so the checklist marks that file as validated.

| Tool | Catches |
|---|---|
| `lint_file(path)` | Syntax errors and diagnostics |
| `missing_annotations(files=[...])` | Missing type hints |
| `analyze_static_code(files=[...])` | Complex functions (loops, branches, lines) |
| `dead_symbols(files=[...])` | Defined symbols never called |
| `exception_surface(qname=...)` | Unhandled exceptions propagating from a symbol |

### Step 5 — Rename (if needed)
```
rename_file(old_path="load_data.py", new_path="read_csv_rows.py")
```
Call before `finalize_file_names`. Rewrites every import referencing the old name.

### Step Last — Finalize
```
finalize_file_names(dry_run=True)   # preview tags first
finalize_file_names()               # append dep-tags to all planned files
```
Use `decode_file_tag("train_lora__lcsc.py")` to reverse any tagged name.

---

## Explore Mode — escalation ladder

Work cheapest → most expensive. Stop when you have enough information.

```
1. workspace_tree()                           # structure, flags, entry/leaf — always start here
2. decode_file_tag(name)                      # dependencies from filename alone, free
3. get_module_api(path)                       # public API: signatures + docstrings, no bodies
4. workspace_index(path=file)                 # all symbols in one file (scope with path=)
5. size_hint(qname_or_path)                   # token cost before reading a body
6. get_symbol(qname, detail='summary')        # one symbol's metadata, no body
7. get_symbol(qname, detail='code')           # full body — only when you need to edit it
```

**Never** call `workspace_index()` without `path=` on a large workspace — it returns
every symbol in every file. **Never** call `get_call_graph()` or `analyze_static_code()`
without `files=[...]` — both do a full workspace parse.

### Focused context tools
```
get_relevant_context(target="module_name")    # transitive deps of one module
get_symbol_with_deps(qname="mod:func")        # symbol + everything it calls
find_usages(qname="mod:func", kind="callers") # who calls this
search(query="token_count")                   # find by name or text
search_ast(kind="function", async_only=True)  # find by AST shape
```

### During exploration: ignore workflow fields
Every response contains `workflow` and `next_suggested_tool`. These track the build
pipeline. During exploration they are noise — do not follow them.

---

## Working set — token deduplication

`get_symbol(detail='code')` and `get_symbol_with_deps` automatically stub bodies
already shown this session:
```
# ↤ mod:func — shown earlier this session
#   re-fetch with get_symbol('mod:func', detail='code')
```
This is intentional. Use `force_show=True` only when you need to re-read after an edit.
Editing a symbol automatically evicts it from the set so the next read is always fresh.

---

## Key invariants

1. **`destination_file` in the response is truth** — after `scaffold_module` or
   `insert_code`, use the `destination_file` value, not what you passed. The server
   may rename the file with a numeric suffix.
2. **Disk filenames beat plan names** — `workspace_tree()` shows actual filenames.
   Plan pending lists use original names; if they differ, the disk wins.
3. **Plan before creating** — never create a file without a clean `plan_module_structure`.
4. **Scaffold before implementing** — stubs must exist so the symbol index is populated.
5. **Docstring on every symbol** — enforced by `insert_code`; fix `missing_docstrings` immediately.
6. **`files=` on quality tools** — required for the checklist to mark that file validated.
7. **`rename_file` before `finalize_file_names`** — rename while imports are still untagged.
8. **`set_workspace_focus` first** — when the project is in a subfolder, focus before anything else.

---

## Targeted edits (no full workflow needed)

For bug fixes or isolated changes to existing files, skip the plan/scaffold workflow:

```python
# 1. Orient
get_module_api("process_data.py")

# 2. Check size before reading body
size_hint("process_data:transform")

# 3. Read what you need
get_symbol("process_data:transform", detail="code")

# 4. Edit
patch_symbol(
    qname="process_data:transform",
    old_lines=["    return result"],
    new_lines=["    return result.strip()"],
)

# 5. Validate
lint_file("process_data.py")
```

---

## Common pitfalls

- **Using a plan-registry name after scaffolding** — always read `destination_file` from
  the scaffold response. If the response says `load_data_10.py`, every subsequent call
  must use `load_data_10.py`, not `load_data.py`.
- **Calling `get_call_graph()` unscoped** — always pass `files=[...]`.
- **Reading a body to get an overview** — use `get_module_api` or `workspace_index(path=)`
  first; a body read costs 10-50× more tokens than the API surface.
- **Forgetting `files=` on quality tools** — `lint(qname=...)` is per-symbol; for the
  checklist to advance, use `lint_file(path)` or pass `files=["name.py"]`.
- **Following `next_suggested_tool` during exploration** — it belongs to the build
  workflow and is irrelevant when you are only reading.