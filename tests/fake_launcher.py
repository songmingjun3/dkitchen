"""A `dsh` launcher that runs the fake ACP server, for tests that start it like the real one.

`dkitchen.paths.dsh_executable()` is pointed at this file, so `dkitchen.acp.Connection` runs
a real subprocess with real pipes; only the harness itself is simulated.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

FAKE = Path(__file__).resolve().parent / "fake_dsh.py"


def launcher(directory: Path) -> Path:
    """Write a `dsh` executable that ignores the profile flags and runs the fake server."""
    if sys.platform == "win32":
        target = directory / "dsh.cmd"
        target.write_text("@echo off\r\n"
                          f'"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    else:
        target = directory / "dsh"
        target.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(FAKE)!r} \"$@\"\n", encoding="utf-8")
        target.chmod(0o755)
    return target


def environment(directory: Path) -> dict:
    """The environment a test needs so `paths.dsh_executable()` finds the fake launcher."""
    return {**os.environ, "DKITCHEN_DSH_BIN": str(launcher(directory))}
