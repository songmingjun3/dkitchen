"""Install the DeepSeek Harness plugin bundle, after checking what it needs.

The harness's own `plugin_manager` installs bundles: writing the profile's `package.json`
or `cordis.patch.yml` by hand is explicitly not supported, and running pnpm in the profile
directory leaves the profile in a state the manager cannot reconcile. So this module does
two things only: it proves the machine can serve the bundle, and it hands the bundle
directory to the harness.

The checks matter because the bundle mounts an MCP server that runs `ding`. Installing it
on a machine without `ding`, or without a reachable harness, produces a plugin whose every
tool call fails — a worse outcome than refusing to install.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dkitchen import paths

#: Where the bundle lives inside a checkout. The wheel carries it too, so an installed
#: `ding` can install the plugin without a checkout.
BUNDLE_NAME = "dsh-dkitchen"

#: The profile the desktop app boots by default, so the one a plugin should reach first.
DEFAULT_PROFILE = "desktop"


def bundle_dir() -> Path:
    """The bundle directory that ships inside the installed `dkitchen` package."""
    beside_package = Path(__file__).resolve().parent / "harness" / BUNDLE_NAME
    if (beside_package / "package.json").is_file():
        return beside_package
    raise ValueError(f"cannot find the {BUNDLE_NAME} bundle inside the installed package; "
                     "reinstall dkitchen")


def check() -> list[tuple[str, bool, str]]:
    """Every prerequisite, as (name, ok, detail). A False row blocks installation."""
    rows: list[tuple[str, bool, str]] = []

    try:
        executable = paths.dsh_executable()
    except ValueError as error:
        rows.append(("dsh launcher", False, str(error)))
    else:
        rows.append(("dsh launcher", True, str(executable)))

    try:
        bundle = bundle_dir()
    except ValueError as error:
        rows.append(("plugin bundle", False, str(error)))
    else:
        missing = [name for name in ("package.json", "cordis.patch.yml", "locale/en.json",
                                     "icon.svg") if not (bundle / name).is_file()]
        rows.append(("plugin bundle", not missing,
                     str(bundle) if not missing else f"missing {', '.join(missing)}"))

    try:
        root = paths.state_root()
        root.mkdir(parents=True, exist_ok=True)
        rows.append(("state root", True, str(root)))
    except OSError as error:
        rows.append(("state root", False, f"{root}: {error}"))

    # `gh` only limits the ticket tools, so its absence is a warning, not a blocker.
    gh = shutil.which("gh")
    rows.append(("gh (ticket tools)", True,
                 gh or "not installed; relay/merge/log will report it when called"))
    return rows


def _plugin_manager_hint() -> str:
    return ("Install it from the harness: open the Web UI's Plugins page and use "
            f"'install bundle' with this directory, or ask a Full-access session to call "
            f"plugin_manager with action=install_bundle and target=<the bundle directory>.")


def installed_profiles() -> list[str]:
    """Every profile that already depends on the bundle."""
    root = paths.dsh_home() / "profiles"
    if not root.is_dir():
        return []
    found = []
    for profile in sorted(root.iterdir()):
        manifest = profile / "package.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if BUNDLE_NAME in (data.get("dependencies") or {}):
            found.append(profile.name)
    return found


def install_into(profile: str) -> None:
    """Add the bundle to `profile` through the harness's own launcher.

    `dsh plugin --profile <name> add <spec>` forwards to pnpm inside the profile and then
    appends the package to `dsh.profile.bundles`, which is exactly what makes the harness
    load it. Reproducing that by hand — writing `package.json`, editing the patch, running
    pnpm — is what the harness's own plugin skill forbids, because a hand-written profile
    is one the manager cannot reconcile later.
    """
    try:
        executable = paths.dsh_executable()
    except ValueError as error:
        raise ValueError(str(error)) from error
    spec = f"link:{bundle_dir()}"
    command = [str(executable), "plugin", "--profile", profile, "add", spec]
    try:
        # pnpm writes UTF-8; a Windows console's GBK default would fail to decode it and
        # raise inside the reader thread instead of reporting the real outcome.
        result = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        raise ValueError(f"dsh plugin add did not finish within 600s")
    except OSError as error:
        raise ValueError(f"cannot run the harness launcher: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(detail or f"dsh plugin add exited {result.returncode}")


def install(*, dry_run: bool = False, profile: str | None = None) -> int:
    rows = check()
    blocking = [row for row in rows if not row[1]]
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}", file=sys.stderr)
    if blocking:
        print(f"\nding: not installing; {len(blocking)} check(s) failed.", file=sys.stderr)
        return 1
    target = profile or DEFAULT_PROFILE
    bundle = bundle_dir()
    if dry_run:
        print(f"\nding: would install {bundle} into profile {target!r}", file=sys.stderr)
        return 0
    try:
        install_into(target)
    except ValueError as error:
        print(f"\nding: {error}", file=sys.stderr)
        print(_plugin_manager_hint(), file=sys.stderr)
        return 1
    print(f"\nding: installed {bundle} into profile {target!r}", file=sys.stderr)
    print(f"Restart the harness (or reload the profile) to load mcp__dkitchen__* tools.",
          file=sys.stderr)
    return 0


def main(args, config_) -> int:
    action = getattr(args, "plugin_action", None) or "check"
    if action == "check":
        for name, ok, detail in check():
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        for profile in installed_profiles():
            print(f"ok   installed in profile: {profile}")
        return 0 if all(ok for _, ok, _ in check()) else 1
    if action == "install":
        return install(dry_run=getattr(args, "dry_run", False),
                       profile=getattr(args, "profile", None))
    raise ValueError(f"unknown plugin action: {action}")
