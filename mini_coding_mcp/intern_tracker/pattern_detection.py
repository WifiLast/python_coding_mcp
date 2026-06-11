"""Pattern detection helpers for intern tracking."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import ActivityPattern, SymbolActivity
from .utils import clamp


def detect_cycles(activities: list[SymbolActivity], max_span_seconds: int = 900) -> list[ActivityPattern]:
    """Detect simple A→B→C→A style edit cycles."""
    ordered = sorted(activities, key=lambda item: item.timestamp)
    cycles: list[ActivityPattern] = []
    for start_index, start in enumerate(ordered):
        path = [start.symbol]
        seen = {start.symbol}
        for current in ordered[start_index + 1 :]:
            if current.timestamp - start.timestamp > max_span_seconds:
                break
            if current.symbol in seen:
                if current.symbol == start.symbol and len(path) >= 3:
                    cycles.append(
                        ActivityPattern(
                            kind="cycle",
                            symbols=path + [start.symbol],
                            strength=clamp(len(path) / 5.0),
                            count=len(path),
                            last_seen=current.timestamp,
                            metadata={"max_span_seconds": max_span_seconds},
                        )
                    )
                break
            seen.add(current.symbol)
            path.append(current.symbol)
    return cycles


def detect_hotspots(activities: list[SymbolActivity], min_callsites: int = 20) -> list[dict[str, Any]]:
    """Return activity hotspots ranked by edit volume."""
    counts: dict[str, int] = defaultdict(int)
    for activity in activities:
        counts[activity.symbol] += 1
    hotspots = [
        {
            "symbol": symbol,
            "edits": edits,
            "severity": "high" if edits >= min_callsites else "medium",
        }
        for symbol, edits in counts.items()
        if edits >= 2
    ]
    hotspots.sort(key=lambda item: (-item["edits"], item["symbol"]))
    return hotspots


def detect_similarity_groups(symbols: list[str]) -> list[list[str]]:
    """Group symbols by shared prefixes for refactoring hints."""
    groups: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols:
        prefix = symbol.split("_", 1)[0]
        groups[prefix].append(symbol)
    return [sorted(items) for items in groups.values() if len(items) >= 3]

