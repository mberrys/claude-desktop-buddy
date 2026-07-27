"""The short-lived half of the bridge: one process per Cursor hook event.

Cursor pipes the event as JSON on stdin and reads our response as JSON on
stdout. Everything here is fail-open on purpose — a bridge that is not
running, a pet that is asleep, or a bug in this file must never be able to
wedge somebody's editor. Any error path falls back to "let Cursor decide".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import ipc

# Which hook events get escalated to the device for a decision. The rest are
# reported for display only. Reads are deliberately not gated by default —
# the agent reads constantly and you would be pressing buttons all day.
DEFAULT_GATED = "beforeShellExecution,beforeMCPExecution"

# Cursor's UI keeps waiting while we do, so this bounds how long a hook can
# sit on an unanswered prompt before handing the decision back.
DEFAULT_TIMEOUT = 60.0

# Cursor hooks carry no token usage, so the pet's level bar is driven by an
# estimate: roughly 4 characters of agent-produced text per token.
CHARS_PER_TOKEN = 4


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _gated_events() -> set[str]:
    raw = os.environ.get("CURSOR_BUDDY_GATE", DEFAULT_GATED)
    return {part.strip() for part in raw.split(",") if part.strip()}


def _timeout() -> float:
    try:
        return float(os.environ.get("CURSOR_BUDDY_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


def _emit(obj: dict[str, Any]) -> None:
    if obj:
        json.dump(obj, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _ensure_daemon() -> None:
    """Start the daemon on first hook if it is not already up."""
    if not _env_flag("CURSOR_BUDDY_AUTOSTART", True) or ipc.daemon_alive():
        return
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW — no console flash on every edit.
        creationflags = 0x00000008 | 0x08000000
    try:
        subprocess.Popen(
            [sys.executable, "-m", "cursor_buddy", "daemon"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError:
        pass


def _session_id(payload: dict[str, Any]) -> str:
    for key in ("conversation_id", "thread_id", "generation_id", "session_id"):
        value = payload.get(key)
        if value:
            return str(value)
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        return str(roots[0])
    return "cursor"


def _basename(path: str) -> str:
    """Last path segment, splitting on both separators.

    `pathlib` only understands the separators of the host it runs on, and
    the display is 135px wide — a full `C:\\Users\\...` path is useless there.
    """
    return re.split(r"[\\/]", str(path).rstrip("\\/"))[-1]


def _edit_chars(edits: Any) -> int:
    """Sum the text an edit introduced, whatever shape Cursor sends it in."""
    total = 0
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                for key in ("new_string", "newText", "new_text", "text", "content"):
                    value = edit.get(key)
                    if isinstance(value, str):
                        total += len(value)
                        break
            elif isinstance(edit, str):
                total += len(edit)
    return total


def _workspace(payload: dict[str, Any]) -> str:
    """Short label for the window an event came from."""
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        return _basename(str(roots[0]))
    return ""


def _notify(**fields: Any) -> None:
    try:
        ipc.request({"type": "event", **fields}, timeout=5.0)
    except ipc.IpcError:
        pass


def _gate(session: str, tool: str, hint: str, workspace: str = "") -> str:
    """Ask the device. Returns a Cursor permission value."""
    try:
        reply = ipc.request(
            {
                "type": "gate",
                "session": session,
                "tool": tool,
                "hint": hint,
                "workspace": workspace,
                "timeout": _timeout(),
            },
            timeout=_timeout() + 10.0,
        )
    except ipc.IpcError:
        return "ask"
    decision = reply.get("decision")
    if decision == "once":
        return "allow"
    if decision == "deny":
        return "deny"
    return "ask"


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("hook_event_name") or "")
    session = _session_id(payload)
    workspace = _workspace(payload)
    gated = _gated_events()

    if event == "beforeSubmitPrompt":
        prompt = str(payload.get("prompt") or "")
        _notify(
            session=session,
            workspace=workspace,
            running=True,
            log=prompt or "new prompt",
            tokens=len(prompt) // CHARS_PER_TOKEN,
        )
        return {"continue": True}

    if event == "beforeShellExecution":
        command = str(payload.get("command") or "")
        if event in gated:
            return {"permission": _gate(session, "Shell", command, workspace)}
        _notify(session=session, workspace=workspace, running=True, log=command or "shell")
        return {"permission": "allow"}

    if event == "beforeMCPExecution":
        tool = str(payload.get("tool_name") or "mcp")
        args = payload.get("arguments")
        hint = json.dumps(args, ensure_ascii=False) if args else ""
        if event in gated:
            return {"permission": _gate(session, tool, hint, workspace)}
        _notify(session=session, workspace=workspace, running=True, log=f"{tool} {hint}".strip())
        return {"permission": "allow"}

    if event == "beforeReadFile":
        path = _basename(payload.get("file_path") or "")
        if event in gated:
            return {"permission": "allow" if _gate(session, "Read", path, workspace) != "deny" else "deny"}
        _notify(session=session, workspace=workspace, running=True, log=f"reading {path}" if path else "reading")
        return {"permission": "allow"}

    if event == "afterFileEdit":
        path = _basename(payload.get("file_path") or "")
        chars = _edit_chars(payload.get("edits"))
        _notify(
            session=session,
            workspace=workspace,
            running=True,
            log=f"edited {path}" if path else "edited a file",
            tokens=chars // CHARS_PER_TOKEN,
        )
        return {}

    if event == "stop":
        status = str(payload.get("status") or "done")
        _notify(session=session, workspace=workspace, running=False, log=f"turn {status}")
        return {}

    _notify(session=session, workspace=workspace, log=event or "event")
    return {}


def main(argv: list[str]) -> int:
    del argv
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        _ensure_daemon()
        _emit(handle(payload))
    except Exception:  # noqa: BLE001 - a broken hook must not break Cursor
        _emit({})
    return 0
