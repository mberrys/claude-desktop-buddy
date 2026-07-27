"""Loopback HTTP endpoint so anything can drive the buddy.

Codex hooks, a ChatGPT desktop automation, a build script — anything that can
POST JSON to localhost can push status onto the device and (unlike the log
tail) read back the approve/deny the user pressed on the stick.

    POST /snapshot   {"total":1,"running":1,"msg":"building"}   merge fields
    POST /entry      {"text":"yarn test"}                        push a line
    POST /tokens     {"n":1200}                                  add tokens
    POST /prompt     {"id":"x","tool":"Shell","hint":"rm -rf"}   raise a prompt
    DELETE /prompt   (or POST /prompt/clear)                     drop it
    GET  /decision?id=x&wait=30   -> {"decision":"once"|"deny"|null}
    GET  /state                   -> the snapshot as sent to the device

Bound to 127.0.0.1 by default. There is no auth, so do not bind it to a
routable address: anything that can reach the port can approve tool calls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..state import Prompt, Snapshot

log = logging.getLogger("buddy.http")

MAX_BODY = 256 * 1024


class DecisionBox:
    """Holds the device's answer to each prompt id until someone reads it."""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self._ttl = ttl_s
        self._lock = threading.Condition()
        self._answers: dict[str, tuple[str, float]] = {}

    def put(self, prompt_id: str, decision: str) -> None:
        with self._lock:
            self._answers[prompt_id] = (decision, time.monotonic())
            self._lock.notify_all()

    def get(self, prompt_id: str, wait_s: float = 0.0) -> str | None:
        deadline = time.monotonic() + wait_s
        with self._lock:
            while True:
                self._expire_locked()
                hit = self._answers.pop(prompt_id, None)
                if hit is not None:
                    return hit[0]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._lock.wait(min(remaining, 1.0))

    def _expire_locked(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for key in [k for k, (_, at) in self._answers.items() if at < cutoff]:
            del self._answers[key]


class _Handler(BaseHTTPRequestHandler):
    server_version = "BuddyBridge/1.0"
    snapshot: Snapshot
    decisions: DecisionBox

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        blob = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path)
        if route.path == "/state":
            self._reply(200, self.snapshot.wire())
            return
        if route.path == "/decision":
            query = parse_qs(route.query)
            prompt_id = (query.get("id") or [""])[0]
            if not prompt_id:
                self._reply(400, {"error": "id required"})
                return
            try:
                wait_s = min(float((query.get("wait") or ["0"])[0]), 600.0)
            except ValueError:
                wait_s = 0.0
            self._reply(200, {"decision": self.decisions.get(prompt_id, wait_s)})
            return
        self._reply(404, {"error": "no such route"})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib naming
        if urlparse(self.path).path == "/prompt":
            self.snapshot.clear_prompt()
            self._reply(200, {"ok": True})
            return
        self._reply(404, {"error": "no such route"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)})
            return

        if route == "/snapshot":
            allowed = {"total", "running", "waiting", "msg", "entries", "tokens", "tokens_today"}
            fields = {k: v for k, v in body.items() if k in allowed}
            if "entries" in fields and not isinstance(fields["entries"], list):
                self._reply(400, {"error": "entries must be a list"})
                return
            self.snapshot.update(**fields)
            self._reply(200, {"ok": True})
            return

        if route == "/entry":
            text = body.get("text") or body.get("entry") or ""
            if not text:
                self._reply(400, {"error": "text required"})
                return
            self.snapshot.add_entry(str(text))
            if body.get("msg"):
                self.snapshot.update(msg=str(body["msg"]))
            self._reply(200, {"ok": True})
            return

        if route == "/tokens":
            try:
                self.snapshot.add_tokens(int(body.get("n") or 0))
            except (TypeError, ValueError):
                self._reply(400, {"error": "n must be an integer"})
                return
            self._reply(200, {"ok": True})
            return

        if route == "/prompt":
            prompt_id = str(body.get("id") or "")
            if not prompt_id:
                self._reply(400, {"error": "id required"})
                return
            self.snapshot.set_prompt(
                Prompt(
                    id=prompt_id,
                    tool=str(body.get("tool") or "Tool"),
                    hint=str(body.get("hint") or ""),
                )
            )
            self.snapshot.update(waiting=1, msg=f"approve: {body.get('tool') or 'Tool'}")
            self._reply(200, {"ok": True})
            return

        if route == "/prompt/clear":
            self.snapshot.clear_prompt(str(body.get("id")) if body.get("id") else None)
            self.snapshot.update(waiting=0)
            self._reply(200, {"ok": True})
            return

        self._reply(404, {"error": "no such route"})


def start(
    snapshot: Snapshot, host: str = "127.0.0.1", port: int = 8787
) -> tuple[ThreadingHTTPServer, DecisionBox]:
    decisions = DecisionBox()
    handler = type("Handler", (_Handler,), {"snapshot": snapshot, "decisions": decisions})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="buddy-http", daemon=True).start()
    log.info("control endpoint on http://%s:%d", host, port)
    return server, decisions
