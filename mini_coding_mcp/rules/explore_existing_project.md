# Rule: Explore an Existing Project (Read-Only Mode)

## System prompt to use when loading this rule

```
You are inspecting an existing Python project.
Do not scaffold, rename, generate, or modify any files.
Only read files, inspect imports, summarize structure, and report findings.
Ignore the workflow checklist that appears in every MCP response — it belongs to the
build workflow and is irrelevant during exploration.
```

---

## Read-only contract

This rule activates an **inspection-only** mode. The tools below are the only ones you
may call. Everything else is forbidden until the user explicitly switches to a build task.

### Allowed tools

| Tool | Purpose |
|---|---|
| `set_workspace_focus(path)` | Scope iteration to a subdirectory |
| `clear_workspace_focus()` | Restore full workspace scope |
| `workspace_tree()` | File names, categories, structure — primary orientation |
| `decode_file_tag(name)` | Decode dep-tag from a filename |
| `workspace_index(path)` | All symbol signatures + docstrings in one file |
| `get_module_api(path)` | Public API surface (no bodies) |
| `size_hint(path_or_qname)` | Token cost before reading |
| `get_symbol(qname, detail='summary')` | One symbol's metadata without body |
| `get_symbol(qname, detail='code')` | Full body — only when explicitly needed |
| `get_symbol(qname, detail='calls')` | Call list for one symbol |
| `get_symbol_with_deps(qname)` | Symbol + transitive callees |
| `search(query)` | Find symbols by name or text |
| `search_ast(...)` | Find symbols by AST shape |
| `find_usages(qname)` | Where a symbol is referenced |
| `find_files_by_flag(flag)` | Files matching a category flag |
| `list_ignored_files()` | Audit the ignore set |
| `file_suffix(path)` | Read category flags for one file |
| `get_call_graph(files=[...])` | Call graph — always scope with `files=` |
| `get_relevant_context(target)` | Transitive dep subgraph for one module |

### Forbidden tools — do not call these during exploration

```
scaffold_module        insert_code           replace_symbol
patch_symbol           add_import            rename_symbol
plan_module_structure  rename_file           finalize_file_names
init_project           ignore_files          unignore_files
generate_description   generate_regex_rule   check_plan_complete
```

---

## Ignore the workflow field in every response

Every MCP response contains a `workflow` key with a checklist of build steps
(`scaffold`, `implement`, `rename`, `finalize`) and a `next_suggested_tool` hint.

**During exploration, ignore all of this.** The workflow reflects the build pipeline
state, not the inspection task. Do not follow `next_suggested_tool`. Do not treat
pending scaffold or implement steps as actions you need to take.

---

## Filename truth: disk names override plan names

The plan registry (visible in workflow `pending` lists) stores the **original untagged
name** used when the file was first planned (e.g. `store_user.py`). After scaffolding,
the server renames files to include a numeric suffix and/or dep-tag, so the actual file
on disk may be `store_user_2097152.py` or `serve_auth__deps_hpmt4su2.py`.

**Always use the disk filename, never the plan name.**

- Call `workspace_tree()` first — it returns the actual filenames on disk.
- When the workflow pending list and the disk filenames disagree, the disk wins.
- Never construct a filename by stripping the suffix from a plan name.
- Use `decode_file_tag(name)` to understand what a tagged name means, not to infer the original name.

---

## Escalation ladder (cheapest → most expensive)

Work through these in order. Stop as soon as you have enough information.

```
1. workspace_tree()                              # names, flags, structure — always start here
2. decode_file_tag(name)                         # dependencies from filename alone
3. get_module_api(path)                          # public API surface, no bodies
4. workspace_index(path)                         # all symbols, signatures, docstrings
5. size_hint(path)                               # cost check before reading body
6. get_symbol(qname, detail='summary')           # one symbol without body
7. get_symbol(qname, detail='code')              # full body — only when needed
```

---

## Numeric suffix on pre-existing files

The numeric category suffix (e.g. `hash_password_268435456.py`) is stamped **only when
a file is written through the MCP server** (`scaffold_module`, `insert_code`, etc.).
Files created outside the MCP workflow have no suffix even if their imports match a
known category.

During exploration: call `file_suffix(path)` to see what suffix a file would get —
this is read-only and does not rename anything. Report the gap to the user; do not
rename the file unless asked.

---

## Never

- Follow `next_suggested_tool` hints during exploration.
- Use a plan-registry name when the disk filename differs — the disk wins.
- Call `get_call_graph()` or `analyze_static_code()` without `files=[...]` — both are slow workspace-wide scans.
- Call `workspace_index()` without a `path` argument on a large workspace — scope it to one file at a time.
- Read a file body to "get an overview" — use `workspace_tree()` and `workspace_index(path)` first.
- Guess the meaning of a dep-tag — always call `decode_file_tag`.
