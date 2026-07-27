"""CLI: `python -m cursor_buddy <command>`"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cursor_buddy", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_daemon = sub.add_parser("daemon", help="run the BLE bridge (keep this running)")
    p_daemon.add_argument("--address", help="BLE address; default is the first Claude* device")
    p_daemon.add_argument("--owner", help="your first name, shown on the device")

    sub.add_parser("scan", help="list nearby buddy devices")
    sub.add_parser("status", help="check whether the daemon is up and connected")

    p_install = sub.add_parser("install", help="write .cursor/hooks.json for a workspace")
    p_install.add_argument("target", nargs="?", default=".", help="workspace root (default: cwd)")
    p_install.add_argument(
        "--user",
        action="store_true",
        help="install into ~/.cursor so every workspace uses the bridge",
    )

    sub.add_parser("hook", help="handle one hook event on stdin (called by Cursor)")

    args = parser.parse_args(argv)

    if args.command == "hook":
        from .hook import main as hook_main

        return hook_main([])

    _configure_logging(args.verbose)

    if args.command == "daemon":
        from .daemon import Daemon

        try:
            asyncio.run(Daemon(address=args.address, owner=args.owner).run())
        except KeyboardInterrupt:
            pass
        return 0

    if args.command == "scan":
        from .ble import scan

        devices = asyncio.run(scan())
        if not devices:
            print("no devices found — is the stick awake and is Bluetooth on?")
            return 1
        for address, name in devices:
            print(f"{address}  {name}")
        return 0

    if args.command == "status":
        from . import ipc

        try:
            reply = ipc.request({"type": "ping"}, timeout=3.0)
        except ipc.IpcError as exc:
            print(f"daemon not running ({exc})")
            return 1
        print("daemon running, device " + ("connected" if reply.get("connected") else "disconnected"))
        return 0

    if args.command == "install":
        from .install import install

        target = Path.home() if args.user else Path(args.target).resolve()
        path = install(target)
        print(f"wrote {path}")
        print("Restart Cursor (or reload the window) to pick up the hooks.")
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
