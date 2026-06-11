"""Hint generation for intern tracking."""

from __future__ import annotations

from typing import Any

from .models import InternHint, SymbolProfile
from .pattern_detection import detect_cycles, detect_similarity_groups
from .utils import clamp


class HintEngine:
    """Generate contextual edit hints from activity history."""

    def __init__(self, store: Any, workspace: Any, config: dict[str, Any]) -> None:
        """Initialize the engine with its backing store and workspace."""
        self.store = store
        self.workspace = workspace
        self.config = config

    def _build_hint(self, hint_type: str, severity: str, symbol: str | None, message: str, action: str, metadata: dict[str, Any] | None = None) -> InternHint:
        """Create a structured hint object."""
        return InternHint(
            type=hint_type,
            severity=severity,
            symbol=symbol,
            message=message,
            action=action,
            metadata=dict(metadata or {}),
        )

    def _symbol_profile(self, symbol: str) -> SymbolProfile:
        """Fetch a symbol profile from the store."""
        return self.store.profile_for(symbol)

    def _frequent_edit_hint(self, symbol: str) -> InternHint | None:
        """Generate a frequent-edit hint for a symbol."""
        if not self.config.get("enable_frequency_hints", True):
            return None
        profile = self._symbol_profile(symbol)
        if profile.edit_count < 3:
            return None
        hours = max((profile.last_seen - profile.first_seen) / 3600.0, 1 / 60.0)
        message = (
            f"{symbol} was edited {profile.edit_count} times in {hours:.1f} hours. "
            "Consider a comprehensive refactor instead of another incremental edit."
        )
        severity = "high" if profile.edit_count >= 5 else "medium"
        return self._build_hint(
            "frequent_edits",
            severity,
            symbol,
            message,
            "review_implementation",
            {"edit_count": profile.edit_count, "window_hours": round(hours, 2)},
        )

    def _co_mutation_hint(self, symbol: str) -> InternHint | None:
        """Generate a co-mutation hint for a symbol."""
        if not self.config.get("enable_pattern_hints", True):
            return None
        patterns = self.store.co_mutations()
        for pattern in patterns:
            if symbol not in pattern.symbols:
                continue
            other = next((item for item in pattern.symbols if item != symbol), None)
            if other is None:
                continue
            message = (
                f"Pattern: {symbol} and {other} are often edited together. "
                "Consider testing their interaction."
            )
            return self._build_hint(
                "co_mutation",
                "medium" if pattern.strength < 0.5 else "high",
                symbol,
                message,
                "test_interaction",
                pattern.to_dict(),
            )
        return None

    def _risk_hint(self, symbol: str) -> InternHint | None:
        """Generate a high-risk hint from edit volume and callsites."""
        if not self.config.get("enable_risk_hints", True):
            return None
        profile = self._symbol_profile(symbol)
        callsites = profile.callsites
        if callsites <= 0 and hasattr(self.workspace, "find_callers"):
            callers = self.workspace.find_callers(symbol, limit=100)
            callsites = int(callers.get("count", 0) or 0)
            self.store.set_symbol_callsites(symbol, callsites)
            profile = self._symbol_profile(symbol)
        if profile.edit_count < 4 or callsites < 10:
            return None
        risk = clamp((profile.edit_count / 8.0) + (callsites / 50.0))
        severity = "high" if risk >= 0.7 else "medium"
        message = (
            f"{symbol} shows high volatility ({profile.edit_count} edits, {callsites} callsites). "
            "Ensure comprehensive test coverage."
        )
        return self._build_hint(
            "high_risk",
            severity,
            symbol,
            message,
            "run_tests",
            {"callsites": callsites, "edit_count": profile.edit_count, "risk": round(risk, 3)},
        )

    def _testing_hint(self, symbol: str) -> InternHint | None:
        """Generate a test execution hint for the touched symbol."""
        if not self.config.get("enable_testing_hints", True):
            return None
        profile = self._symbol_profile(symbol)
        if profile.edit_count == 0:
            return None
        message = f"Consider running tests for {symbol} after the edit."
        return self._build_hint(
            "testing",
            "low" if profile.edit_count < 4 else "medium",
            symbol,
            message,
            "run_tests",
            {"edit_count": profile.edit_count},
        )

    def _refactor_hint(self, symbol: str) -> InternHint | None:
        """Generate a refactoring hint from similar symbol names."""
        if not self.config.get("enable_refactor_hints", True):
            return None
        namespace = symbol.rsplit(":", 1)[0]
        if not namespace:
            return None
        recent_symbols = [activity.symbol for activity in self.store.recent_activities(hours=24.0)]
        if symbol not in recent_symbols:
            recent_symbols.append(symbol)
        groups = detect_similarity_groups(recent_symbols)
        for items in groups:
            if symbol not in items:
                continue
            message = (
                f"{', '.join(items[:3])} follow a similar pattern. "
                "Consider extracting a shared helper or factory."
            )
            return self._build_hint(
                "refactor_opportunity",
                "medium",
                symbol,
                message,
                "extract_common_logic",
                {"symbols": items},
            )
        return None

    def _pattern_hints(self, symbol: str) -> list[InternHint]:
        """Generate hints derived from repeated patterns."""
        hints: list[InternHint] = []
        for activity_pattern in detect_cycles(self.store.recent_activities(hours=24.0)):
            if symbol in activity_pattern.symbols:
                hints.append(
                    self._build_hint(
                        "cycle",
                        "medium",
                        symbol,
                        f"Detected a recurring edit cycle involving {', '.join(activity_pattern.symbols)}.",
                        "review_cycle",
                        activity_pattern.to_dict(),
                    )
                )
        return hints

    def generate_hints(self, context: dict[str, Any]) -> dict[str, Any]:
        """Generate a deduplicated hint payload for a write operation."""
        if not self.config.get("enabled", True):
            return {"hints": [], "message": "Intern tracking disabled."}
        qnames = [str(item) for item in context.get("qnames", []) if item]
        if not qnames and context.get("qname"):
            qnames = [str(context["qname"])]
        if not qnames:
            return {"hints": [], "message": "No tracked symbols in context."}

        hints: list[InternHint] = []
        for symbol in dict.fromkeys(qnames):
            for hint_factory in (
                self._frequent_edit_hint,
                self._co_mutation_hint,
                self._risk_hint,
                self._testing_hint,
                self._refactor_hint,
            ):
                hint = hint_factory(symbol)
                if hint is not None:
                    hints.append(hint)
            hints.extend(self._pattern_hints(symbol))

        deduped: list[InternHint] = []
        seen: set[tuple[str, str | None, str]] = set()
        for hint in hints:
            key = (hint.type, hint.symbol, hint.message)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(hint)
        verbosity = str(self.config.get("hint_verbosity", "medium"))
        if verbosity == "minimal":
            deduped = deduped[:2]
        elif verbosity == "medium":
            deduped = deduped[:4]
        return {
            "hints": [hint.to_dict() for hint in deduped],
            "message": "Intern observations during this operation",
        }
