1. insert_code
Insert code into a destination file. Prefer qname anchors like pkg.mod:Class.method. For Python files, imports are placed with import blocks, constants stay near the top, and definitions are inserted relative to the anchor symbol when provided. The default response uses stable symbol IDs and omits line numbers. Every inserted function or class MUST have a docstring. Check missing_docstrings in the response and add docstrings to any listed names before proceeding.
2. init_project
Step -1: create a runnable Python project layout before planning modules. Writes pyproject.toml, requirements.txt, .gitignore, tests/conftest.py, and a src package named after the project.
3. set_workspace_focus
Scope all relative path resolution and workspace iteration to a subdirectory. Call this when the target project lives inside a subfolder of the workspace root (e.g. 'test_login_backend'). After setting, bare filenames like 'hash_password.py' resolve to '<focus>/hash_password.py' and workspace_index/workspace_tree only show files inside that folder. Persisted in .mcp_plan.json. Use clear_workspace_focus to restore the full workspace scope.
4. clear_workspace_focus
Remove the workspace focus set by set_workspace_focus, restoring full workspace scope.
5. set_workspace_root
Dynamically change the workspace root to a different directory. This allows the MCP to work with different projects during a single session. The new root must be a valid directory. All subsequent tool calls will use this new root. Call with the target directory path.
6. get_workspace_root
Return information about the currently active workspace root, including source file count and focus directory if set.
7. scaffold_module
Reserve space in a new or existing module by writing stub definitions for all planned symbols before implementing them. Each stub gets a docstring and raises NotImplementedError so the symbol index knows what the module will contain. The server may append a numeric category suffix to the filename based on the imports and function calls present after scaffolding. Call this first when creating any new file. stubs is a list of objects with fields: name (required), kind ('function' | 'async_function' | 'class', default 'function'), args (argument string, e.g. 'path: str, model: nn.Module'), returns (return annotation, e.g. 'None'), docstring (one-line description), bases (base classes for kind='class', e.g. 'Dataset'). Pass with_tests=True to also generate a parallel failing test stub file for public functions.
8. workspace_index
Return the workspace-wide symbol index or the index for a single file. Default mode is an outline with qname, name, kind, and signature only; set verbose=True for full details.
9. get_symbol
Search the workspace for a symbol by qualified name or name. Use detail=summary, code, calls, position, or children. When detail=code or detail=calls, symbols already served this session are replaced with a stub comment to save context. Pass force_show=True to override and receive the full body again.
10. search
Search the workspace for symbols by query text and optional kind filter. Supports AST filters directly: async_only, base, missing_return_annotation, and raises.
11. get_symbols
Fetch several symbols in one round trip. Applies working-set stubs to repeated code bodies.
12. size_hint
Return a size hint for a file or symbol before reading it. The result includes line count, character count, and an estimated token count.
13. get_module_api
Return the public API of a module: public top-level classes, functions, and variables with signatures and docstrings, but without bodies.
14. search_ast
Search for AST shapes instead of text. Supports filters such as async_only, base, missing_return_annotation, raises, calls, decorator, and parameter_count_gt.
15. get_symbol_with_deps
Return a symbol together with the definitions of everything it directly calls within the workspace.
16. find_call_path
Find the shortest call path from one symbol to another. Returns a compact qname/signature chain.
17. neighbors
Return the direct caller/callee neighborhood for a symbol, optionally expanded by hops. The response is compact: qname/signature pairs only.
18. get_module_consumers
Return which workspace files consume a module and which imported names they use.
19. impact_of_change
Analyze the impact of changing a symbol. Returns callers and signature-mismatch flags, optionally comparing against a proposed new signature.
20. class_hierarchy
Return the inheritance chain for a class, including each ancestor's public methods.
21. dead_symbols
Find functions, methods, and classes defined in the scoped workspace files that are never called anywhere.
22. exception_surface
Return direct and transitive exception surfaces for a symbol, including exceptions raised by callees.
23. missing_annotations
List functions and methods missing parameter annotations or return annotations in the scoped workspace.
24. lint
Return current diagnostics filtered to a single stable symbol ID.
25. lint_file
Lint every symbol in a file after enumerating it through the index. This is the file-level shortcut for per-symbol linting.
26. check_plan_complete
Verify that the plan is fully implemented before finalizing file names. Returns every planned symbol still raising NotImplementedError, plus any planned files not yet on disk.
27. find_usages
Find where a symbol is used. Set kind=references, callers, or all. Use limit to cap the returned list sizes and avoid large payloads.
28. replace_symbol
Replace a symbol in place using its qualified name or name.
29. patch_symbol
Patch a sub-range of a symbol instead of replacing the entire body. Supports either a line-range patch or an old_lines/new_lines text replacement inside the symbol.
30. add_import
Add an import with deduplication.
31. rename_symbol
Rename a symbol across the workspace in a best-effort way.
32. plan_module_structure
Declare the full set of files you intend to create, their purposes, and their import dependencies before writing any code. Call this as STEP 0. Each entry in 'files' must have: name (verb_noun.py), purpose (one sentence), depends_on (list of other names in this plan that this file imports from). Returns naming_issues for files violating the verb_noun convention and missing_deps for depends_on references not present in the plan. Do not create any file until both lists are empty.
33. rename_file
Rename a file and rewrite every import that references it across the workspace. Use this as the LAST step if a file's name no longer matches its abstraction after implementation. new_path must follow the verb_noun.py convention.
34. finalize_file_names
STEP LAST (after rename_file) — append a compact dep-tag to every finalized file. The tag is the sorted initials of all intra-workspace modules the file imports from (external packages are excluded). Example: train_lora.py importing load_captions and save_checkpoint becomes train_lora__lcsc.py. Pass files=None to process all planned files, or supply a specific list of filenames. Use decode_file_tag to reverse any tagged name back to its full dependency list. Pass dry_run=True to preview the tags without writing any files.
35. decode_file_tag
Decode a dep-tagged filename (e.g. train_lora__lcsc.py) back to its local dependency list. Checks the plan registry first; falls back to re-deriving from the actual file's imports. The tag is the sorted initials of each intra-workspace imported module stem.
36. file_suffix
Infer the numeric suffix flags for one Python file by reading its source. Returns both the numeric bitmask and the active category names.
37. generate_regex_rule
Auto-create a regex rule from target strings using a trie-based generator. The result includes the anchored pattern and a compile check.
38. find_files_by_flag
Find all Python files whose inferred suffix flags include a given category, such as NETWORK_HTTP_API_CALLS. Pass files=[...] to scope the search explicitly; when omitted, the server prefers planned files and falls back to the full workspace.
39. ignore_files
Ignore one or more files so they are excluded from workspace iteration, call graphs, indexing, and other source-scoped tools. Paths are resolved against the workspace root and persisted in .mcp_plan.json.
40. unignore_files
Remove one or more files from the ignore list so workspace iteration includes them again. Paths are resolved against the workspace root and the updated list is persisted.
41. list_ignored_files
List the current ignore set with filename suffix metadata only. This does not read file contents; it decodes the numeric suffix from the filename and returns the active category names for audit.
42. get_call_graph
Return a function call graph showing which function calls which other function. Pass files=[...] to scope it explicitly; when omitted, the server prefers planned files and falls back to the full workspace. The result also includes module_dependencies, reverse_dependencies, entry_points, leaves, hotspots, and cycles. Use limit to cap large lists. Set include_unresolved=True to surface unresolved call edges.
43. get_relevant_context
Return a compact context bundle for one symbol: signature, docstring, direct callers, and direct callees.
44. analyze_static_code
Run static analysis and return functions that are considered complex. Pass files=[...] to scope it explicitly; when omitted, the server prefers planned files and falls back to the full workspace. A function is flagged when it exceeds the configured loop, branch, line, or repetition limits. Use this as an internal splitting signal.
45. generate_description
Auto-generate description.md for the workspace from the live AST. Includes module docstrings, every function/class signature and docstring, and what each function calls and is called by. Always call this instead of writing .md files manually. Re-call after implementing stubs to keep the description current.
46. workspace_tree
Return a workspace tree with per-file metadata: size, symbol counts, import edges, categories, module docstring, entry-point and leaf flags, dependency tag, and source purpose if present. Pass files=[...] or roots=[...] to scope the view; when omitted, the server uses the same relevant-source heuristic as generate_description.