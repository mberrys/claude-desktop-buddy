"""Offline checks for the bridge: no device or Codex install required.

    python test_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from buddy_bridge.ble import BuddyLink  # noqa: E402
from buddy_bridge.sources import http as http_source  # noqa: E402
from buddy_bridge.sources.codex import CodexSource  # noqa: E402
from buddy_bridge.state import Prompt, Snapshot  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'' if ok else f'  ({detail})'}")
    if not ok:
        failures.append(label)


def wait_for(predicate, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# -- snapshot ---------------------------------------------------------------


def test_snapshot() -> None:
    print("snapshot")
    snap = Snapshot()
    snap.update(total=2, running=1, msg="x" * 60)
    check("msg is clipped for the 24-byte device field", len(snap.wire()["msg"]) <= 23)

    for i in range(20):
        snap.add_entry(f"line {i}")
    check("entries are capped", len(snap.wire()["entries"]) == 6)
    check("entries are newest-first", snap.wire()["entries"][0] == "line 19")
    snap.add_entry("line 19")
    check("duplicate head entry is dropped", snap.wire()["entries"].count("line 19") == 1)

    snap.add_tokens(1000)
    snap.add_tokens(500)
    check("tokens accumulate", snap.wire()["tokens"] == 1500)
    check("tokens_today tracks tokens", snap.wire()["tokens_today"] == 1500)

    check("no prompt key when idle", "prompt" not in snap.wire())
    snap.set_prompt(Prompt(id="req1", tool="Shell", hint="rm -rf /tmp/foo"))
    check("prompt is published", snap.wire()["prompt"]["id"] == "req1")
    check("mismatched id does not clear", snap.clear_prompt("other") is False)
    check("matching id clears", snap.clear_prompt("req1") is True)

    snap.take_dirty()
    check("dirty clears after read", snap.take_dirty() is False)
    snap.update(msg="new")
    check("dirty sets on change", snap.take_dirty() is True)

    line = snap.line()
    check("line is newline-terminated", line.endswith(b"\n"))
    check("line is valid JSON", isinstance(json.loads(line), dict))


# -- codex tail -------------------------------------------------------------

ROLLOUT = [
    {"timestamp": "t", "type": "session_meta", "payload": {"id": "abc"}},
    {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run the tests"}]},
    },
    {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "shell",
            "arguments": json.dumps({"command": ["pytest", "-q"]}),
        },
    },
    {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {"output_tokens": 900}}},
    },
    {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {"output_tokens": 1400}}},
    },
    {
        "type": "event_msg",
        "payload": {"type": "exec_approval_request", "call_id": "call-7", "command": ["rm", "-rf", "build"]},
    },
]


def test_codex() -> None:
    print("codex source")
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "2026" / "07" / "25"
        day.mkdir(parents=True)
        log = day / "rollout-2026-07-25T10-00-00-abc.jsonl"
        log.touch()

        snap = Snapshot()
        source = CodexSource(snap, Path(tmp))
        source.start()
        try:
            check("new log is discovered", wait_for(lambda: log in source._sessions))

            with log.open("a", encoding="utf-8") as fh:
                for record in ROLLOUT:
                    fh.write(json.dumps(record) + "\n")

            check(
                "user message reaches the transcript",
                wait_for(lambda: any("run the tests" in e for e in snap.entries)),
                str(snap.entries),
            )
            check(
                "shell call is rendered as a command",
                wait_for(lambda: any("pytest -q" in e for e in snap.entries)),
                str(snap.entries),
            )
            check(
                "cumulative token counts become deltas",
                wait_for(lambda: snap.tokens == 1400),
                f"tokens={snap.tokens}",
            )
            check(
                "approval request raises a prompt",
                wait_for(lambda: snap.prompt is not None and snap.prompt.id == "call-7"),
            )
            check("prompt hint carries the command", "rm -rf build" in (snap.prompt.hint or ""))
            check("waiting count follows the prompt", wait_for(lambda: snap.waiting == 1))
            check("session counted as running", snap.total == 1 and snap.running == 1)

            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "event_msg", "payload": {"type": "exec_command_begin"}}) + "\n")
            check("resolution clears the prompt", wait_for(lambda: snap.prompt is None))

            with log.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {"type": "message", "role": "assistant", "content": [{"text": "all green"}]},
                        }
                    )
                    + "\n"
                )
            check("assistant turn ends the running state", wait_for(lambda: snap.running == 0))
            check("assistant text becomes msg", snap.msg == "all green")

            # A half-written line must not be consumed until it is complete.
            with log.open("a", encoding="utf-8") as fh:
                fh.write('{"type":"response_item","payload":{"type":"message","role":"assis')
                fh.flush()
                time.sleep(0.6)
                before = len(snap.entries)
                fh.write('tant","content":[{"text":"tail end"}]}}\n')
            check("partial line is buffered, not dropped", wait_for(lambda: len(snap.entries) == before + 1))
        finally:
            source.stop()


def test_codex_flat_format() -> None:
    print("codex source (legacy flat records)")
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "rollout-old.jsonl"
        log.touch()
        snap = Snapshot()
        source = CodexSource(snap, Path(tmp))
        source.start()
        try:
            wait_for(lambda: log in source._sessions)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "message", "role": "assistant", "content": "flat hello"}) + "\n")
                fh.write("not json at all\n")
                fh.write(json.dumps({"type": "message", "role": "user", "content": "flat ask"}) + "\n")
            check("flat records still parse", wait_for(lambda: any("flat hello" in e for e in snap.entries)))
            check("garbage lines are skipped", wait_for(lambda: any("flat ask" in e for e in snap.entries)))
        finally:
            source.stop()


# -- http endpoint ----------------------------------------------------------


def post(port: int, route: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def get(port: int, route: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=35) as response:
        return json.loads(response.read())


def test_http() -> None:
    print("control endpoint")
    snap = Snapshot()
    server, decisions = http_source.start(snap, "127.0.0.1", 0)
    port = server.server_address[1]
    try:
        post(port, "/snapshot", {"total": 3, "running": 2, "msg": "building", "bogus": 1})
        check("snapshot fields merge", snap.total == 3 and snap.running == 2)
        check("unknown fields are ignored", not hasattr(snap, "bogus"))

        post(port, "/entry", {"text": "yarn test"})
        check("entry is pushed", snap.entries[0].endswith("yarn test") or snap.entries[0] == "yarn test")

        post(port, "/tokens", {"n": 250})
        check("tokens are added", snap.tokens == 250)

        post(port, "/prompt", {"id": "p1", "tool": "Shell", "hint": "del /s"})
        check("prompt is raised", snap.prompt is not None and snap.prompt.id == "p1")
        check("state route reflects the prompt", get(port, "/state")["prompt"]["id"] == "p1")

        check("unanswered decision returns null", get(port, "/decision?id=p1")["decision"] is None)
        decisions.put("p1", "once")
        check("decision is readable once", get(port, "/decision?id=p1")["decision"] == "once")
        check("decision is consumed", get(port, "/decision?id=p1")["decision"] is None)

        post(port, "/prompt/clear", {"id": "p1"})
        check("prompt clears", snap.prompt is None)

        try:
            get(port, "/nope")
            check("unknown route 404s", False, "no error raised")
        except urllib.error.HTTPError as exc:
            check("unknown route 404s", exc.code == 404)
    finally:
        server.shutdown()


# -- protocol framing -------------------------------------------------------


class FakeClient:
    """Just enough BleakClient surface for BuddyLink."""

    mtu_size = 40  # small on purpose: forces multi-write framing

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.is_connected = True

    async def write_gatt_char(self, _uuid, data, response=False):
        assert len(data) <= self.mtu_size - 3, "write exceeded the negotiated MTU"
        self.writes.append(bytes(data))

    async def start_notify(self, _uuid, _cb):
        pass

    async def stop_notify(self, _uuid):
        pass

    def sent(self) -> list[dict]:
        lines = b"".join(self.writes).split(b"\n")
        return [json.loads(line) for line in lines if line.strip()]


def test_protocol() -> None:
    print("protocol framing")
    asyncio.run(_protocol())


async def _protocol() -> None:
    snap = Snapshot()
    client = FakeClient()
    answered: list[tuple[str, str]] = []

    async def on_decision(prompt_id: str, decision: str) -> None:
        answered.append((prompt_id, decision))

    link = BuddyLink(client, snap, owner="Michael", on_decision=on_decision)

    snap.update(total=1, running=1, msg="hello")
    await link._write_bytes(snap.line())
    check("long payloads split across MTU-sized writes", len(client.writes) > 1)
    check("reassembled payload is one JSON line", client.sent()[0]["msg"] == "hello")

    # An ack arriving in fragments, mid-line, must still parse.
    ack = b'{"ack":"status","ok":true,"data":{"name":"Clawd","sec":true}}\n'
    for i in range(0, len(ack), 7):
        link._on_notify(None, bytearray(ack[i : i + 7]))
    check("fragmented notification reassembles", link.device_status == {"name": "Clawd", "sec": True})

    snap.set_prompt(Prompt(id="req9", tool="Shell", hint="x"))
    link._on_notify(None, bytearray(b'{"cmd":"permission","id":"req9","decision":"deny"}\n'))
    await asyncio.sleep(0)
    check("device decision is routed", answered == [("req9", "deny")])
    check("device decision clears the prompt", snap.prompt is None)

    client.writes.clear()
    task = asyncio.create_task(link._command({"cmd": "name", "name": "Clawd"}, timeout=2.0))
    await asyncio.sleep(0.05)
    link._on_notify(None, bytearray(b'{"ack":"name","ok":true,"n":0}\n'))
    result = await task
    check("command waits for its ack", result["ok"] is True)

    task = asyncio.create_task(link._command({"cmd": "unpair"}, timeout=1.0))
    await asyncio.sleep(0.05)
    link._on_notify(None, bytearray(b'{"ack":"unpair","ok":false,"error":"nope"}\n'))
    try:
        await task
        check("refused command raises", False, "no exception")
    except RuntimeError as exc:
        check("refused command raises", "nope" in str(exc))

    try:
        await link._command({"cmd": "status"}, timeout=0.3)
        check("missing ack times out", False, "no timeout")
    except TimeoutError:
        check("missing ack times out", True)


def test_push() -> None:
    print("folder push")
    asyncio.run(_push())


async def _push() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "bufo"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"name": "bufo-pack"}), encoding="utf-8")
        (folder / "idle.gif").write_bytes(b"GIF89a" + b"\x01" * 1500)
        (folder / ".hidden").write_text("skip me", encoding="utf-8")

        client = FakeClient()
        client.mtu_size = 517
        link = BuddyLink(client, Snapshot(), owner=None)

        async def auto_ack() -> None:
            # Ack whatever the pusher sends, as the firmware would.
            seen = 0
            while True:
                await asyncio.sleep(0.01)
                for message in client.sent()[seen:]:
                    seen += 1
                    if "cmd" in message:
                        link._on_notify(
                            None, bytearray(json.dumps({"ack": message["cmd"], "ok": True, "n": 0}).encode() + b"\n")
                        )

        acker = asyncio.create_task(auto_ack())
        try:
            await link.push_folder(folder)
        finally:
            acker.cancel()

        sent = client.sent()
        begin = next(m for m in sent if m.get("cmd") == "char_begin")
        check("manifest name wins over folder name", begin["name"] == "bufo-pack")
        names = [m["path"] for m in sent if m.get("cmd") == "file"]
        check("dotfiles are skipped", names == ["idle.gif", "manifest.json"], str(names))
        check("total excludes dotfiles", begin["total"] == 1506 + len(json.dumps({"name": "bufo-pack"})))
        check("chunks are sent", sum(1 for m in sent if m.get("cmd") == "chunk") >= 4)
        check("stream is closed", sent[-1]["cmd"] == "char_end")


if __name__ == "__main__":
    test_snapshot()
    test_codex()
    test_codex_flat_format()
    test_http()
    test_protocol()
    test_push()
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        raise SystemExit(1)
    print("all checks passed")
