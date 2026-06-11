"""Integration helpers for app-level intern tracking hooks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .activity_store import InternActivityStore
from .config import DEFAULT_INTERN_CONFIG, merged_intern_config
from .dashboard import build_dashboard
from .hint_engine import HintEngine


class InternTrackingIntegration:
    """Bridge workspace edits, persistence, and hint generation."""

    def __init__(self, root: Path, workspace: Any, module_plan: dict[str, Any] | None = None) -> None:
        """Initialize intern tracking against the active workspace."""
        self.root = root.resolve()
        self.workspace = workspace
        self.module_plan = module_plan if module_plan is not None else {}
        self.store = InternActivityStore(self.root, module_plan=self.module_plan, config=self.module_plan.get("_intern_config", {}))
        self.config = merged_intern_config(DEFAULT_INTERN_CONFIG, self.store.config)
        self.hint_engine = HintEngine(self.store, self.workspace, self.config)

    def refresh(self) -> None:
        """Refresh configuration and collaborators from the current plan."""
        self.store = InternActivityStore(self.root, module_plan=self.module_plan, config=self.module_plan.get("_intern_config", {}))
        self.config = merged_intern_config(DEFAULT_INTERN_CONFIG, self.store.config)
        self.hint_engine = HintEngine(self.store, self.workspace, self.config)

    def configure(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Update the tracking configuration and persist it."""
        self.config = self.store.configure(updates)
        self.hint_engine.config = self.config
        self.module_plan["_intern_config"] = dict(self.config)
        return dict(self.config)

    def enabled_for(self, operation: str) -> bool:
        """Return whether a given write operation should be tracked."""
        return self.store.enabled_for(operation)

    def _persist(self) -> None:
        """Persist the current module plan through the store."""
        self.store.persist()

    def record_operation(
        self,
        qnames: list[str],
        operation: str,
        file_path: str,
        lines_changed: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an edit operation and return contextual hints."""
        if not self.enabled_for(operation):
            return {"enabled": False, "hints": []}
        recorded: list[str] = []
        for qname in dict.fromkeys([item for item in qnames if item]):
            self.store.record_activity(
                symbol=qname,
                operation=operation,
                file_path=file_path,
                lines_changed=lines_changed,
                related_symbols=[item for item in qnames if item and item != qname],
                metadata=metadata or {},
            )
            recorded.append(qname)
        self._persist()
        hints = self.generate_hints(
            {
                "qnames": recorded,
                "qname": recorded[0] if recorded else None,
                "operation": operation,
                "file_path": file_path,
                "lines_changed": lines_changed,
                "metadata": metadata or {},
            }
        )
        return {"enabled": True, "recorded": recorded, **hints}

    def generate_hints(self, context: dict[str, Any]) -> dict[str, Any]:
        """Generate hints for an operation context."""
        return self.hint_engine.generate_hints(context)

    def attach_to_result(
        self,
        result: dict[str, Any],
        operation: str,
        qnames: list[str],
        file_path: str,
        lines_changed: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an operation and attach hints to the result payload."""
        payload = self.record_operation(qnames, operation, file_path, lines_changed=lines_changed, metadata=metadata)
        if payload.get("hints"):
            result["intern_hints"] = payload
        return result

    def dashboard(self, hours: float = 24.0) -> dict[str, Any]:
        """Return the current intern tracking dashboard."""
        return build_dashboard(self.store, self.config, hours=hours)

