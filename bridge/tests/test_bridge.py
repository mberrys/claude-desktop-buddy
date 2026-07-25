"""End-to-end test of the bridge with a fake device in place of the BLE link.

Runs the real daemon, the real IPC layer, and the real hook handler; only
`BuddyLink` is swapped out. Run it with `python bridge/tests/test_bridge.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE))

_TMP = tempfile.mkdtemp(prefix="cursor-buddy-test-")
os.environ["CURSOR_BUDDY_HOME"] = _TMP
os.environ["CURSOR_BUDDY_PORT"] = "8799"
os.environ["CURSOR_BUDDY_AUTOSTART"] = "0"

from cursor_buddy import ipc  # noqa: E402
from cursor_buddy.daemon import Daemon  # noqa: E402
from cursor_buddy.protocol import LineBuffer, encode_line  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else f' — {detail}'}")
    if not ok:
        failures.append(name)


class FakeLink:
    """Stands in for BuddyLink: records snapshots, replays device messages."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.sent: list[dict] = []
        self.connected = True

    async def send(self, obj: dict) -> bool:
        if not self.connected:
            return False
        self.sent.append(obj)
        return True

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def device_says(self, obj: dict) -> None:
        await self.daemon._on_device_message(obj)

    def last_snapshot(self) -> dict:
        for obj in reversed(self.sent):
            if "total" in obj:
                return obj
        return {}


def hook_call(payload: dict, timeout: float = 20.0) -> dict:
    """Invoke run_hook.py exactly the way Cursor does."""
    proc = subprocess.run(
        [sys.executable, str(BRIDGE / "run_hook.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def test_protocol() -> None:
    print("protocol")
    buf = LineBuffer()
    line = encode_line({"cmd": "permission", "id": "x", "decision": "once"})
    # Deliver it in MTU-sized dribs to prove reassembly works.
    got: list[dict] = []
    for i in range(0, len(line), 7):
        got.extend(buf.feed(line[i : i + 7]))
    check("fragmented line reassembles", got == [{"cmd": "permission", "id": "x", "decision": "once"}], str(got))
    check("garbage is dropped", buf.feed(b"not json\n") == [])
    two = buf.feed(encode_line({"a": 1}) + encode_line({"b": 2}))
    check("two lines in one packet", two == [{"a": 1}, {"b": 2}], str(two))


def main() -> int:
    daemon = Daemon(owner="Michael")
    link = FakeLink(daemon)
    daemon.link = link  # type: ignore[assignment]

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def serve() -> None:
        port = ipc.listen_port()
        daemon._token = ipc.write_endpoint(port)
        server = await asyncio.start_server(daemon._handle_client, "127.0.0.1", port)
        ready.set()
        async with server:
            await asyncio.gather(daemon._snapshot_loop(), daemon.link.run())

    threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True).start()
    ready.wait(5)

    test_protocol()

    print("notification hooks")
    check("ping reaches daemon", ipc.request({"type": "ping"}, 3.0).get("ok") is True)

    hook_call({"hook_event_name": "beforeSubmitPrompt", "prompt": "add a test", "conversation_id": "c1"})
    time.sleep(0.4)
    snap = link.last_snapshot()
    check("session shows as running", snap.get("running") == 1, json.dumps(snap))
    check("prompt text lands in transcript", any("add a test" in e for e in snap.get("entries", [])), json.dumps(snap))
    check("msg reflects work", snap.get("msg") == "working…", str(snap.get("msg")))

    hook_call({
        "hook_event_name": "afterFileEdit",
        "file_path": "C:\\src\\main.cpp",
        "edits": [{"new_string": "x" * 400}],
        "conversation_id": "c1",
    })
    time.sleep(0.4)
    snap = link.last_snapshot()
    check("edit is reported by basename", any("edited main.cpp" in e for e in snap.get("entries", [])), json.dumps(snap.get("entries")))
    check("tokens accrue from edits", snap.get("tokens") == 100 + len("add a test") // 4, str(snap.get("tokens")))

    out = hook_call({"hook_event_name": "stop", "status": "completed", "conversation_id": "c1"})
    time.sleep(0.4)
    check("stop returns no response body", out == {}, str(out))
    check("stop clears running", link.last_snapshot().get("running") == 0, json.dumps(link.last_snapshot()))

    print("approvals")

    def approve_when_prompted(decision: str, holder: dict) -> None:
        for _ in range(200):
            snap = link.last_snapshot()
            prompt = snap.get("prompt")
            if prompt:
                holder["prompt"] = prompt
                asyncio.run_coroutine_threadsafe(
                    link.device_says({"cmd": "permission", "id": prompt["id"], "decision": decision}),
                    loop,
                ).result(5)
                return
            time.sleep(0.05)

    for decision, expected in (("once", "allow"), ("deny", "deny")):
        holder: dict = {}
        t = threading.Thread(target=approve_when_prompted, args=(decision, holder))
        t.start()
        out = hook_call({
            "hook_event_name": "beforeShellExecution",
            "command": "git push --force",
            "conversation_id": "c1",
        })
        t.join()
        check(f"device {decision} -> {expected}", out.get("permission") == expected, str(out))
        check(f"{decision}: hint reached the device", holder.get("prompt", {}).get("hint") == "git push --force", str(holder))

    time.sleep(0.4)
    snap = link.last_snapshot()
    check("no prompt left pending", "prompt" not in snap and snap.get("waiting") == 0, json.dumps(snap))
    check("decisions are logged", any("approved" in e for e in snap.get("entries", [])), json.dumps(snap.get("entries")))

    print("two windows at once")
    hook_call({
        "hook_event_name": "beforeSubmitPrompt",
        "prompt": "one",
        "conversation_id": "c1",
        "workspace_roots": ["C:\\src\\buddy"],
    })
    time.sleep(0.3)
    snap = link.last_snapshot()
    check("single workspace stays untagged", not any("[" in e for e in snap.get("entries", [])), json.dumps(snap.get("entries")))

    hook_call({
        "hook_event_name": "beforeSubmitPrompt",
        "prompt": "two",
        "conversation_id": "c2",
        "workspace_roots": ["C:\\src\\website"],
    })
    time.sleep(0.3)
    snap = link.last_snapshot()
    check("both windows counted as sessions", snap.get("total") == 2, json.dumps(snap))
    check("second window is tagged", any("[website] two" in e for e in snap.get("entries", [])), json.dumps(snap.get("entries")))

    holder = {}
    t = threading.Thread(target=approve_when_prompted, args=("once", holder))
    t.start()
    hook_call({
        "hook_event_name": "beforeShellExecution",
        "command": "npm run build",
        "conversation_id": "c2",
        "workspace_roots": ["C:\\src\\website"],
    })
    t.join()
    check("prompt names the workspace", holder.get("prompt", {}).get("tool") == "website:Shell", str(holder))

    # Two prompts in flight: the device shows the older one, both stay counted.
    slow: dict = {}

    def gate_in_background(key: str, conversation: str, command: str) -> None:
        slow[key] = hook_call({
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "conversation_id": conversation,
            "workspace_roots": ["C:\\src\\buddy" if conversation == "c1" else "C:\\src\\website"],
        }, timeout=40.0)

    first = threading.Thread(target=gate_in_background, args=("a", "c1", "first cmd"))
    first.start()
    time.sleep(1.0)
    second = threading.Thread(target=gate_in_background, args=("b", "c2", "second cmd"))
    second.start()
    time.sleep(1.0)
    snap = link.last_snapshot()
    check("both prompts counted as waiting", snap.get("waiting") == 2, json.dumps(snap))
    check("device shows the older prompt", snap.get("prompt", {}).get("hint") == "first cmd", json.dumps(snap.get("prompt")))

    holder_a: dict = {"prompt": snap["prompt"]}
    asyncio.run_coroutine_threadsafe(
        link.device_says({"cmd": "permission", "id": snap["prompt"]["id"], "decision": "once"}), loop
    ).result(5)
    first.join(30)
    time.sleep(0.5)
    snap = link.last_snapshot()
    check("resolving the first surfaces the second", snap.get("prompt", {}).get("hint") == "second cmd", json.dumps(snap.get("prompt")))
    asyncio.run_coroutine_threadsafe(
        link.device_says({"cmd": "permission", "id": snap["prompt"]["id"], "decision": "deny"}), loop
    ).result(5)
    second.join(30)
    check("each window got its own verdict", slow.get("a", {}).get("permission") == "allow" and slow.get("b", {}).get("permission") == "deny", str(slow))
    del holder_a

    print("fail-open behaviour")
    os.environ["CURSOR_BUDDY_TIMEOUT"] = "1"
    out = hook_call({"hook_event_name": "beforeShellExecution", "command": "ls", "conversation_id": "c1"})
    check("unanswered prompt falls back to ask", out.get("permission") == "ask", str(out))
    os.environ["CURSOR_BUDDY_TIMEOUT"] = "60"

    link.connected = False
    out = hook_call({"hook_event_name": "beforeShellExecution", "command": "ls", "conversation_id": "c1"})
    check("disconnected device falls back to ask", out.get("permission") == "ask", str(out))
    link.connected = True

    port = os.environ["CURSOR_BUDDY_PORT"]
    os.environ["CURSOR_BUDDY_PORT"] = "8798"  # nothing listening
    out = hook_call({"hook_event_name": "beforeShellExecution", "command": "ls", "conversation_id": "c1"})
    check("dead daemon falls back to ask", out.get("permission") == "ask", str(out))
    out = hook_call({"hook_event_name": "beforeSubmitPrompt", "prompt": "hi", "conversation_id": "c1"})
    check("dead daemon still lets prompts through", out.get("continue") is True, str(out))
    os.environ["CURSOR_BUDDY_PORT"] = port

    os.environ["CURSOR_BUDDY_TIMEOUT"] = "1"
    out = hook_call({"hook_event_name": "beforeShellExecution", "command": "ls"})  # no conversation_id
    check("missing session id is tolerated", "permission" in out, str(out))
    os.environ["CURSOR_BUDDY_TIMEOUT"] = "60"

    print("auth")
    import socket

    with socket.create_connection(("127.0.0.1", int(port)), timeout=3) as sock:
        sock.sendall(b'{"type":"ping","token":"wrong"}\n')
        reply = json.loads(sock.recv(4096).decode().split("\n")[0])
    check("bad token is rejected", reply.get("ok") is False, str(reply))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
