"""Windows-side bridge: Codex/ChatGPT activity -> Hardware Buddy over BLE.

    python -m buddy_bridge                    tail Codex, connect, run forever
    python -m buddy_bridge --scan             list nearby buddy devices
    python -m buddy_bridge --demo             fake traffic, no Codex needed
    python -m buddy_bridge --push characters/bufo
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from .ble import find_device, serve
from .sources import codex as codex_source
from .sources import http as http_source
from .state import Prompt, Snapshot

log = logging.getLogger("buddy")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="buddy_bridge", description=__doc__)
    parser.add_argument("--name-prefix", default="Claude", help="advertised name prefix to match")
    parser.add_argument("--address", help="connect to this BLE address, skipping the name filter")
    parser.add_argument("--owner", help="name to greet you by on the device")
    parser.add_argument("--sessions-dir", type=Path, help="override the Codex sessions directory")
    parser.add_argument("--no-codex", action="store_true", help="skip the Codex log tail")
    parser.add_argument("--no-http", action="store_true", help="skip the control endpoint")
    parser.add_argument("--host", default="127.0.0.1", help="control endpoint bind address")
    parser.add_argument("--port", type=int, default=8787, help="control endpoint port")
    parser.add_argument("--no-pair", action="store_true", help="don't attempt to bond on connect")
    parser.add_argument("--push", type=Path, metavar="FOLDER", help="push a character pack, then run")
    parser.add_argument("--scan", action="store_true", help="list matching devices and exit")
    parser.add_argument("--demo", action="store_true", help="drive the device with fake traffic")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


async def _scan(args: argparse.Namespace) -> int:
    from bleak import BleakScanner

    print(f"scanning 10s for devices named {args.name_prefix}*…")
    found = await BleakScanner.discover(timeout=10.0)
    matches = [d for d in found if (d.name or "").lower().startswith(args.name_prefix.lower())]
    if not matches:
        print("nothing matched. Is the stick awake (press a button) and bluetooth on?")
        print(f"saw {len(found)} other BLE devices, so the adapter itself is working.")
        return 1
    for device in matches:
        print(f"  {device.address}  {device.name}")
    return 0


async def _demo(snapshot: Snapshot) -> None:
    """Cycle the states so you can check the link without running Codex."""
    scenes = [
        dict(total=1, running=0, waiting=0, msg="idle"),
        dict(total=2, running=2, waiting=0, msg="running tests"),
        dict(total=2, running=1, waiting=1, msg="approve: Shell"),
        dict(total=1, running=0, waiting=0, msg="done"),
    ]
    i = 0
    while True:
        scene = scenes[i % len(scenes)]
        snapshot.update(**scene)
        snapshot.add_entry(f"demo scene {i}: {scene['msg']}")
        snapshot.add_tokens(1500)
        if scene["waiting"]:
            snapshot.set_prompt(Prompt(id=f"demo-{i}", tool="Shell", hint="rm -rf /tmp/foo"))
        else:
            snapshot.clear_prompt()
        i += 1
        await asyncio.sleep(8)


async def _run(args: argparse.Namespace) -> int:
    snapshot = Snapshot()
    decisions = None
    server = None
    codex = None

    if not args.no_http:
        try:
            server, decisions = http_source.start(snapshot, args.host, args.port)
        except OSError as exc:
            log.error("control endpoint failed to bind %s:%d (%s)", args.host, args.port, exc)
            return 1

    if not args.no_codex and not args.demo:
        codex = codex_source.CodexSource(snapshot, args.sessions_dir)
        log.info("tailing Codex sessions in %s", codex.sessions_dir)
        codex.start()

    async def on_decision(prompt_id: str, decision: str) -> None:
        if decisions is not None:
            decisions.put(prompt_id, decision)
        else:
            log.info("dropped decision %s=%s (control endpoint disabled)", prompt_id, decision)

    tasks = [
        asyncio.create_task(
            serve(
                snapshot,
                name_prefix=args.name_prefix,
                address=args.address,
                owner=args.owner,
                on_decision=on_decision,
                push_folder=args.push,
                pair=not args.no_pair,
            )
        )
    ]
    if args.demo:
        tasks.append(asyncio.create_task(_demo(snapshot)))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        if codex is not None:
            codex.stop()
        if server is not None:
            server.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-12s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.push is not None and not args.push.is_dir():
        log.error("--push wants a folder, %s is not one", args.push)
        return 2

    if sys.platform == "win32":
        # WinRT BLE needs the proactor loop, which is already the default on
        # 3.8+; assert it so a policy set elsewhere doesn't break the backend.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    try:
        return asyncio.run(_scan(args) if args.scan else _run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
