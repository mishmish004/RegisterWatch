#!/usr/bin/env python3
"""Kept so existing invocations keep working: runs the GB register through the
shared engine. Everything else goes through the CLI:

    uv run registerwatch ingest gb_ukgc [--force] [--accept-count-delta] [--root DIR]
    uv run registerwatch ingest            # every register
"""

from __future__ import annotations

import pathlib
import sys

if __package__ is None and "registerwatch" not in sys.modules:  # run as a file
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from registerwatch.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["ingest", "gb_ukgc", *sys.argv[1:]]))
