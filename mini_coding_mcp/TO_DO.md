Plan: JavaScript / TypeScript Support
Step 1 — Language adapter protocol (new file: lang/adapter.py)
Define a LanguageAdapter protocol that every language must implement. This is the contract the rest of the codebase programs against, so nothing outside lang/ ever imports ast directly again.


class LanguageAdapter(Protocol):
    extensions: frozenset[str]
    def parse(self, source: str) -> Any | None: ...
    def extract_symbols(self, tree, path: Path) -> list[SymbolDetail]: ...
    def extract_imports(self, tree, path: Path) -> list[ImportEdge]: ...
    def extract_calls(self, tree, path: Path, source: str) -> list[CallEdge]: ...
    def extract_complexity(self, tree, path: Path) -> list[FunctionComplexity]: ...
    def insert_code(self, source: str, code: str, anchor: str | None, position: str) -> str: ...
    def replace_symbol(self, source: str, symbol: SymbolDetail, new_source: str) -> str: ...
Step 2 — Wrap existing Python logic (new file: lang/python_adapter.py)
Move all the Python-specific logic from indexer.py, manipulator.py, call_graph.py, and static_analysis.py behind PythonAdapter implementing the protocol. No behaviour changes — purely a structural move. This proves the abstraction works before adding JS.

Step 3 — JavaScript / TypeScript adapter (new file: lang/javascript_adapter.py)
Use tree-sitter (pip install tree-sitter tree-sitter-javascript tree-sitter-typescript) — the only Python-native option that handles JS, TS, JSX, and TSX reliably without subprocess overhead.

Supported extensions: .js, .mjs, .cjs, .ts, .tsx, .jsx

Key JS-specific mapping challenges to handle explicitly:

JS construct	Python equivalent
function foo(){} / const foo = () =>	def foo()
export default / module.exports	module-level symbol
import X from 'y' / require('y')	import / from x import
class Foo extends Bar	class Foo(Bar)
async function / Promise	async def / asyncio
Always exclude from iteration: node_modules/, dist/, build/, .next/, *.min.js, *.bundle.js

Step 4 — Language router (new file: lang/router.py)

_ADAPTERS: dict[str, LanguageAdapter] = {
    ".py": PythonAdapter(),
    ".js": JavaScriptAdapter(), ".mjs": JavaScriptAdapter(),
    ".ts": JavaScriptAdapter(), ".tsx": JavaScriptAdapter(),
    ".jsx": JavaScriptAdapter(),
}

def adapter_for(path: Path) -> LanguageAdapter | None:
    return _ADAPTERS.get(path.suffix.lower())
Step 5 — Replace _iter_workspace_python_files in workspace.py
Rename to _iter_workspace_source_files(langs=None) where langs is an optional set of extensions to filter by. All 12 call sites in workspace.py get updated. The ignore list, hidden-directory filter, and __pycache__ exclusion all stay — add node_modules to the exclusion list.

_scope_python_files becomes _scope_source_files with the .py hardcode replaced by a check against _ADAPTERS.

Step 6 — Extend file_suffix.py
Add JS npm packages to LIBRARY_FLAG_LOOKUP:


# JS — runtime / server
"express": Category.NETWORK_HTTP_API_CALLS,
"fastify": Category.NETWORK_HTTP_API_CALLS,
"axios": Category.NETWORK_HTTP_API_CALLS,
"socket.io": Category.WEBSOCKET_AND_STREAMING,
"ws": Category.WEBSOCKET_AND_STREAMING,
"mongoose": Category.DATABASE_OPERATIONS,
"prisma": Category.DATABASE_OPERATIONS,
"fs": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
"path": Category.PATH_AND_FILESYSTEM_OPERATIONS,
"worker_threads": Category.THREADING_AND_MULTIPROCESSING,
"child_process": Category.SUBPROCESS_AND_SHELL_COMMANDS | Category.PROCESS_LIFECYCLE,
"crypto": Category.SECURITY_AUTHENTICATION_SECRETS | Category.ENCODING_DECODING,
# JS — frontend / UI
"react": Category.UI_AND_CLI_INTERFACES,
"vue": Category.UI_AND_CLI_INTERFACES,
"svelte": Category.UI_AND_CLI_INTERFACES,
# JS — testing
"jest": Category.TESTING | Category.MOCKING_AND_FIXTURES,
"vitest": Category.TESTING,
"mocha": Category.TESTING,
# JS — validation
"zod": Category.VALIDATION_AND_SANITIZATION | Category.TYPES_AND_SCHEMAS,
"joi": Category.VALIDATION_AND_SANITIZATION,
_SuffixVisitor uses Python's ast — for JS files, the tree-sitter adapter extracts modules and calls directly and feeds them into infer_file_suffix_result via a language-agnostic path.

Step 7 — Extend scaffold_module for JS
Add a language parameter ("python" default, "javascript", "typescript"). JS scaffold emits:


// TypeScript scaffold
export async function integrate_expression(expression: string): Promise<string> {
    /** Integrate a mathematical expression. */
    throw new Error("NotImplemented");
}
Naming convention for JS files stays verb_noun.js/ts — the same regex in plan_module_structure already enforces this.

Step 8 — Update generate_description and get_call_graph
Both currently call Python-specific parsers directly. Route through adapter_for(path) instead. The output format stays identical — qname, file_path, signature, docstring — so the MCP tools above them need no changes.

Implementation order

Step 1  lang/adapter.py          — protocol only, no deps
Step 2  lang/python_adapter.py   — wrap existing code, run existing tests
Step 3  lang/javascript_adapter.py — tree-sitter JS/TS
Step 4  lang/router.py           — wire adapters together
Step 5  workspace.py             — replace _iter_workspace_python_files
Step 6  file_suffix.py           — npm package mappings
Step 7  scaffold_module          — JS template mode
Step 8  generate_description + get_call_graph — route through adapter
Steps 1-4 can be done and tested in isolation before touching workspace.py. Step 5 is the integration risk — run the full test suite after it.