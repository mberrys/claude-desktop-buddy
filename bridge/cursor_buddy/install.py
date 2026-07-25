"""Writes the Cursor hooks.json that points at this bridge.

Merges into an existing file rather than replacing it, so a workspace that
already runs its own hooks keeps them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOK_EVENTS = [
    "beforeSubmitPrompt",
    "beforeShellExecution",
    "beforeMCPExecution",
    "afterFileEdit",
    "stop",
]

SCHEMA = "https://unpkg.com/cursor-hooks@latest/schema/hooks.schema.json"


def hook_command() -> str:
    """Absolute paths on both halves: Cursor's cwd is not ours to assume."""
    runner = Path(__file__).resolve().parent.parent / "run_hook.py"
    python = Path(sys.executable)
    if python.name.lower() == "python.exe":
        # Avoid a console window flashing on every hook invocation.
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            python = pythonw
    return f'"{python}" "{runner}"'


def install(target_dir: Path) -> Path:
    cursor_dir = target_dir / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    path = cursor_dir / "hooks.json"

    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text("utf-8"))
        except ValueError:
            backup = path.with_suffix(".json.bak")
            backup.write_text(path.read_text("utf-8"), encoding="utf-8")
            print(f"existing hooks.json was not valid JSON; saved it to {backup}")
            config = {}
    if not isinstance(config, dict):
        config = {}

    config.setdefault("$schema", SCHEMA)
    config["version"] = 1
    hooks = config.setdefault("hooks", {})
    command = hook_command()

    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        if any(isinstance(e, dict) and "run_hook.py" in str(e.get("command", "")) for e in entries):
            entries[:] = [
                e for e in entries if not (isinstance(e, dict) and "run_hook.py" in str(e.get("command", "")))
            ]
        entries.append({"command": command})

    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
