"""Wire protocol constants and helpers for the Hardware Buddy BLE link.

Everything here mirrors REFERENCE.md at the repository root. The bridge
plays the role the Claude desktop apps play there: it is the *host*, the
ESP32 is the *device*.
"""

from __future__ import annotations

import json
import time
from typing import Any

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host (notify)

# Devices advertise a name starting with this so the picker can filter.
DEVICE_NAME_PREFIX = "Claude"

# The device treats a snapshot drought of ~30s as a dead link, so keep the
# keepalive comfortably under that.
KEEPALIVE_SECONDS = 10.0


def encode_line(obj: dict[str, Any]) -> bytes:
    """One JSON object per line, UTF-8, newline terminated."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def time_sync() -> dict[str, Any]:
    """`{"time": [epoch_seconds, utc_offset_seconds]}`"""
    now = time.time()
    offset = -(time.altzone if time.localtime(now).tm_isdst else time.timezone)
    return {"time": [int(now), int(offset)]}


def owner_cmd(name: str) -> dict[str, Any]:
    return {"cmd": "owner", "name": name}


def status_cmd() -> dict[str, Any]:
    return {"cmd": "status"}


class LineBuffer:
    """Reassembles newline-delimited JSON from fragmented BLE notifications."""

    def __init__(self, limit: int = 64 * 1024) -> None:
        self._buf = bytearray()
        self._limit = limit

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buf.extend(data)
        if len(self._buf) > self._limit:
            # A device spraying garbage should not grow us without bound.
            del self._buf[: len(self._buf) - self._limit]
        out: list[dict[str, Any]] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            raw = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out
