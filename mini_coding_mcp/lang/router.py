from __future__ import annotations

from pathlib import Path

from .adapter import LanguageAdapter
from .iec61131_adapter import IEC61131Adapter
from .javascript_adapter import JavaScriptAdapter
from .python_adapter import PythonAdapter

PYTHON_ADAPTER = PythonAdapter()
JAVASCRIPT_ADAPTER = JavaScriptAdapter()
IEC61131_ADAPTER = IEC61131Adapter()

_ADAPTERS: dict[str, LanguageAdapter] = {
    ".py": PYTHON_ADAPTER,
    ".js": JAVASCRIPT_ADAPTER,
    ".mjs": JAVASCRIPT_ADAPTER,
    ".cjs": JAVASCRIPT_ADAPTER,
    ".ts": JAVASCRIPT_ADAPTER,
    ".tsx": JAVASCRIPT_ADAPTER,
    ".jsx": JAVASCRIPT_ADAPTER,
    ".st": IEC61131_ADAPTER,
    ".scl": IEC61131_ADAPTER,
    ".iec": IEC61131_ADAPTER,
}


def adapter_for(path: Path) -> LanguageAdapter | None:
    return _ADAPTERS.get(path.suffix.lower())


def supported_extensions() -> frozenset[str]:
    return frozenset(_ADAPTERS)
