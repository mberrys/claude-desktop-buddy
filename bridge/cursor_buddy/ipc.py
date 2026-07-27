"""Loopback IPC between the short-lived hook processes and the daemon.

Cursor spawns a fresh process per hook event, but the BLE connection has to
outlive them, so the hooks are thin clients that talk newline-delimited
JSON to a long-running daemon over 127.0.0.1.

The socket is loopback-only, but every local process on a Windows box can
reach loopback, so requests carry a token from a file in the user profile.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path
from typing import Any

from .state import STATE_DIR

ENDPOINT_FILE = STATE_DIR / "endpoint.json"
DEFAULT_PORT = 8787


class IpcError(RuntimeError):
    pass


def _env_port() -> int | None:
    raw = os.environ.get("CURSOR_BUDDY_PORT")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def write_endpoint(port: int) -> str:
    """Called by the daemon at startup. Returns the token it should expect."""
    token = secrets.token_hex(16)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ENDPOINT_FILE.write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
    try:
        os.chmod(ENDPOINT_FILE, 0o600)
    except OSError:
        pass
    return token


def read_endpoint() -> tuple[int, str]:
    try:
        data = json.loads(ENDPOINT_FILE.read_text("utf-8"))
        port, token = int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError) as exc:
        raise IpcError(f"no daemon endpoint at {ENDPOINT_FILE}") from exc
    return _env_port() or port, token


def listen_port() -> int:
    return _env_port() or DEFAULT_PORT


def request(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Send one request, wait for one response. Blocking, no asyncio."""
    port, token = read_endpoint()
    payload = dict(payload, token=token)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise IpcError("daemon closed the connection")
                buf.extend(chunk)
    except OSError as exc:
        raise IpcError(str(exc)) from exc
    try:
        reply = json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))
    except ValueError as exc:
        raise IpcError("malformed reply from daemon") from exc
    if not isinstance(reply, dict):
        raise IpcError("malformed reply from daemon")
    return reply


def daemon_alive() -> bool:
    try:
        return bool(request({"type": "ping"}, timeout=2.0).get("ok"))
    except IpcError:
        return False
