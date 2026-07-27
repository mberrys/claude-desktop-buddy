"""`notify` hook for Codex CLI — forwards turn events to a running bridge.

Codex invokes the configured program with one argument: a JSON blob
describing the event. Wire it up in `%USERPROFILE%\\.codex\\config.toml`:

    notify = ["python", "-m", "buddy_bridge.notify_hook"]

The log tail already covers most of this; the hook is worth adding because
it fires the instant a turn completes, which makes the celebrate/idle
transition feel immediate instead of poll-delayed.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:8787"


def post(endpoint: str, route: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        endpoint.rstrip("/") + route,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # The bridge may not be running; a notify hook must never block Codex.
    with urllib.request.urlopen(request, timeout=2.0):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    endpoint = DEFAULT_ENDPOINT
    if argv and argv[0].startswith("http"):
        endpoint = argv.pop(0)
    if not argv:
        return 0

    try:
        event = json.loads(argv[0])
    except json.JSONDecodeError:
        return 0
    if not isinstance(event, dict):
        return 0

    kind = str(event.get("type") or "")
    try:
        if kind == "agent-turn-complete":
            message = str(event.get("last-assistant-message") or "turn complete")
            post(endpoint, "/entry", {"text": message, "msg": message})
            post(endpoint, "/snapshot", {"running": 0, "waiting": 0})
        else:
            post(endpoint, "/entry", {"text": kind or "codex event"})
    except (urllib.error.URLError, OSError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
