"""The long-running half of the bridge.

Owns the BLE connection and the session state, and answers the hook
processes over loopback. This is the piece that plays the role the Claude
desktop app plays in REFERENCE.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import ipc
from .ble import BuddyLink
from .protocol import KEEPALIVE_SECONDS, owner_cmd, time_sync
from .state import BuddyState

log = logging.getLogger("cursor-buddy.daemon")


class Daemon:
    def __init__(self, address: str | None = None, owner: str | None = None) -> None:
        self.state = BuddyState()
        self.owner = owner
        self.link = BuddyLink(
            address=address,
            on_message=self._on_device_message,
            on_connect=self._on_device_connect,
        )
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._dirty = asyncio.Event()
        self._token = ""

    # ------------------------------------------------------------ device I/O

    async def _on_device_connect(self) -> None:
        await self.link.send(time_sync())
        if self.owner:
            await self.link.send(owner_cmd(self.owner))
        await self._push_snapshot()

    async def _on_device_message(self, obj: dict[str, Any]) -> None:
        if obj.get("cmd") == "permission":
            prompt_id = str(obj.get("id", ""))
            decision = str(obj.get("decision", ""))
            if decision not in ("once", "deny"):
                log.warning("ignoring unknown decision %r", decision)
                return
            self._settle(prompt_id, decision)
            return
        if "ack" in obj:
            log.debug("ack: %s", obj)
            return
        log.debug("device: %s", obj)

    def _settle(self, prompt_id: str, decision: str) -> None:
        prompt = self.state.resolve_prompt(prompt_id)
        waiter = self._waiters.pop(prompt_id, None)
        if prompt is None and waiter is None:
            log.info("decision for unknown prompt %s (already resolved?)", prompt_id)
            return
        verb = "approved" if decision == "once" else "denied"
        if prompt is not None:
            self.state.log(f"{verb} {prompt.tool}")
        if waiter is not None and not waiter.done():
            waiter.set_result(decision)
        self._dirty.set()

    async def _push_snapshot(self) -> None:
        await self.link.send(self.state.snapshot())

    # -------------------------------------------------------------- IPC side

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not raw:
                return
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                return
            if not isinstance(msg, dict) or msg.get("token") != self._token:
                await self._reply(writer, {"ok": False, "error": "unauthorized"})
                return
            await self._reply(writer, await self._dispatch(msg))
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _reply(self, writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await writer.drain()

    async def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg.get("type")
        if kind == "ping":
            return {"ok": True, "connected": self.link.connected}
        if kind == "event":
            return self._on_event(msg)
        if kind == "gate":
            return await self._on_gate(msg)
        return {"ok": False, "error": f"unknown request {kind!r}"}

    def _on_event(self, msg: dict[str, Any]) -> dict[str, Any]:
        session = str(msg.get("session") or "default")
        running = msg.get("running")
        self.state.touch_session(
            session,
            running if isinstance(running, bool) else None,
            workspace=str(msg.get("workspace") or ""),
        )
        if msg.get("log"):
            tag = self.state.tag(session)
            self.state.log(f"[{tag}] {msg['log']}" if tag else str(msg["log"]))
        tokens = msg.get("tokens")
        if isinstance(tokens, int):
            self.state.add_tokens(tokens)
        self._dirty.set()
        return {"ok": True}

    async def _on_gate(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Raise a permission prompt on the device and wait for the verdict."""
        session = str(msg.get("session") or "default")
        timeout = float(msg.get("timeout") or 60.0)
        self.state.touch_session(session, running=True, workspace=str(msg.get("workspace") or ""))

        if not self.link.connected:
            # No pet to ask. Hand the decision straight back to Cursor.
            return {"ok": True, "decision": "unavailable"}

        tool = str(msg.get("tool") or "tool")
        tag = self.state.tag(session)
        prompt = self.state.add_prompt(f"{tag}:{tool}" if tag else tool, str(msg.get("hint") or ""))
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[prompt.id] = waiter
        self._dirty.set()
        log.info("prompt %s: %s %s", prompt.id, prompt.tool, prompt.hint)

        try:
            decision = await asyncio.wait_for(waiter, timeout=timeout)
            return {"ok": True, "decision": decision}
        except asyncio.TimeoutError:
            self.state.resolve_prompt(prompt.id)
            self._waiters.pop(prompt.id, None)
            self._dirty.set()
            log.info("prompt %s timed out", prompt.id)
            return {"ok": True, "decision": "timeout"}

    # ----------------------------------------------------------------- loops

    async def _snapshot_loop(self) -> None:
        """Push on every change, and at least every keepalive interval."""
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._dirty.clear()
            if self.link.connected:
                await self._push_snapshot()

    async def run(self) -> None:
        port = ipc.listen_port()
        self._token = ipc.write_endpoint(port)
        server = await asyncio.start_server(self._handle_client, "127.0.0.1", port)
        log.info("listening on 127.0.0.1:%d", port)
        async with server:
            await asyncio.gather(self.link.run(), self._snapshot_loop())
