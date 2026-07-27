"""Derive a buddy snapshot by tailing Codex CLI session logs.

Codex writes one JSONL rollout per session under
`%USERPROFILE%\\.codex\\sessions\\YYYY\\MM\\DD\\rollout-*.jsonl` (or
`$CODEX_HOME/sessions`). The schema has shifted across releases, so every
field lookup here is defensive: unknown record shapes are ignored rather
than fatal, and a session that only yields timestamps still drives the
running/idle animation correctly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..state import Prompt, Snapshot

log = logging.getLogger("buddy.codex")

# A session counts as "running" this long after its last logged event, and
# stays in the session count this long after that.
RUNNING_GRACE_S = 25.0
OPEN_GRACE_S = 15 * 60.0
POLL_S = 0.5

APPROVAL_TYPES = {
    "exec_approval_request",
    "apply_patch_approval_request",
    "patch_approval_request",
}
APPROVAL_RESOLVED_TYPES = {
    "exec_command_begin",
    "exec_command_end",
    "patch_apply_begin",
    "patch_apply_end",
    "turn_aborted",
    "task_complete",
}


def default_sessions_dir() -> Path:
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else Path.home() / ".codex"
    return base / "sessions"


def _text_of(content: Any) -> str:
    """Flatten a message `content` array (or plain string) to one line."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            value = block.get("text") or block.get("content") or ""
            if isinstance(value, str):
                parts.append(value)
    return " ".join(p for p in parts if p)


def _command_of(payload: dict[str, Any]) -> str:
    """Best-effort human command string for a tool/shell call record."""
    for key in ("command", "cmd"):
        value = payload.get(key)
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
        if isinstance(value, str) and value:
            return value

    args = payload.get("arguments") or payload.get("args") or payload.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args
    if isinstance(args, dict):
        for key in ("command", "cmd", "script"):
            value = args.get(key)
            if isinstance(value, list):
                return " ".join(str(v) for v in value)
            if isinstance(value, str) and value:
                return value
        if args:
            return json.dumps(args, separators=(",", ":"))
    return ""


@dataclass
class _Session:
    path: Path
    handle: Any = None
    pos: int = 0
    last_event: float = field(default_factory=time.monotonic)
    running: bool = False
    output_tokens: int = 0

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            finally:
                self.handle = None


class CodexSource:
    """Polls the sessions tree and folds new records into a Snapshot."""

    def __init__(
        self,
        snapshot: Snapshot,
        sessions_dir: Path | None = None,
        *,
        approvals: bool = True,
    ) -> None:
        self.snapshot = snapshot
        self.sessions_dir = sessions_dir or default_sessions_dir()
        self.approvals = approvals
        self._sessions: dict[Path, _Session] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = time.time()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="codex-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for session in self._sessions.values():
            session.close()

    def _run(self) -> None:
        if not self.sessions_dir.exists():
            log.warning(
                "%s does not exist yet — start a Codex session and it will appear",
                self.sessions_dir,
            )
        while not self._stop.is_set():
            try:
                self._discover()
                for session in list(self._sessions.values()):
                    self._drain(session)
                self._recompute()
            except Exception:  # noqa: BLE001 - a bad log line must not kill the tail
                log.exception("codex poll failed")
            self._stop.wait(POLL_S)

    # -- file tracking ----------------------------------------------------

    def _candidates(self) -> Iterable[Path]:
        if not self.sessions_dir.exists():
            return []
        # Only look at logs touched recently; the tree grows without bound.
        cutoff = time.time() - OPEN_GRACE_S
        found = []
        for path in self.sessions_dir.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    found.append(path)
            except OSError:
                continue
        return found

    def _discover(self) -> None:
        live = set(self._candidates())
        for path in live - set(self._sessions):
            session = _Session(path=path)
            # A log that already existed when we started is history, not
            # activity: seek to its end so we only report what happens next.
            try:
                session.pos = path.stat().st_size if path.stat().st_mtime < self._started else 0
            except OSError:
                session.pos = 0
            self._sessions[path] = session
            log.info("watching %s", path.name)
        for path in set(self._sessions) - live:
            self._sessions.pop(path).close()

    def _drain(self, session: _Session) -> None:
        try:
            size = session.path.stat().st_size
        except OSError:
            return
        if size < session.pos:  # truncated/rotated
            session.pos = 0
        if size == session.pos:
            return
        try:
            if session.handle is None:
                session.handle = session.path.open("r", encoding="utf-8", errors="replace")
            session.handle.seek(session.pos)
            for line in session.handle:
                if not line.endswith("\n"):  # partial write, retry next poll
                    break
                session.pos += len(line.encode("utf-8", "replace"))
                line = line.strip()
                if line:
                    self._record(session, line)
        except OSError as exc:
            log.debug("read %s: %s", session.path.name, exc)
            session.close()

    # -- record folding ---------------------------------------------------

    def _record(self, session: _Session, raw: str) -> None:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(rec, dict):
            return

        session.last_event = time.monotonic()
        session.running = True

        payload = rec.get("payload")
        if not isinstance(payload, dict):
            payload = rec  # older flat format: the record *is* the item
        kind = str(payload.get("type") or rec.get("type") or "")

        if kind == "message":
            role = str(payload.get("role") or "")
            text = _text_of(payload.get("content"))
            if not text:
                return
            if role == "assistant":
                self.snapshot.add_entry(f"{time.strftime('%H:%M')} {text}")
                self.snapshot.update(msg=text)
                session.running = False  # turn handed back to the user
            elif role == "user":
                self.snapshot.add_entry(f"{time.strftime('%H:%M')} > {text}")
                self.snapshot.update(msg="thinking…")
            return

        if kind in ("function_call", "local_shell_call", "custom_tool_call", "tool_call"):
            tool = str(payload.get("name") or payload.get("tool") or "tool")
            command = _command_of(payload)
            label = f"{tool}: {command}" if command else tool
            self.snapshot.add_entry(f"{time.strftime('%H:%M')} {label}")
            self.snapshot.update(msg=command or tool)
            return

        if kind == "reasoning":
            summary = _text_of(payload.get("summary") or payload.get("content"))
            if summary:
                self.snapshot.update(msg=summary)
            return

        if kind == "token_count":
            self._tokens(session, payload)
            return

        if kind in APPROVAL_TYPES and self.approvals:
            prompt_id = str(
                payload.get("id") or payload.get("call_id") or payload.get("turn_id") or raw[:32]
            )
            tool = "Patch" if "patch" in kind else "Shell"
            self.snapshot.set_prompt(Prompt(id=prompt_id, tool=tool, hint=_command_of(payload)))
            self.snapshot.update(msg=f"approve: {tool}")
            return

        if kind in APPROVAL_RESOLVED_TYPES:
            self.snapshot.clear_prompt()
            if kind in ("task_complete", "turn_aborted"):
                session.running = False

    def _tokens(self, session: _Session, payload: dict[str, Any]) -> None:
        info = payload.get("info")
        source: Any = info if isinstance(info, dict) else payload
        usage = source.get("total_token_usage") or source.get("usage") or source
        if not isinstance(usage, dict):
            return
        value = usage.get("output_tokens")
        if not isinstance(value, int):
            return
        # Codex reports cumulative-per-session; feed the snapshot the delta.
        delta = value - session.output_tokens
        session.output_tokens = value
        if delta > 0:
            self.snapshot.add_tokens(delta)

    def _recompute(self) -> None:
        now = time.monotonic()
        total = running = 0
        for session in self._sessions.values():
            age = now - session.last_event
            if age > OPEN_GRACE_S:
                continue
            total += 1
            if session.running and age <= RUNNING_GRACE_S:
                running += 1
            elif age > RUNNING_GRACE_S:
                session.running = False

        waiting = 1 if self.snapshot.prompt is not None else 0
        current = (self.snapshot.total, self.snapshot.running, self.snapshot.waiting)
        if current != (total, running, waiting):
            self.snapshot.update(total=total, running=running, waiting=waiting)
