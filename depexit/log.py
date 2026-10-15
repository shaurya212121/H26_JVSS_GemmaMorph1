"""Minimal progress logging to stderr (UTF-8 safe on Windows consoles)."""

from __future__ import annotations

import sys


def log(message: str) -> None:
    try:
        print(message, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)
