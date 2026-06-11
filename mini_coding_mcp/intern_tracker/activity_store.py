"""Persistent activity storage for intern tracking."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from .config import DEFAULT_INTERN_CONFIG, merged_intern_config
from .models import ActivityPattern, SymbolActivity, SymbolProfile
from .utils import now_ts, velocity_per_hour, volatility_score, clamp


class InternActivityStore:
    """Store and query symbol activity in the workspace plan file."""

    def __init__(self, root: Path, module_plan: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> None:
        """Initialize the store from an existing workspace plan."""
        self.root = root.resolve()
        self.plan_path = self.root / ".mcp_plan.json"
        self._lock = RLock()
        self.module_plan = module_plan if module_plan is not None else self._load_plan()
        self.config = merged_intern_config(DEFAULT_INTERN_CONFIG, config or self.module_plan.get("_intern_config", {}))
        self.module_plan.setdefault("_intern_config", {})
        self.module_plan["_intern_config"].update(self.config)
        self.module_plan.setdefault("_intern_tracking", {})
        self._state = self.module_plan["_intern_tracking"]
        self._state.setdefault("version", 1)
        self._state.setdefault("activities", [])
        self._state.setdefault("profiles", {})
        self._state.setdefault("patterns", [])
        self._state.setdefault("stats", {"total_operations": 0, "total_symbols_edited": 0})
        self._state.setdefault("last_pruned_at", 0.0)
        self._state.setdefault("recent_files", {})
        self.prune()

    def _load_plan(self) -> dict[str, Any]:
        """Load the persisted plan file if it exists."""
        if not self.plan_path.exists():
            return {"_ignored": set()}
        try:
            payload = json.loads(self.plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"_ignored": set()}
        if isinstance(payload, dict):
            return payload
        return {"_ignored": set()}

    def persist(self) -> None:
        """Write the current module plan to disk."""
        with self._lock:
            payload: dict[str, Any] = {}
            for key, value in self.module_plan.items():
                if key == "_ignored":
                    if isinstance(value, set):
                        payload[key] = sorted(str(item) for item in value)
                    elif isinstance(value, list):
                        payload[key] = sorted(str(item) for item in value)
                    else:
                        payload[key] = []
                    continue
                payload[key] = value
            self.plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def configure(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Update the intern configuration in place."""
        with self._lock:
            self.config = merged_intern_config(self.config, updates)
            self.module_plan["_intern_config"] = dict(self.config)
            self.persist()
            return dict(self.config)

    def enabled_for(self, operation: str) -> bool:
        """Return whether intern tracking should record a specific operation."""
        if not self.config.get("enabled", True):
            return False
        enabled_operations = self.config.get("enabled_operations", {})
        if isinstance(enabled_operations, dict):
            return bool(enabled_operations.get(operation, True))
        return True

    def _activities(self) -> list[SymbolActivity]:
        """Return the tracked activities as model objects."""
        return [SymbolActivity.from_dict(item) for item in self._state.get("activities", []) if isinstance(item, dict)]

    def prune(self) -> int:
        """Remove activities that exceed the retention window."""
        with self._lock:
            retention_hours = float(self.config.get("retention_hours", 24) or 24)
            cutoff = now_ts() - retention_hours * 3600.0
            activities = [activity for activity in self._activities() if activity.timestamp >= cutoff]
            removed = len(self._state.get("activities", [])) - len(activities)
            self._state["activities"] = [activity.to_dict() for activity in activities]
            self._state["last_pruned_at"] = now_ts()
            self._rebuild_profiles_locked(activities)
            self._rebuild_patterns_locked(activities)
            return max(removed, 0)

    def _rebuild_profiles_locked(self, activities: list[SymbolActivity]) -> None:
        """Recompute symbol profiles from retained activities."""
        profiles: dict[str, SymbolProfile] = {}
        by_symbol: dict[str, list[SymbolActivity]] = {}
        for activity in activities:
            by_symbol.setdefault(activity.symbol, []).append(activity)
        for symbol, items in by_symbol.items():
            first_seen = min(item.timestamp for item in items)
            last_seen = max(item.timestamp for item in items)
            profile = SymbolProfile(
                symbol=symbol,
                edit_count=len(items),
                first_seen=first_seen,
                last_seen=last_seen,
                velocity_per_hour=velocity_per_hour(len(items), first_seen, last_seen),
                volatility=volatility_score(len(items), first_seen, last_seen, float(self.config.get("retention_hours", 24) or 24)),
                callsites=int(self.module_plan.get("_intern_tracking", {}).get("profiles", {}).get(symbol, {}).get("callsites", 0) or 0),
                related_symbols=sorted({item for activity in items for item in activity.related_symbols if item != symbol}),
                operations=sorted({item.operation for item in items}),
            )
            profiles[symbol] = profile
        self._state["profiles"] = {symbol: profile.to_dict() for symbol, profile in profiles.items()}

    def _rebuild_patterns_locked(self, activities: list[SymbolActivity]) -> None:
        """Recompute lightweight co-edit patterns from recent activities."""
        patterns: list[ActivityPattern] = []
        window_seconds = int(self.config.get("co_mutation_time_window_seconds", 300) or 300)
        min_edits = int(self.config.get("min_co_edits_for_pattern", 2) or 2)
        co_counts: dict[tuple[str, str], list[float]] = {}
        ordered = sorted(activities, key=lambda item: item.timestamp)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if right.timestamp - left.timestamp > window_seconds:
                    break
                pair = tuple(sorted({left.symbol, right.symbol}))
                if len(pair) < 2:
                    continue
                co_counts.setdefault(pair, []).append(right.timestamp)
        for pair, timestamps in co_counts.items():
            if len(timestamps) < min_edits:
                continue
            patterns.append(
                ActivityPattern(
                    kind="co_mutation",
                    symbols=list(pair),
                    strength=clamp(len(timestamps) / max(len(activities), 1)),
                    count=len(timestamps),
                    last_seen=max(timestamps),
                    metadata={"window_seconds": window_seconds},
                )
            )
        self._state["patterns"] = [pattern.to_dict() for pattern in patterns]

    def record_activity(
        self,
        symbol: str,
        operation: str,
        file_path: str,
        lines_changed: int = 0,
        related_symbols: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> SymbolActivity:
        """Append a symbol activity and persist the updated plan."""
        with self._lock:
            activity = SymbolActivity(
                symbol=symbol,
                operation=operation,
                file_path=file_path,
                timestamp=timestamp if timestamp is not None else now_ts(),
                lines_changed=max(int(lines_changed or 0), 0),
                related_symbols=[item for item in (related_symbols or []) if item],
                metadata=dict(metadata or {}),
            )
            activities = self._activities()
            activities.append(activity)
            self._state["activities"] = [item.to_dict() for item in activities]
            self._state["stats"]["total_operations"] = int(self._state["stats"].get("total_operations", 0) or 0) + 1
            if activity.symbol:
                self._state["stats"]["total_symbols_edited"] = len({item.symbol for item in activities if item.symbol})
            profile = self.profile_for(activity.symbol, activities=activities)
            self._state.setdefault("profiles", {})[activity.symbol] = profile.to_dict()
            self._rebuild_patterns_locked(activities)
            self._state["last_pruned_at"] = now_ts()
            self.persist()
            return activity

    def recent_activities(self, hours: float = 24.0, symbol: str | None = None) -> list[SymbolActivity]:
        """Return activities from the last N hours, optionally filtered by symbol."""
        with self._lock:
            cutoff = now_ts() - max(hours, 0.0) * 3600.0
            activities = [activity for activity in self._activities() if activity.timestamp >= cutoff]
            if symbol:
                activities = [activity for activity in activities if activity.symbol == symbol]
            return sorted(activities, key=lambda item: item.timestamp)

    def profile_for(self, symbol: str, activities: list[SymbolActivity] | None = None) -> SymbolProfile:
        """Return an aggregate profile for a symbol."""
        with self._lock:
            cached = self._state.get("profiles", {}).get(symbol)
            if activities is None and isinstance(cached, dict):
                return SymbolProfile.from_dict(cached)
            source = activities if activities is not None else self._activities()
            filtered = [activity for activity in source if activity.symbol == symbol]
            if not filtered:
                return SymbolProfile(symbol=symbol)
            first_seen = min(item.timestamp for item in filtered)
            last_seen = max(item.timestamp for item in filtered)
            profile = SymbolProfile(
                symbol=symbol,
                edit_count=len(filtered),
                first_seen=first_seen,
                last_seen=last_seen,
                velocity_per_hour=velocity_per_hour(len(filtered), first_seen, last_seen),
                volatility=volatility_score(len(filtered), first_seen, last_seen, float(self.config.get("retention_hours", 24) or 24)),
                callsites=int(cached.get("callsites", 0) if isinstance(cached, dict) else 0),
                related_symbols=sorted({item for activity in filtered for item in activity.related_symbols if item != symbol}),
                operations=sorted({item.operation for item in filtered}),
            )
            return profile

    def set_symbol_callsites(self, symbol: str, callsites: int) -> None:
        """Cache a symbol's callsite count for risk scoring."""
        with self._lock:
            profiles = self._state.setdefault("profiles", {})
            profile = profiles.get(symbol, {})
            if not isinstance(profile, dict):
                profile = {}
            profile["symbol"] = symbol
            profile["callsites"] = max(int(callsites or 0), 0)
            profiles[symbol] = profile
            self.persist()

    def co_mutations(self, window_seconds: int | None = None, min_edits: int | None = None) -> list[ActivityPattern]:
        """Return co-edit patterns from the retained activity window."""
        with self._lock:
            window_seconds = int(window_seconds or self.config.get("co_mutation_time_window_seconds", 300) or 300)
            min_edits = int(min_edits or self.config.get("min_co_edits_for_pattern", 2) or 2)
            activities = self._activities()
            ordered = sorted(activities, key=lambda item: item.timestamp)
            pair_hits: dict[tuple[str, str], list[float]] = {}
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    if right.timestamp - left.timestamp > window_seconds:
                        break
                    if not left.symbol or not right.symbol:
                        continue
                    pair = tuple(sorted({left.symbol, right.symbol}))
                    if len(pair) < 2:
                        continue
                    pair_hits.setdefault(pair, []).append(right.timestamp)
            patterns: list[ActivityPattern] = []
            for pair, timestamps in pair_hits.items():
                if len(timestamps) < min_edits:
                    continue
                patterns.append(
                    ActivityPattern(
                        kind="co_mutation",
                        symbols=list(pair),
                        strength=clamp(len(timestamps) / max(len(activities), 1)),
                        count=len(timestamps),
                        last_seen=max(timestamps),
                        metadata={"window_seconds": window_seconds},
                    )
                )
            patterns.sort(key=lambda item: (-item.count, item.last_seen, item.symbols))
            return patterns

    def symbol_activity_count(self, symbol: str, hours: float = 24.0) -> int:
        """Return the number of edits for one symbol in a time window."""
        return len(self.recent_activities(hours=hours, symbol=symbol))

    def summary(self, hours: float = 24.0) -> dict[str, Any]:
        """Build a compact tracking summary for dashboards."""
        activities = self.recent_activities(hours=hours)
        by_symbol: dict[str, int] = {}
        for activity in activities:
            by_symbol[activity.symbol] = by_symbol.get(activity.symbol, 0) + 1
        most_active = sorted(
            (
                {
                    "symbol": symbol,
                    "edits": edits,
                    "velocity_per_hour": self.profile_for(symbol).velocity_per_hour,
                    "volatility": self.profile_for(symbol).volatility,
                }
                for symbol, edits in by_symbol.items()
            ),
            key=lambda item: (-item["edits"], item["symbol"]),
        )
        return {
            "window_hours": hours,
            "total_symbols_edited": len(by_symbol),
            "total_operations": len(activities),
            "detected_patterns": len(self.co_mutations()),
            "most_active_symbols": most_active[:10],
        }
