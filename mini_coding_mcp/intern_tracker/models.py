"""Data models for intern tracking state and hints."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class SymbolActivity:
    """Represent one tracked edit or write operation for a symbol."""

    symbol: str
    operation: str
    file_path: str
    timestamp: float
    lines_changed: int = 0
    related_symbols: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the activity into JSON-compatible form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SymbolActivity":
        """Construct an activity from persisted JSON data."""
        return cls(
            symbol=str(payload.get("symbol", "")),
            operation=str(payload.get("operation", "")),
            file_path=str(payload.get("file_path", "")),
            timestamp=float(payload.get("timestamp", 0.0)),
            lines_changed=int(payload.get("lines_changed", 0) or 0),
            related_symbols=[str(item) for item in payload.get("related_symbols", []) if isinstance(item, str)],
            metadata=dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {},
        )


@dataclass(slots=True)
class SymbolProfile:
    """Summarize symbol-level activity and volatility."""

    symbol: str
    edit_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    velocity_per_hour: float = 0.0
    volatility: float = 0.0
    callsites: int = 0
    related_symbols: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the profile into JSON-compatible form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SymbolProfile":
        """Construct a profile from persisted JSON data."""
        return cls(
            symbol=str(payload.get("symbol", "")),
            edit_count=int(payload.get("edit_count", 0) or 0),
            first_seen=float(payload.get("first_seen", 0.0)),
            last_seen=float(payload.get("last_seen", 0.0)),
            velocity_per_hour=float(payload.get("velocity_per_hour", 0.0) or 0.0),
            volatility=float(payload.get("volatility", 0.0) or 0.0),
            callsites=int(payload.get("callsites", 0) or 0),
            related_symbols=[str(item) for item in payload.get("related_symbols", []) if isinstance(item, str)],
            operations=[str(item) for item in payload.get("operations", []) if isinstance(item, str)],
        )


@dataclass(slots=True)
class ActivityPattern:
    """Describe a repeated co-edit or cyclic pattern."""

    kind: str
    symbols: list[str]
    strength: float
    count: int
    last_seen: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the pattern into JSON-compatible form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActivityPattern":
        """Construct a pattern from persisted JSON data."""
        return cls(
            kind=str(payload.get("kind", "")),
            symbols=[str(item) for item in payload.get("symbols", []) if isinstance(item, str)],
            strength=float(payload.get("strength", 0.0) or 0.0),
            count=int(payload.get("count", 0) or 0),
            last_seen=float(payload.get("last_seen", 0.0)),
            metadata=dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {},
        )


@dataclass(slots=True)
class RiskAssessment:
    """Capture a symbol's risk score and supporting context."""

    symbol: str
    score: float
    severity: str
    callsites: int
    edit_count: int
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the risk assessment into JSON-compatible form."""
        return asdict(self)


@dataclass(slots=True)
class InternHint:
    """Represent one contextual hint emitted by the intern layer."""

    type: str
    severity: str
    symbol: str | None
    message: str
    action: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the hint into JSON-compatible form."""
        return asdict(self)

