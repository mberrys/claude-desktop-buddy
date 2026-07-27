"""BLE central side of the Hardware Buddy protocol (see REFERENCE.md).

This is the half the Claude desktop app normally plays. On Windows 11 bleak
sits on WinRT, which only exposes GATT for devices Windows already knows, so
`pair()` is attempted on connect — the firmware advertises DisplayOnly and
Windows pops a PIN prompt for the passkey shown on the stick.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .state import Snapshot

log = logging.getLogger("buddy.ble")

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # we write
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # we subscribe

KEEPALIVE_S = 10.0
STATUS_POLL_S = 30.0
ACK_TIMEOUT_S = 8.0
CHUNK_BYTES = 512  # base64 of this must fit one write; MTU-chunked below
MAX_PUSH_BYTES = 1_800_000

DecisionHandler = Callable[[str, str], Awaitable[None]]


class BuddyLink:
    """One connection's worth of protocol state."""

    def __init__(
        self,
        client: BleakClient,
        snapshot: Snapshot,
        owner: str | None,
        on_decision: DecisionHandler | None = None,
    ) -> None:
        self._client = client
        self._snapshot = snapshot
        self._owner = owner
        self._on_decision = on_decision
        self._rx = bytearray()
        self._acks: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._last_status: dict[str, Any] | None = None

    # -- wire I/O ---------------------------------------------------------

    async def _write_line(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        await self._write_bytes(data)

    async def _write_bytes(self, data: bytes) -> None:
        # ATT write payload is MTU-3; bleak exposes mtu_size once connected.
        limit = max(20, (self._client.mtu_size or 23) - 3)
        for i in range(0, len(data), limit):
            await self._client.write_gatt_char(NUS_RX, data[i : i + limit], response=False)

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        self._rx.extend(data)
        while b"\n" in self._rx:
            raw, _, rest = bytes(self._rx).partition(b"\n")
            self._rx = bytearray(rest)
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                log.debug("unparseable line from device: %r", line[:120])
                continue
            if isinstance(msg, dict):
                self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "ack" in msg:
            if msg.get("ack") == "status":
                self._last_status = msg.get("data") or {}
            self._acks.put_nowait(msg)
            return
        if msg.get("cmd") == "permission":
            prompt_id = str(msg.get("id") or "")
            decision = str(msg.get("decision") or "")
            log.info("device answered %s -> %s", prompt_id, decision)
            if self._on_decision is not None:
                asyncio.ensure_future(self._on_decision(prompt_id, decision))
            self._snapshot.clear_prompt(prompt_id)
            return
        log.debug("device says: %s", msg)

    async def _command(self, obj: dict[str, Any], timeout: float = ACK_TIMEOUT_S) -> dict[str, Any]:
        """Send a `cmd` and wait for its matching ack."""
        while not self._acks.empty():  # drop stale acks from a timed-out call
            self._acks.get_nowait()
        await self._write_line(obj)
        want = obj.get("cmd")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no ack for {want!r}")
            ack = await asyncio.wait_for(self._acks.get(), timeout=remaining)
            if ack.get("ack") == want:
                if not ack.get("ok", False):
                    raise RuntimeError(f"device refused {want!r}: {ack.get('error', 'no reason')}")
                return ack

    # -- session ----------------------------------------------------------

    async def run(self, push_folder: Path | None = None) -> None:
        await self._client.start_notify(NUS_TX, self._on_notify)
        try:
            offset = -time.timezone if time.localtime().tm_isdst == 0 else -time.altzone
            await self._write_line({"time": [int(time.time()), int(offset)]})
            if self._owner:
                with contextlib.suppress(TimeoutError, RuntimeError):
                    await self._command({"cmd": "owner", "name": self._owner})

            with contextlib.suppress(TimeoutError, RuntimeError):
                status = await self._command({"cmd": "status"})
                data = status.get("data") or {}
                log.info(
                    "device %s | secure=%s | battery=%s%%",
                    data.get("name", "?"),
                    data.get("sec", False),
                    (data.get("bat") or {}).get("pct", "?"),
                )

            if push_folder is not None:
                await self.push_folder(push_folder)

            await self._heartbeat_loop()
        finally:
            with contextlib.suppress(Exception):
                await self._client.stop_notify(NUS_TX)

    async def _heartbeat_loop(self) -> None:
        """Snapshot on every change, plus a keepalive so the device stays awake."""
        last_send = 0.0
        last_status = time.monotonic()
        while self._client.is_connected:
            now = time.monotonic()
            if self._snapshot.take_dirty() or now - last_send >= KEEPALIVE_S:
                await self._write_bytes(self._snapshot.line())
                last_send = now
            if now - last_status >= STATUS_POLL_S:
                last_status = now
                with contextlib.suppress(TimeoutError, RuntimeError, asyncio.TimeoutError):
                    await self._command({"cmd": "status"})
            await asyncio.sleep(0.25)

    @property
    def device_status(self) -> dict[str, Any] | None:
        return self._last_status

    # -- folder push ------------------------------------------------------

    async def push_folder(self, folder: Path) -> None:
        """Stream a character pack the way the desktop's drop target does."""
        files = sorted(
            p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
        )
        if not files:
            raise ValueError(f"{folder} has no pushable files")
        total = sum(p.stat().st_size for p in files)
        if total > MAX_PUSH_BYTES:
            raise ValueError(f"{folder} is {total} bytes, over the {MAX_PUSH_BYTES} limit")

        name = folder.name
        manifest = folder / "manifest.json"
        if manifest.is_file():
            with contextlib.suppress(Exception):
                name = json.loads(manifest.read_text("utf-8")).get("name") or name

        log.info("pushing %s (%d files, %d bytes)", name, len(files), total)
        await self._command({"cmd": "char_begin", "name": name, "total": total})
        for path in files:
            payload = path.read_bytes()
            await self._command({"cmd": "file", "path": path.name, "size": len(payload)})
            for i in range(0, len(payload), CHUNK_BYTES):
                blob = base64.b64encode(payload[i : i + CHUNK_BYTES]).decode("ascii")
                await self._command({"cmd": "chunk", "d": blob}, timeout=15.0)
            await self._command({"cmd": "file_end"}, timeout=15.0)
            log.info("  %s (%d bytes)", path.name, len(payload))
        await self._command({"cmd": "char_end"}, timeout=20.0)
        log.info("push complete")


async def find_device(name_prefix: str, address: str | None, timeout: float) -> BLEDevice | None:
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    prefix = name_prefix.lower()

    def match(device: BLEDevice, adv: Any) -> bool:
        candidate = (adv.local_name or device.name or "").lower()
        if candidate.startswith(prefix):
            return True
        return NUS_SERVICE in {u.lower() for u in (adv.service_uuids or [])} and bool(
            candidate.startswith(prefix)
        )

    return await BleakScanner.find_device_by_filter(match, timeout=timeout)


async def serve(
    snapshot: Snapshot,
    *,
    name_prefix: str = "Claude",
    address: str | None = None,
    owner: str | None = None,
    on_decision: DecisionHandler | None = None,
    push_folder: Path | None = None,
    scan_timeout: float = 10.0,
    pair: bool = True,
) -> None:
    """Connect, run the protocol, reconnect forever."""
    backoff = 2.0
    while True:
        device = await find_device(name_prefix, address, scan_timeout)
        if device is None:
            log.info("no device matching %r found, rescanning", address or name_prefix + "*")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)
            continue

        log.info("connecting to %s (%s)", device.name, device.address)
        try:
            async with BleakClient(device, timeout=20.0) as client:
                if pair:
                    # Encrypted characteristics need a bond; on Windows this
                    # raises once the device is already paired, which is fine.
                    try:
                        await client.pair()
                    except Exception as exc:  # noqa: BLE001 - backend-specific
                        log.debug("pair() returned %s (already bonded?)", exc)
                backoff = 2.0
                log.info("connected")
                link = BuddyLink(client, snapshot, owner, on_decision)
                await link.run(push_folder=push_folder)
                push_folder = None  # only push once per run
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the bridge alive
            log.warning("link dropped: %s", exc)
        log.info("disconnected, reconnecting in %.0fs", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.5, 30.0)
