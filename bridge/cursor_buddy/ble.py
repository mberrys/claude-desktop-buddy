"""BLE transport: scan, connect, reconnect, line framing.

Uses bleak, which on Windows 11 sits on WinRT. Two things follow from that:
pairing/bonding is handled by Windows itself (Settings → Bluetooth), and the
adapter must be on before we can scan.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from .protocol import (
    DEVICE_NAME_PREFIX,
    NUS_RX,
    NUS_SERVICE,
    NUS_TX,
    LineBuffer,
    encode_line,
)

log = logging.getLogger("cursor-buddy.ble")

SCAN_SECONDS = 8.0
RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0


async def scan(seconds: float = SCAN_SECONDS) -> list[tuple[str, str]]:
    """Return `(address, name)` for every buddy-looking device in range."""
    found: dict[str, str] = {}
    devices = await BleakScanner.discover(timeout=seconds, service_uuids=[NUS_SERVICE])
    for dev in devices:
        found[dev.address] = dev.name or "(unnamed)"
    if not found:
        # Some stacks omit the service UUID from the advertisement; fall back
        # to matching on the advertised name.
        for dev in await BleakScanner.discover(timeout=seconds):
            if (dev.name or "").startswith(DEVICE_NAME_PREFIX):
                found[dev.address] = dev.name or "(unnamed)"
    return sorted(found.items(), key=lambda kv: kv[1])


async def _find(address: str | None) -> str | None:
    if address:
        return address
    devices = await scan()
    for addr, name in devices:
        if name.startswith(DEVICE_NAME_PREFIX):
            return addr
    return devices[0][0] if devices else None


class BuddyLink:
    """Keeps one device connected and pushes JSON lines at it.

    `on_message` is awaited for every object the device sends. `on_connect`
    is awaited once per successful connection, which is where the host is
    expected to send its time sync, owner name, and first snapshot.
    """

    def __init__(
        self,
        address: str | None = None,
        on_message: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_connect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.address = address
        self._on_message = on_message
        self._on_connect = on_connect
        self._client: BleakClient | None = None
        self._buffer = LineBuffer()
        self._write_lock = asyncio.Lock()
        self._disconnected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def send(self, obj: dict[str, Any]) -> bool:
        """Best-effort write. A dropped link is normal, not an error."""
        client = self._client
        if client is None or not client.is_connected:
            return False
        try:
            async with self._write_lock:
                await client.write_gatt_char(NUS_RX, encode_line(obj), response=False)
            return True
        except (BleakError, OSError, asyncio.TimeoutError) as exc:
            log.debug("write failed: %s", exc)
            return False

    def _handle_notify(self, _sender: Any, data: bytearray) -> None:
        for obj in self._buffer.feed(bytes(data)):
            if self._on_message is not None:
                asyncio.get_running_loop().create_task(self._on_message(obj))

    async def run(self) -> None:
        """Connect, stay connected, reconnect forever. Never returns."""
        backoff = RECONNECT_MIN
        while True:
            try:
                address = await _find(self.address)
                if address is None:
                    log.info("no buddy device found, retrying in %.0fs", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)
                    continue
                await self._session(address)
                backoff = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except (BleakError, OSError) as exc:
                log.info("link error (%s), retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    async def _session(self, address: str) -> None:
        self._disconnected.clear()
        self._buffer = LineBuffer()

        def on_disconnect(_client: BleakClient) -> None:
            log.info("device disconnected")
            self._disconnected.set()

        log.info("connecting to %s", address)
        async with BleakClient(address, disconnected_callback=on_disconnect) as client:
            self._client = client
            try:
                await client.start_notify(NUS_TX, self._handle_notify)
                log.info("connected")
                if self._on_connect is not None:
                    await self._on_connect()
                await self._disconnected.wait()
            finally:
                self._client = None
