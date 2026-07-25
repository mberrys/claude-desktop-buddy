"""Session state derived from Cursor hook events, rendered as snapshots.

The device only ever sees the snapshot shape documented in REFERENCE.md,
so all the Cursor-specific bookkeeping stays on this side of the link.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# A conversation with no events for this long is assumed closed. Cursor has
# no "session ended" hook beyond `stop`, which fires per turn, not per chat.
SESSION_TTL = 30 * 60.0

# How many transcript lines the device is willing to scroll through.
MAX_ENTRIES = 12

# Prompts nobody answers eventually stop blocking the display. The hook that
# raised the prompt applies its own (shorter) timeout; this is just a floor
# so a crashed hook cannot pin the pet in `attention` forever.
PROMPT_TTL = 10 * 60.0

STATE_DIR = Path(os.environ.get("CURSOR_BUDDY_HOME") or (Path.home() / ".cursor-buddy"))
TOKENS_FILE = STATE_DIR / "tokens.json"

_ids = itertools.count(1)


@dataclass
class Session:
    id: str
    running: bool = False
    workspace: str = ""
    last_seen: float = field(default_factory=time.time)


@dataclass
class PendingPrompt:
    id: str
    tool: str
    hint: str
    created: float = field(default_factory=time.time)


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BuddyState:
    """Everything the pet reacts to, in one place."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.prompts: dict[str, PendingPrompt] = {}
        self.entries: deque[tuple[float, str]] = deque(maxlen=MAX_ENTRIES)
        self.tokens = 0
        self.tokens_today = 0
        self._today = time.localtime().tm_yday
        self._load_tokens()

    # ---------------------------------------------------------------- tokens

    def _load_tokens(self) -> None:
        """`tokens_today` is meant to survive a restart; `tokens` is not."""
        try:
            saved = json.loads(TOKENS_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if saved.get("yday") == self._today:
            self.tokens_today = int(saved.get("tokens_today", 0))

    def _save_tokens(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            TOKENS_FILE.write_text(
                json.dumps({"yday": self._today, "tokens_today": self.tokens_today}),
                encoding="utf-8",
            )
        except OSError:
            pass

    def add_tokens(self, n: int) -> None:
        """Cursor hooks carry no usage data, so callers pass an estimate.

        See `hook.py` for how work is converted into token-ish units. The
        device only uses this to drive its level-up celebration, so being
        in the right ballpark is enough.
        """
        if n <= 0:
            return
        yday = time.localtime().tm_yday
        if yday != self._today:
            self._today = yday
            self.tokens_today = 0
        self.tokens += n
        self.tokens_today += n
        self._save_tokens()

    # -------------------------------------------------------------- sessions

    def touch_session(
        self, session_id: str, running: bool | None = None, workspace: str = ""
    ) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            session = Session(id=session_id)
            self.sessions[session_id] = session
        session.last_seen = time.time()
        if running is not None:
            session.running = running
        if workspace:
            session.workspace = workspace
        return session

    def active_workspaces(self) -> set[str]:
        return {s.workspace for s in self.sessions.values() if s.workspace}

    def tag(self, session_id: str) -> str:
        """Workspace label, but only when there is something to disambiguate.

        Several Cursor windows all feed one device, so a bare `git push` on
        a 135px screen is ambiguous the moment a second workspace is live.
        With one workspace the tag is noise, so it stays off.
        """
        if len(self.active_workspaces()) < 2:
            return ""
        session = self.sessions.get(session_id)
        return session.workspace if session else ""

    def expire(self) -> None:
        now = time.time()
        for sid, session in list(self.sessions.items()):
            if now - session.last_seen > SESSION_TTL:
                del self.sessions[sid]
        for pid, prompt in list(self.prompts.items()):
            if now - prompt.created > PROMPT_TTL:
                del self.prompts[pid]

    # --------------------------------------------------------------- prompts

    def add_prompt(self, tool: str, hint: str) -> PendingPrompt:
        prompt = PendingPrompt(
            id=f"cur_{next(_ids)}_{int(time.time())}",
            tool=_short(tool, 24),
            hint=_short(hint, 96),
        )
        self.prompts[prompt.id] = prompt
        return prompt

    def resolve_prompt(self, prompt_id: str) -> PendingPrompt | None:
        return self.prompts.pop(prompt_id, None)

    def oldest_prompt(self) -> PendingPrompt | None:
        if not self.prompts:
            return None
        return min(self.prompts.values(), key=lambda p: p.created)

    # ------------------------------------------------------------ transcript

    def log(self, text: str) -> None:
        text = _short(text, 64)
        if text:
            self.entries.appendleft((time.time(), text))

    # -------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        self.expire()
        running = sum(1 for s in self.sessions.values() if s.running)
        prompt = self.oldest_prompt()

        if prompt is not None:
            msg = f"approve: {prompt.tool}"
        elif running == 1:
            msg = "working…"
        elif running > 1:
            msg = f"working… ({running})"
        elif self.sessions:
            msg = "idle"
        else:
            msg = "no sessions"

        snap: dict[str, Any] = {
            "total": len(self.sessions),
            "running": running,
            "waiting": len(self.prompts),
            "msg": msg,
            "entries": list(self._format_entries()),
            "tokens": self.tokens,
            "tokens_today": self.tokens_today,
        }
        if prompt is not None:
            snap["prompt"] = {"id": prompt.id, "tool": prompt.tool, "hint": prompt.hint}
        return snap

    def _format_entries(self) -> Iterable[str]:
        for ts, text in self.entries:
            yield f"{time.strftime('%H:%M', time.localtime(ts))} {text}"
