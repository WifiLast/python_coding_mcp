from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

_TTL = 30 * 60  # seconds


@dataclass
class WorkingSetEntry:
    qname: str
    content_hash: str
    shown_at: float
    show_count: int = 0


class WorkingSet:
    def __init__(self, ttl: float = _TTL) -> None:
        self._ttl = ttl
        self._entries: dict[str, WorkingSetEntry] = {}

    def _hash(self, body: str) -> str:
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [q for q, e in self._entries.items() if now - e.shown_at > self._ttl]
        for q in expired:
            del self._entries[q]

    def check(self, qname: str, body: str) -> tuple[str, str | None]:
        """Return (mode, note): mode is 'full', 'stub', or 'updated'."""
        self._evict_expired()
        h = self._hash(body)
        entry = self._entries.get(qname)
        if entry is None:
            return "full", None
        if entry.content_hash == h:
            return "stub", None
        return "updated", "updated since last shown"

    def record(self, qname: str, body: str) -> None:
        h = self._hash(body)
        entry = self._entries.get(qname)
        if entry is None:
            self._entries[qname] = WorkingSetEntry(qname=qname, content_hash=h, shown_at=time.monotonic(), show_count=1)
        else:
            entry.content_hash = h
            entry.shown_at = time.monotonic()
            entry.show_count += 1

    def evict(self, qname: str) -> None:
        self._entries.pop(qname, None)

    def evict_many(self, qnames: list[str]) -> None:
        for q in qnames:
            self._entries.pop(q, None)

    def clear(self) -> None:
        self._entries.clear()

    def stub_text(self, qname: str, refetch: str | None = None) -> str:
        refetch = refetch or f"get_symbol('{qname}', projection='code')"
        return (
            f"# ↤ {qname} — shown earlier this session\n"
            f"#   re-fetch with {refetch}\n"
        )
