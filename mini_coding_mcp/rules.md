# mini-coding-mcp rules

## Symbol identity
Always refer to code by qname: `pkg.mod:Class.method`. Never use line numbers.
Get a qname with `workspace_index(path)` or `search(query)`. The format is `module.path:QualName`.

## File naming
Name files as `verb_noun.py` — lowercase, one underscore minimum, describing the module's primary action and **specific** subject.
Good: `train_lora.py`, `load_captions.py`, `save_checkpoint.py`, `inject_lora_layers.py`.
Bad (no underscore): `dataset.py`, `utils.py`. Bad (too generic): `train_model.py`, `load_data.py`, `run_script.py`.
The noun must identify the domain object, not the abstraction category.

After the file is scaffolded, the server may append a numeric category suffix before any dependency tag.
The suffix is a bitmask derived from imports and call sites, so `train_lora_256.py` means the file matched
`FILE_IO`, while `train_lora_768.py` means it matched both `FILE_IO` and `PATH_AND_FILESYSTEM_OPERATIONS`.
Edit `mini_coding_mcp/file_suffix.py` to change the lookup dictionaries:
`LIBRARY_FLAG_LOOKUP`, `FUNCTION_FLAG_LOOKUP`, `QUALIFIED_FUNCTION_FLAG_LOOKUP`, and `METHOD_FLAG_LOOKUP`.
Use `file_suffix(path)` or `flags_from_filename(name)` to recover the bitmask from a filename like `train_lora_768.py`.

### Dep-tag collision prevention

The dep-tag appended by `finalize_file_names` is the **sorted initials** of each locally-imported module stem.
`hash_password` → `hp`, `manage_tokens` → `mt`, so a file importing both becomes `…__deps_hpmt.py`.

**Collision risk:** two modules with the same initials (e.g. `hash_password` and `handle_params`, both → `hp`)
make the tag ambiguous at a glance.

**Prevention rule:** before accepting a new filename in `plan_module_structure`, check whether any
existing module in the workspace produces the same initials. If a collision exists, rename one of the
files so its initials become unique. Call `decode_file_tag` on any tagged name to get the definitive
dependency list — the tag is a compact visual hint; `decode_file_tag` is always the source of truth.

## Mandatory workflow for any new set of files

### STEP 0 — Plan (before touching any file)
Call `plan_module_structure(files)` with every file you intend to create:
- `name`: the proposed filename (must pass verb_noun check and not be a generic term)
- `purpose`: one sentence describing what the module does — this becomes the file's module docstring
- `depends_on`: list of other files in this plan that this file imports from

The server returns `naming_issues` (with `reason` and `hint` per file) and `missing_deps`.
Fix all issues until `ok: true`. The server rejects both non-verb_noun names and known-generic stems like `train_model`, `load_data`, `run_script`.
At the end of the plan call, the server writes `.mcp_constraints.md` in the project root. Treat it as the active plan contract: it lists the allowed import edges, placement rules, symbol inventory, operation routing, and file-specific quality gates. Read it before the next step.

### STEP 1 — Scaffold
Call `scaffold_module(file, stubs)` for each file. The server automatically writes:
1. A module-level docstring from the plan's `purpose` field (with import dependencies noted)
2. Each stub as a `raise NotImplementedError` function/class with its own docstring
3. An updated `.mcp_constraints.md` entry for the file's symbol inventory and quality gates

The file will be unscaffoldable (`ok: false`) if `plan_module_structure` was not called first.
Include every foreseeable helper even if not requested yet.

### STEP 2 — Implement
Fill each stub one at a time with `replace_symbol`.

### STEP LAST — Rename then Finalize
If a file's name no longer matches its abstraction after implementation, call `rename_file(old, new)` first.
It moves the file and rewrites all `import` and `from … import` statements across the workspace.

Then call `finalize_file_names()` (or `finalize_file_names(files=[...])` for a subset).
The server appends a compact dep-tag derived from every intra-workspace import in the file.
Only files within the workspace folder count; external packages (stdlib, pip) are excluded.

**Tag encoding:** each locally-imported module stem contributes its initials (first character of each
underscore-separated word). Initials are sorted alphabetically and concatenated.
Example: `train_lora.py` imports `load_captions` (→ `lc`) and `save_checkpoint` (→ `sc`);
the file is renamed `train_lora__lcsc.py`.

Use `decode_file_tag(encoded_name)` to reverse any tagged name back to the full dependency list.

## Docstrings
Every function and class must have a one-line docstring describing inputs, outputs, and side effects.
After every `insert_code` call, check `missing_docstrings` in the response. If it is non-empty, fix those names before continuing.

## Read order (cheapest first)
1. `workspace_index(path)` — signatures + docstrings for the whole file. Start here.
2. `get_symbol(qname, detail='summary')` — signature, docstring, calls made, names read/written. Use before deciding to read the body.
3. `get_symbol(qname, detail='code')` — full source. Only call when you need the body. Already-seen bodies are returned as stubs; pass `force_show=True` to override.
4. `find_usages(qname, kind='all')` — before renaming or deleting.

## Write order (most precise first)
1. `replace_symbol(qname, new_source)` — whole-function swap. Preferred for any change inside an existing function or class.
2. `add_method(class_qname, code)` — add a new method to an existing class.
3. `insert_code(file, code, anchor=..., position=...)` — add near an existing symbol.
4. `insert_code(file, code)` — only when no anchor exists (new file, new top-level symbol).

## Imports
Always use `add_import(module, names=[])`. It deduplicates. Never hand-edit import blocks.
If the target file is part of a declared plan, `add_import` will reject any local import edge that is not listed in that file's `depends_on` whitelist.

## After every write
Check the response:
- `ok: false` or `introduced` list non-empty → there are new lint errors. Fix before continuing.
- `missing_docstrings` non-empty → add docstrings to listed names before continuing.
- `fixed_count > 0` → you resolved pre-existing issues.
Use `lint(qname)` to see the current state of a symbol without writing.

## Rename
Use `rename_symbol(old_qname, new_name)`. It is workspace-wide. Do not use `replace_symbol` + `find_usages` manually.

## Working set (context deduplication)
`get_symbol(detail='code')` stubs out symbols already served this session to save context.
The stub contains the qname and a re-fetch hint. A symbol is automatically un-stubbed after any write that touches it.
Pass `force_show=True` if you need the body back in context (e.g. before editing it).

## Project description
Never write README, description, or documentation `.md` files by hand.
Call `generate_description()` to produce `description.md` from the live AST.
It includes module docstrings, every function signature + docstring, and the full call graph (calls / called by).
Re-call it after implementing stubs to keep the description current.

## Never
- Pass line numbers to any tool.
- Re-read a file after a write to verify — the `introduced` list tells you what changed.
- Insert a function or class without a docstring.
- Create a file without calling `plan_module_structure` first.
- Write `.md` files manually — use `generate_description` instead.
- Use `get_call_graph()` or `analyze_static_code()` unless diagnosing complexity; they are slow workspace-wide scans.
- Skip `scaffold_module` when creating a new file — stubs must exist before implementations.
- Rename a file by hand — always use `rename_file` so imports stay consistent.
