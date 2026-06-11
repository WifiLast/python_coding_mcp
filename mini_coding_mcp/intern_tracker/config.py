"""Configuration defaults and helpers for intern tracking."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_INTERN_CONFIG: dict[str, Any] = {
    "enabled": True,
    "retention_hours": 24,
    "hint_verbosity": "medium",
    "enable_frequency_hints": True,
    "enable_pattern_hints": True,
    "enable_testing_hints": True,
    "enable_refactor_hints": True,
    "enable_risk_hints": True,
    "min_co_edits_for_pattern": 2,
    "co_mutation_time_window_seconds": 300,
    "enabled_operations": {
        "insert_code": True,
        "replace_symbol": True,
        "patch_symbol": True,
        "rename_symbol": True,
        "scaffold_module": True,
    },
}


def merged_intern_config(base: dict[str, Any] | None = None, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge intern configuration dictionaries without mutating inputs."""
    merged = deepcopy(DEFAULT_INTERN_CONFIG)
    for source in (base or {}, updates or {}):
        for key, value in source.items():
            if key == "enabled_operations" and isinstance(value, dict):
                merged.setdefault("enabled_operations", {})
                merged["enabled_operations"].update({str(op): bool(flag) for op, flag in value.items()})
                continue
            merged[key] = value
    return merged

