"""Utility helpers for intern tracking."""

from __future__ import annotations

import hashlib
import time
from typing import Any


def now_ts() -> float:
    """Return the current UTC timestamp in seconds."""
    return time.time()


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    """Clamp a numeric value to a bounded range."""
    return max(lower, min(upper, value))


def safe_int(value: Any, default: int = 0) -> int:
    """Convert a value to int without raising on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compact_range(start_line: int, start_col: int, end_line: int, end_col: int) -> str:
    """Format a line anchor using compact start:end notation."""
    return f"{start_line}:{start_col}-{end_line}:{end_col}"


def checksum_text(text: str) -> str:
    """Return a short sha256 checksum for text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def velocity_per_hour(count: int, first_seen: float, last_seen: float) -> float:
    """Compute an activity velocity from a count and time span."""
    if count <= 0:
        return 0.0
    span_hours = max((last_seen - first_seen) / 3600.0, 1 / 60.0)
    return count / span_hours


def volatility_score(count: int, first_seen: float, last_seen: float, window_hours: float) -> float:
    """Compute a compact volatility score from edit density and recency."""
    if count <= 0:
        return 0.0
    density = min(1.0, count / max(window_hours * 2.0, 1.0))
    recency = clamp(1.0 / max((now_ts() - last_seen) / 3600.0 + 1.0, 1.0))
    spread = clamp((last_seen - first_seen) / max(window_hours * 3600.0, 1.0))
    return clamp(0.55 * density + 0.25 * recency + 0.20 * spread)


def mean(values: list[float]) -> float:
    """Return the arithmetic mean for a non-empty list."""
    if not values:
        return 0.0
    return sum(values) / len(values)
