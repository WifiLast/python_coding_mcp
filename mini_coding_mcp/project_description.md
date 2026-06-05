# Project Description: `mini_coding_mcp`

`mini_coding_mcp` is a powerful, lightweight Model Context Protocol (MCP) server that provides structured Python code intelligence, AST-based symbol indexing, cross-reference searching, diagnostics type-checking, and intelligent, syntax-aware code injections.

## Project Structure & File Layout

Following codebase modularization requirements, the codebase is split into 4 core files to ensure each file remains well under the 900-line size limit.

### 1. [workspace.py](file:///c:/Users/MartinStark/Documents/GitHub/PLS/mcp_module_builder/mini_coding_mcp/workspace.py)
* **Description**: The core orchestrator module containing the `MiniWorkspace` class.
* **Key Functions**:
  - `MiniWorkspace.__init__` (Line 35): Initializes the workspace index caches and scans symbols.
  - `MiniWorkspace.insert_code` (Line 293): Coordinates code injection transactions using manipulator tools.
  - `MiniWorkspace.index_workspace` / `MiniWorkspace.file_index` / `MiniWorkspace.outline` (Line 383+): Symbol index views for the workspace or a single file.
  - `MiniWorkspace.replace_symbol` (Line 508): Replaces code blocks in-place for a specific qualified name.
  - `MiniWorkspace.find_references` / `MiniWorkspace.find_callers` / `MiniWorkspace.search_ast` (Line 432+): Usage and structural lookup helpers.

### 2. [indexer.py](file:///c:/Users/MartinStark/Documents/GitHub/PLS/mcp_module_builder/mini_coding_mcp/indexer.py)
* **Description**: AST traversal and analysis module for symbol indexing and reference scanning.
* **Key Functions**:
  - `PythonSymbolIndexer` class (Line 14): `ast.NodeVisitor` that builds index entries for files.
  - `find_reference_hits_in_file` (Line 83): Scans file AST for identifiers matching a symbol name.
  - `extract_function_calls` (Line 196): Resolves call names and locations inside a function node.
  - `extract_symbol_summary` (Line 230): Collects AST read/write variables, calls, and signatures.

### 3. [manipulator.py](file:///c:/Users/MartinStark/Documents/GitHub/PLS/mcp_module_builder/mini_coding_mcp/manipulator.py)
* **Description**: Syntax-aware code splicing, editing, and injection helpers.
* **Key Functions**:
  - `apply_python_snippet` (Line 182): Splices code snippets into existing files at correct positions (e.g. imports at top, constants next, definitions at end) or around anchor elements.
  - `replace_source_span` (Line 207): Replaces source lines in a given range.
  - `_classify_block` (Line 23): Categorizes parsed code snippets into imports, constants, or definitions.

### 4. [app.py](file:///c:/Users/MartinStark/Documents/GitHub/PLS/mcp_module_builder/mini_coding_mcp/app.py)
* **Description**: MCP application wrapper and tool registry.
* **Key Functions**:
  - `create_app` (Line 12): Registers tools (e.g., `insert_code`, `get_symbol`, `search`, `workspace_index`, `find_usages`) to the `FastMCP` instance.
  - `main` (Line 158): Handles parser configuration and starts the MCP server over standard input/output.
