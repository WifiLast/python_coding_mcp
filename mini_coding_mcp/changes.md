# `mini_coding_mcp`

A Model Context Protocol (MCP) server that lets LLMs read and edit Python codebases through structured symbol queries instead of raw file dumps. The goal is to cut the token cost of coding agents: navigation happens by qualified name, edits are syntax-aware, and every query returns only the projection the caller asks for — a signature, a list of callers, or full source.

## Design principles

- **Stable symbol IDs over line numbers.** Code is addressed by qualified name (`pkg.mod.Class.method`), not by line range. Edits and queries stay valid as files shift.
- **Minimum-viable projections.** Each tool returns the smallest useful view by default. The LLM expands on demand and never pays for context it didn't ask for.
- **Syntax-aware splicing.** Insertions land in the correct section of a file (imports at the top, constants in the middle, definitions at the end) without the LLM doing layout math.
- **Self-describing files.** Every file the tool creates or edits encodes enough information in its name and header that the LLM can navigate the project without opening bodies.

## Conventions enforced on managed code

These conventions apply to every Python file the tool creates or edits.

### File naming

A file's name encodes three things at a glance:

- **What the file does** — a semantic tag such as `control_flow`, `data_model`, `io`, or `transform`.
- **What it imports** — the modules it depends on.
- **What depends on it** — the files that import it.

A directory listing alone is enough for the LLM to understand how the project is wired, without opening a single body.

### File splitting and continuation marker

When a file would exceed **800 lines**, the tool splits it into numbered parts. Both halves carry the marker, so the relationship is obvious from the name alone:

- Before split: `workspace.py`
- After split: `workspace_1.py` and `workspace_2.py`
- Further splits add `_3`, `_4`, …

The `_1` suffix on the original file is the hint that continuation parts exist; without it, the LLM might assume the file stands alone. Each part keeps its own header listing the symbols it owns, so the index stays local.

### File header

Every file opens with a structured header containing:

1. A **flow summary** of the module, if it contains any control flow.
2. A **function index** — every function defined in the file, with its signature and a one-line description.
3. An **inlined utilities** section, clearly marked.

The header is the cheapest view of the file; an LLM reads it first and decides from there whether any body is worth fetching.

### Inlined utilities

Small, frequently used helper functions (below a configurable size threshold) are copied into every file that needs them, placed in the marked section after the header. This keeps the LLM from chasing imports while reading.

The tool deduplicates against the working set: if a helper has already been shown in an earlier file in the current session, it is omitted from later ones. The LLM sees each utility exactly once per session, regardless of how many files use it.

## Symbol query interface

Pass a qualified name like `math.exp` and choose one projection:

- **`code`** — the source of the symbol.
- **`position`** — the file and location where it is defined.
- **`callers`** — every site in the workspace that uses it.

The same interface works for any indexed symbol, whether it lives in the project or in a third-party module that has been scanned.

## Project structure

The tool's own codebase is split into four files, each kept under the 800-line ceiling. Once any of them grows past it, the split convention above takes over.

### `workspace.py`
Orchestrator. Owns the symbol index and routes every read, write, and lookup through one `MiniWorkspace` instance.

- `MiniWorkspace.__init__` (line 35) — builds the index caches and performs the initial workspace scan.
- `MiniWorkspace.outline` (line 383) — hierarchical symbol outline for a file or module qname.
- `MiniWorkspace.find_references` (line 432) — every site in the workspace that uses a given name.
- `MiniWorkspace.replace_symbol` (line 508) — replaces a symbol's source in place by qname.
- `MiniWorkspace.insert_code` (line 293) — runs a code-injection transaction via `manipulator.py`.

### `indexer.py`
AST traversal layer. Builds the symbol index and answers structural questions about parsed code.

- `PythonSymbolIndexer` (line 14) — `ast.NodeVisitor` subclass that emits index entries per file.
- `find_reference_hits_in_file` (line 83) — scans a file's AST for identifiers matching a target name.
- `extract_function_calls` (line 196) — resolves the calls made inside a function, with locations.
- `extract_symbol_summary` (line 230) — collects reads, writes, calls, and signatures for a symbol.

### `manipulator.py`
Syntax-aware code splicing and editing.

- `apply_python_snippet` (line 182) — places a snippet at the correct location based on its kind, or around a named anchor.
- `replace_source_span` (line 207) — line-range replacement primitive used by higher-level edit tools.
- `_classify_block` (line 23) — categorizes a parsed snippet as import, constant, or definition.

### `app.py`
MCP entry point.

- `create_app` (line 12) — registers the tools (`insert_code`, `get_symbol`, `search`, …) on a `FastMCP` instance.
- `main` (line 158) — parses arguments and serves the protocol over stdio.