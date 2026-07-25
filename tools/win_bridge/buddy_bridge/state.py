"""Snapshot model shared by every source.

A source's only job is to keep a Snapshot up to date. The BLE loop takes
whatever the snapshot says at send time, so sources never touch the link.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any


MAX_ENTRIES = 6
MAX_MSG = 23  # firmware truncates msg at 24 bytes including the NUL


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Prompt:
    id: str
    tool: str = ""
    hint: str = ""

    def wire(self) -> dict[str, str]:
        return {
            "id": self.id,
            "tool": _clip(self.tool, 19),
            "hint": _clip(self.hint, 43),
        }


@dataclass
class Snapshot:
    """Mutable, lock-guarded view of everything the device displays."""

    total: int = 0
    running: int = 0
    waiting: int = 0
    msg: str = ""
    entries: list[str] = field(default_factory=list)
    tokens: int = 0
    tokens_today: int = 0
    prompt: Prompt | None = None

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _tokens_day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"), repr=False)
    _dirty: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- mutation ---------------------------------------------------------

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)
            self._dirty.set()

    def add_entry(self, text: str) -> None:
        """Push a transcript line. Newest first, deduped against the head."""
        line = _clip(text, 91)
        if not line:
            return
        with self._lock:
            if self.entries and self.entries[0] == line:
                return
            self.entries.insert(0, line)
            del self.entries[MAX_ENTRIES:]
            self._dirty.set()

    def add_tokens(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._roll_day_locked()
            self.tokens += n
            self.tokens_today += n
            self._dirty.set()

    def set_prompt(self, prompt: Prompt | None) -> None:
        with self._lock:
            self.prompt = prompt
            self._dirty.set()

    def clear_prompt(self, prompt_id: str | None = None) -> bool:
        """Drop the pending prompt. With an id, only if it still matches."""
        with self._lock:
            if self.prompt is None:
                return False
            if prompt_id is not None and self.prompt.id != prompt_id:
                return False
            self.prompt = None
            self._dirty.set()
            return True

    def _roll_day_locked(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._tokens_day:
            self._tokens_day = today
            self.tokens_today = 0

    # -- reading ----------------------------------------------------------

    def take_dirty(self) -> bool:
        """True at most once per change; the send loop uses it to skip work."""
        if self._dirty.is_set():
            self._dirty.clear()
            return True
        return False

    def wire(self) -> dict[str, Any]:
        with self._lock:
            self._roll_day_locked()
            payload: dict[str, Any] = {
                "total": self.total,
                "running": self.running,
                "waiting": self.waiting,
                "msg": _clip(self.msg, MAX_MSG),
                "entries": list(self.entries),
                "tokens": self.tokens,
                "tokens_today": self.tokens_today,
            }
            if self.prompt is not None:
                payload["prompt"] = self.prompt.wire()
            return payload

    def line(self) -> bytes:
        return (json.dumps(self.wire(), separators=(",", ":")) + "\n").encode("utf-8")
