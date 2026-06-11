"""Dashboard helpers for intern tracking."""

from __future__ import annotations

from typing import Any


def build_dashboard(store: Any, config: dict[str, Any], hours: float = 24.0) -> dict[str, Any]:
    """Return a compact dashboard payload for the current workspace."""
    summary = store.summary(hours=hours)
    summary["config"] = {
        "enabled": config.get("enabled", True),
        "retention_hours": config.get("retention_hours", 24),
        "hint_verbosity": config.get("hint_verbosity", "medium"),
    }
    summary["most_active_symbols"] = summary.get("most_active_symbols", [])
    return summary

