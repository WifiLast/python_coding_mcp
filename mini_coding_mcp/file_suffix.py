from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import IntFlag
from pathlib import Path
from typing import Iterable

__all__ = [
    "Category",
    "FileSuffixResult",
    "LIBRARY_FLAG_LOOKUP",
    "FUNCTION_FLAG_LOOKUP",
    "QUALIFIED_FUNCTION_FLAG_LOOKUP",
    "METHOD_FLAG_LOOKUP",
    "category_from_name",
    "category_names",
    "infer_file_suffix_result",
    "infer_file_suffix_flags",
    "flags_from_filename",
    "decode_filename_suffix",
    "suffix_number",
    "split_numeric_suffix",
    "apply_numeric_suffix",
    "filename_with_suffix",
]


class Category(IntFlag):
    MAIN_ENTRYPOINT_TERMINAL_BOOTSTRAP = 1 << 0
    MATH_OPERATIONS = 1 << 1
    STRING_OPERATIONS = 1 << 2
    VARIABLE_AND_STATE_MANAGEMENT = 1 << 3
    DATA_STRUCTURES = 1 << 4
    CONTROL_FLOW = 1 << 5
    FUNCTIONS_AND_CALLABLES = 1 << 6
    CLASSES_AND_OBJECTS = 1 << 7
    FILE_IO = 1 << 8
    PATH_AND_FILESYSTEM_OPERATIONS = 1 << 9
    JSON_YAML_TOML_CSV_PROCESSING = 1 << 10
    PARSING_AND_SERIALIZATION = 1 << 11
    DATE_TIME_OPERATIONS = 1 << 12
    REGEX_AND_PATTERN_MATCHING = 1 << 13
    VALIDATION_AND_SANITIZATION = 1 << 14
    ERROR_HANDLING = 1 << 15
    LOGGING_AND_TERMINAL_OUTPUT = 1 << 16
    CONFIGURATION_MANAGEMENT = 1 << 17
    ENVIRONMENT_VARIABLES = 1 << 18
    DEPENDENCY_IMPORTS = 1 << 19
    NETWORK_HTTP_API_CALLS = 1 << 20
    DATABASE_OPERATIONS = 1 << 21
    ASYNC_AND_CONCURRENCY = 1 << 22
    THREADING_AND_MULTIPROCESSING = 1 << 23
    SUBPROCESS_AND_SHELL_COMMANDS = 1 << 24
    EXTERNAL_APPLICATION_CONTROL = 1 << 25
    IPC_RPC_AND_MCP_COMMUNICATION = 1 << 26
    WEBSOCKET_AND_STREAMING = 1 << 27
    SECURITY_AUTHENTICATION_SECRETS = 1 << 28
    PERMISSIONS_AND_SANDBOXING = 1 << 29
    TESTING = 1 << 30
    MOCKING_AND_FIXTURES = 1 << 31
    LINTING_FORMATTING_TYPING = 1 << 32
    PERFORMANCE_OPTIMIZATION = 1 << 33
    CACHING = 1 << 34
    MEMORY_MANAGEMENT = 1 << 35
    DEBUGGING_AND_DIAGNOSTICS = 1 << 36
    PROFILING_AND_TRACING = 1 << 37
    DOCUMENTATION = 1 << 38
    EXAMPLES_AND_USAGE = 1 << 39
    UI_AND_CLI_INTERFACES = 1 << 40
    ARGUMENT_PARSING = 1 << 41
    INTERACTIVE_TERMINAL_TOOLS = 1 << 42
    PROCESS_LIFECYCLE = 1 << 43
    STARTUP_SHUTDOWN_HOOKS = 1 << 44
    PLUGIN_EXTENSION_SYSTEM = 1 << 45
    CODE_SEARCH_AND_INDEXING = 1 << 46
    CODE_EDITING_AND_PATCHING = 1 << 47
    CODE_GENERATION = 1 << 48
    GRAPH_OPERATIONS = 1 << 49
    EVENT_HANDLING = 1 << 50
    QUEUE_AND_MESSAGING = 1 << 51
    ENCODING_DECODING = 1 << 52
    COMPRESSION = 1 << 53
    SCHEMA_MIGRATION = 1 << 54
    EMBEDDING_AND_VECTOR_OPERATIONS = 1 << 55
    PROMPT_ENGINEERING = 1 << 56
    HEALTH_CHECK_AND_METRICS = 1 << 57
    REPO_GIT_OPERATIONS = 1 << 58
    BUILD_PACKAGING_DISTRIBUTION = 1 << 59
    IMAGE_PROCESSING = 1 << 60
    AUDIO_PROCESSING = 1 << 61
    VIDEO_PROCESSING = 1 << 62
    GEOMETRY_3D_PROCESSING = 1 << 63
    BLENDER_OPERATIONS = 1 << 64
    MACHINE_LEARNING_AI = 1 << 65
    NUMERIC_SCIENTIFIC_COMPUTING = 1 << 66
    SIMULATION = 1 << 67
    HARDWARE_DEVICE_IO = 1 << 68
    AUTOMATION_WORKFLOWS = 1 << 69
    SHARED_UTILITIES = 1 << 70
    CONSTANTS = 1 << 71
    TYPES_AND_SCHEMAS = 1 << 72
    EXCEPTIONS = 1 << 73
    ADAPTERS = 1 << 74
    PROTOCOLS_INTERFACES = 1 << 75
    COMPATIBILITY_LAYER = 1 << 76
    EXPERIMENTAL = 1 << 77
    DEPRECATED = 1 << 78
    MISCELLANEOUS = 1 << 79


LIBRARY_FLAG_LOOKUP: dict[str, Category] = {
    "argparse": Category.ARGUMENT_PARSING | Category.UI_AND_CLI_INTERFACES,
    "asyncio": Category.ASYNC_AND_CONCURRENCY,
    "concurrent.futures": Category.THREADING_AND_MULTIPROCESSING,
    "configparser": Category.CONFIGURATION_MANAGEMENT,
    "csv": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "datetime": Category.DATE_TIME_OPERATIONS,
    "glob": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    # Web frameworks
    "fastapi": Category.NETWORK_HTTP_API_CALLS,
    "flask": Category.NETWORK_HTTP_API_CALLS,
    "django": Category.NETWORK_HTTP_API_CALLS | Category.DATABASE_OPERATIONS,
    "starlette": Category.NETWORK_HTTP_API_CALLS | Category.ASYNC_AND_CONCURRENCY,
    "aiohttp": Category.NETWORK_HTTP_API_CALLS | Category.ASYNC_AND_CONCURRENCY,
    "tornado": Category.NETWORK_HTTP_API_CALLS | Category.ASYNC_AND_CONCURRENCY,
    "litestar": Category.NETWORK_HTTP_API_CALLS,
    # ASGI / WSGI servers
    "uvicorn": Category.NETWORK_HTTP_API_CALLS | Category.PROCESS_LIFECYCLE,
    "gunicorn": Category.NETWORK_HTTP_API_CALLS | Category.PROCESS_LIFECYCLE,
    "hypercorn": Category.NETWORK_HTTP_API_CALLS | Category.PROCESS_LIFECYCLE,
    # Data validation / serialisation
    "pydantic": Category.TYPES_AND_SCHEMAS | Category.VALIDATION_AND_SANITIZATION,
    "marshmallow": Category.TYPES_AND_SCHEMAS | Category.PARSING_AND_SERIALIZATION,
    "attrs": Category.TYPES_AND_SCHEMAS,
    "cerberus": Category.VALIDATION_AND_SANITIZATION,
    # Authentication / security
    "passlib": Category.SECURITY_AUTHENTICATION_SECRETS,
    "bcrypt": Category.SECURITY_AUTHENTICATION_SECRETS,
    "jose": Category.SECURITY_AUTHENTICATION_SECRETS,
    "jwt": Category.SECURITY_AUTHENTICATION_SECRETS,
    "python_jose": Category.SECURITY_AUTHENTICATION_SECRETS,
    "cryptography": Category.SECURITY_AUTHENTICATION_SECRETS | Category.ENCODING_DECODING,
    "hashlib": Category.SECURITY_AUTHENTICATION_SECRETS | Category.ENCODING_DECODING,
    "hmac": Category.SECURITY_AUTHENTICATION_SECRETS,
    "secrets": Category.SECURITY_AUTHENTICATION_SECRETS,
    "itsdangerous": Category.SECURITY_AUTHENTICATION_SECRETS,
    "authlib": Category.SECURITY_AUTHENTICATION_SECRETS,
    # HTTP clients
    "httpx": Category.NETWORK_HTTP_API_CALLS,
    # ORM / databases
    "sqlalchemy": Category.DATABASE_OPERATIONS,
    "alembic": Category.DATABASE_OPERATIONS,
    "tortoise": Category.DATABASE_OPERATIONS | Category.ASYNC_AND_CONCURRENCY,
    "databases": Category.DATABASE_OPERATIONS | Category.ASYNC_AND_CONCURRENCY,
    "motor": Category.DATABASE_OPERATIONS | Category.ASYNC_AND_CONCURRENCY,
    "pymongo": Category.DATABASE_OPERATIONS,
    "redis": Category.DATABASE_OPERATIONS,
    "aiomysql": Category.DATABASE_OPERATIONS | Category.ASYNC_AND_CONCURRENCY,
    "asyncpg": Category.DATABASE_OPERATIONS | Category.ASYNC_AND_CONCURRENCY,
    "aiosqlite": Category.DATABASE_OPERATIONS | Category.ASYNC_AND_CONCURRENCY,
    "json": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "logging": Category.LOGGING_AND_TERMINAL_OUTPUT,
    "mcp": Category.IPC_RPC_AND_MCP_COMMUNICATION,
    "multiprocessing": Category.THREADING_AND_MULTIPROCESSING,
    "os": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "os.path": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "pathlib": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "psycopg2": Category.DATABASE_OPERATIONS,
    "pytest": Category.TESTING,
    "re": Category.REGEX_AND_PATTERN_MATCHING,
    "requests": Category.NETWORK_HTTP_API_CALLS,
    "shlex": Category.SUBPROCESS_AND_SHELL_COMMANDS,
    "sqlite3": Category.DATABASE_OPERATIONS,
    "subprocess": Category.SUBPROCESS_AND_SHELL_COMMANDS | Category.PROCESS_LIFECYCLE,
    "threading": Category.THREADING_AND_MULTIPROCESSING,
    "tomllib": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "tomli": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "typing": Category.TYPES_AND_SCHEMAS | Category.LINTING_FORMATTING_TYPING,
    "unittest": Category.TESTING,
    "urllib": Category.NETWORK_HTTP_API_CALLS,
    "yaml": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    # JS / TS runtime and ecosystem
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
    # JS / TS UI
    "react": Category.UI_AND_CLI_INTERFACES,
    "vue": Category.UI_AND_CLI_INTERFACES,
    "svelte": Category.UI_AND_CLI_INTERFACES,
    # JS / TS testing
    "jest": Category.TESTING | Category.MOCKING_AND_FIXTURES,
    "vitest": Category.TESTING,
    "mocha": Category.TESTING,
    # JS / TS validation
    "zod": Category.VALIDATION_AND_SANITIZATION | Category.TYPES_AND_SCHEMAS,
    "joi": Category.VALIDATION_AND_SANITIZATION,
    # Structured Text / PLC ecosystems
    "STANDARD": Category.FUNCTIONS_AND_CALLABLES | Category.CONTROL_FLOW,
    "UTIL": Category.SHARED_UTILITIES,
    "OSCAT": Category.AUTOMATION_WORKFLOWS | Category.HARDWARE_DEVICE_IO,
    "IOLINK": Category.HARDWARE_DEVICE_IO,
    "PLCopen": Category.AUTOMATION_WORKFLOWS | Category.HARDWARE_DEVICE_IO,
    # Scientific / numeric
    "sympy": Category.MATH_OPERATIONS | Category.NUMERIC_SCIENTIFIC_COMPUTING,
    "numpy": Category.NUMERIC_SCIENTIFIC_COMPUTING,
    "scipy": Category.NUMERIC_SCIENTIFIC_COMPUTING | Category.MATH_OPERATIONS,
    "pandas": Category.NUMERIC_SCIENTIFIC_COMPUTING | Category.DATA_STRUCTURES,
    "matplotlib": Category.IMAGE_PROCESSING,
    "sklearn": Category.MACHINE_LEARNING_AI | Category.NUMERIC_SCIENTIFIC_COMPUTING,
    "torch": Category.MACHINE_LEARNING_AI | Category.NUMERIC_SCIENTIFIC_COMPUTING,
    "tensorflow": Category.MACHINE_LEARNING_AI | Category.NUMERIC_SCIENTIFIC_COMPUTING,
    "cv2": Category.IMAGE_PROCESSING,
    "PIL": Category.IMAGE_PROCESSING,
}

FUNCTION_FLAG_LOOKUP: dict[str, Category] = {
    "open": Category.FILE_IO,
    "read_text": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "write_text": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "read_bytes": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "write_bytes": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "mkdir": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "rename": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "unlink": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "touch": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "glob": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "rglob": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "exists": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Thread": Category.THREADING_AND_MULTIPROCESSING,
    "Lock": Category.THREADING_AND_MULTIPROCESSING,
    "RLock": Category.THREADING_AND_MULTIPROCESSING,
    "create_task": Category.ASYNC_AND_CONCURRENCY,
    "gather": Category.ASYNC_AND_CONCURRENCY,
}

QUALIFIED_FUNCTION_FLAG_LOOKUP: dict[str, Category] = {
    "json.load": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "json.loads": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "json.dump": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "json.dumps": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "json.safe_load": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "json.safe_dump": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "yaml.load": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "yaml.safe_load": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "yaml.safe_dump": Category.JSON_YAML_TOML_CSV_PROCESSING | Category.PARSING_AND_SERIALIZATION,
    "re.compile": Category.REGEX_AND_PATTERN_MATCHING,
    "re.search": Category.REGEX_AND_PATTERN_MATCHING,
    "re.match": Category.REGEX_AND_PATTERN_MATCHING,
    "re.fullmatch": Category.REGEX_AND_PATTERN_MATCHING,
    "subprocess.run": Category.SUBPROCESS_AND_SHELL_COMMANDS | Category.PROCESS_LIFECYCLE,
    "subprocess.Popen": Category.SUBPROCESS_AND_SHELL_COMMANDS | Category.PROCESS_LIFECYCLE,
    "requests.get": Category.NETWORK_HTTP_API_CALLS,
    "requests.post": Category.NETWORK_HTTP_API_CALLS,
    "requests.put": Category.NETWORK_HTTP_API_CALLS,
    "requests.delete": Category.NETWORK_HTTP_API_CALLS,
    "requests.patch": Category.NETWORK_HTTP_API_CALLS,
    "httpx.get": Category.NETWORK_HTTP_API_CALLS,
    "httpx.post": Category.NETWORK_HTTP_API_CALLS,
    "httpx.put": Category.NETWORK_HTTP_API_CALLS,
    "httpx.delete": Category.NETWORK_HTTP_API_CALLS,
    "httpx.patch": Category.NETWORK_HTTP_API_CALLS,
}

METHOD_FLAG_LOOKUP: dict[str, Category] = {
    "Path.open": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.read_text": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.write_text": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.read_bytes": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.write_bytes": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.exists": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.mkdir": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.rename": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.unlink": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.glob": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "Path.rglob": Category.FILE_IO | Category.PATH_AND_FILESYSTEM_OPERATIONS,
    "logger.info": Category.LOGGING_AND_TERMINAL_OUTPUT,
    "logger.warning": Category.LOGGING_AND_TERMINAL_OUTPUT,
    "logger.error": Category.LOGGING_AND_TERMINAL_OUTPUT,
    "logger.debug": Category.LOGGING_AND_TERMINAL_OUTPUT | Category.DEBUGGING_AND_DIAGNOSTICS,
}

# Backwards-compatible aliases for older internal names.
_MODULE_RULES = LIBRARY_FLAG_LOOKUP
_CALL_RULES = list(FUNCTION_FLAG_LOOKUP.items())
_QUALIFIED_CALL_RULES = list(QUALIFIED_FUNCTION_FLAG_LOOKUP.items())
_METHOD_RULES = list(METHOD_FLAG_LOOKUP.items())


@dataclass(slots=True)
class FileSuffixResult:
    """Summarize inferred suffix flags for a source file."""

    flags: Category
    modules: list[str]
    calls: list[str]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = [node.attr]
        current = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    try:
        return ast.unparse(node)
    except Exception:
        return "<unknown>"


class _SuffixVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.modules: set[str] = set()
        self.calls: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.modules.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.modules.add(node.module)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.add(_call_name(node.func))
        self.generic_visit(node)


def _apply_module_rules(modules: Iterable[str]) -> Category:
    flags = Category(0)
    for module in modules:
        module_root = module.split(".")[0]
        flags |= LIBRARY_FLAG_LOOKUP.get(module, Category(0))
        flags |= LIBRARY_FLAG_LOOKUP.get(module_root, Category(0))
    return flags


def _apply_call_rules(calls: Iterable[str]) -> Category:
    flags = Category(0)
    for call in calls:
        call_root = call.split(".")[0]
        for pattern, category in FUNCTION_FLAG_LOOKUP.items():
            if call == pattern or call_root == pattern:
                flags |= category
        for pattern, category in QUALIFIED_FUNCTION_FLAG_LOOKUP.items():
            if call == pattern or call.endswith("." + pattern):
                flags |= category
        for pattern, category in METHOD_FLAG_LOOKUP.items():
            if call == pattern or call.endswith("." + pattern):
                flags |= category
    return flags


def infer_file_suffix_result(
    source: str,
    modules: Iterable[str] | None = None,
    calls: Iterable[str] | None = None,
) -> FileSuffixResult:
    """Infer suffix flags from imports and function calls in a source string."""
    if modules is not None or calls is not None:
        module_values = sorted(set(modules or []))
        call_values = sorted(set(calls or []))
        flags = _apply_module_rules(module_values) | _apply_call_rules(call_values)
        if flags == Category(0):
            flags = Category.MISCELLANEOUS
        return FileSuffixResult(flags=flags, modules=module_values, calls=call_values)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FileSuffixResult(flags=Category.MISCELLANEOUS, modules=[], calls=[])

    visitor = _SuffixVisitor()
    visitor.visit(tree)
    flags = _apply_module_rules(visitor.modules) | _apply_call_rules(visitor.calls)
    return FileSuffixResult(flags=flags, modules=sorted(visitor.modules), calls=sorted(visitor.calls))


def infer_file_suffix_flags(source: str) -> Category:
    """Return the category bitmask inferred from a Python source string."""
    return infer_file_suffix_result(source).flags


def suffix_number(flags: Category) -> int:
    """Return the numeric suffix value for a category bitmask."""
    return int(flags)


def category_from_name(name: str) -> Category:
    """Resolve a category enum member by name or raise ValueError."""
    try:
        return Category[name]
    except KeyError as exc:
        raise ValueError(f"Unknown category flag: {name}") from exc


def category_names(flags: Category) -> list[str]:
    """Return the active category names for a bitmask in sorted enum order."""
    return [category.name for category in Category if category and category & flags]


def split_numeric_suffix(stem: str) -> tuple[str, int | None]:
    """Split a trailing numeric suffix from a stem when present."""
    match = re.match(r"^(?P<base>.+?)_(?P<num>\d+)$", stem)
    if match is None:
        return stem, None
    return match.group("base"), int(match.group("num"))


def apply_numeric_suffix(stem: str, flags: Category) -> str:
    """Append or replace the numeric suffix on a filename stem."""
    value = suffix_number(flags)
    if value <= 0:
        return split_numeric_suffix(stem)[0]
    base, _ = split_numeric_suffix(stem)
    return f"{base}_{value}"


def filename_with_suffix(name: str, flags: Category) -> str:
    """Return a filename with a numeric category suffix before the extension."""
    path = Path(name)
    return apply_numeric_suffix(path.stem, flags) + path.suffix


def flags_from_filename(name: str) -> Category:
    """Decode a numeric suffix from a filename into the corresponding category bitmask."""
    stem = Path(name).stem
    _, value = split_numeric_suffix(stem)
    if value is None:
        return Category(0)
    return Category(value)


def decode_filename_suffix(name: str) -> dict[str, object]:
    """Decode a filename into its numeric suffix, categories, and base stem."""
    path = Path(name)
    base_stem, value = split_numeric_suffix(path.stem)
    flags = Category(value or 0)
    return {
        "name": path.name,
        "base_name": base_stem + path.suffix,
        "stem": base_stem,
        "value": value or 0,
        "flags": int(flags),
        "categories": category_names(flags),
    }
