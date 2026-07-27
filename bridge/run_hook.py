#!/usr/bin/env python3
"""Entry point Cursor invokes for every hook event.

A plain script rather than `python -m cursor_buddy hook` so the command in
hooks.json never depends on the working directory Cursor happens to use.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cursor_buddy.hook import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
