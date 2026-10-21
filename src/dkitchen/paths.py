"""Where dkitchen keeps its state and how it finds the DeepSeek Harness runtime.

Everything dkitchen writes lives under one directory, chosen in this order:

1. `DKITCHEN_STATE_HOME`, for tests and unusual deployments;
2. `XDG_STATE_HOME`, the POSIX convention dkitchen used before Windows support;
3. `%LOCALAPPDATA%` on Windows, where a per-user, non-roaming location belongs;
4. `~/.local/state`, the fallback everywhere else.

The DeepSeek Harness home (`$DSH_HOME`, else `~/.dsh`) is read, never written, except by
`harness` commands that install a profile there.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

WINDOWS = sys.platform == "win32"

DSH_BIN_ENV = "DKITCHEN_DSH_BIN"
DEFAULT_PROFILE = "dkitchen"


def state_root() -> Path:
    """The directory holding every file dkitchen writes."""
    explicit = os.environ.get("DKITCHEN_STATE_HOME")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "ding"
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "dkitchen"
    return Path.home() / ".local" / "state" / "ding"


def state(*parts: str) -> Path:
    return state_root().joinpath(*parts)


def user_key() -> str:
    """A stable, filesystem- and pipe-safe name for the current user."""
    name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    safe = "".join(character if character.isalnum() else "-" for character in name).strip("-")
    return safe or "user"


def dsh_home() -> Path:
    explicit = os.environ.get("DSH_HOME")
    if explicit:
        return Path(explicit)
    return Path.home() / ".dsh"


def sessions_root() -> Path:
    """Where DeepSeek Harness persists session logs."""
    return dsh_home() / "sessions"


def profile_dir(profile: str) -> Path:
    return dsh_home() / "profiles" / profile


#: Places the desktop application installs the `dsh` launcher on each platform.
_KNOWN_DSH = (
    Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd",
    Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"),
    Path.home() / ".local" / "share" / "deepseek-harness" / "runtime" / "cli" / "bin" / "dsh",
)


def dsh_executable() -> str:
    """The `dsh` launcher to spawn: explicit setting first, then PATH, then known installs."""
    explicit = os.environ.get(DSH_BIN_ENV)
    if explicit:
        return explicit
    names = ("dsh.cmd", "dsh.exe", "dsh") if WINDOWS else ("dsh",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in _KNOWN_DSH:
        if str(candidate) and candidate.is_file():
            return str(candidate)
    raise ValueError(
        "cannot find the DeepSeek Harness launcher. Install DeepSeek Harness, put `dsh` on PATH, "
        f"or set {DSH_BIN_ENV} to its full path."
    )
