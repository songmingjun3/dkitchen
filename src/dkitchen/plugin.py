"""Install the DeepSeek Harness plugin bundle, after checking what it needs.

The harness's own `plugin_manager` installs bundles: writing the profile's `package.json`
or `cordis.patch.yml` by hand is explicitly not supported, and running pnpm in the profile
directory leaves the profile in a state the manager cannot reconcile. So this module does
two things only: it proves the machine can serve the bundle, and it hands the bundle
directory to the harness.

One profile needs a word of warning. The desktop application holds a process-lifetime lock
on its own profile, so `dsh --profile desktop --dump-config` refuses to run and the
composition cannot be inspected from the command line after installing. The install itself
works: the dependency, the `dsh.profile.bundles` entry, the `node_modules` link, and the
lockfile entry all land, and the application's plugin manager keeps them when it installs
something else. Only the post-install verification has to happen on the Plugins page.

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

#: The profile the desktop application boots, which is the one a user means by "my
#: harness". It can be installed into from the command line; only its composition cannot
#: be dumped afterwards, because the application holds a process-lifetime lock on it.
DESKTOP_PROFILE = "desktop"

#: Where `ding plugin install` goes when no profile is named.
DEFAULT_PROFILE = DESKTOP_PROFILE

#: The profile `ding` drives sessions through, which `harness init` writes.
KITCHEN_PROFILE = paths.DEFAULT_PROFILE

#: Bundles that publish the provider routes a station can select, installable from the
#: registry so a machine with no configured profile can still run a kitchen.
#:
#: They are named rather than discovered because the routes live *inside* each plugin's own
#: `cordis.patch.yml` — `dsh-llm-agentrouter` declares its `agentrouter` provider there,
#: and `dsh-wtq-pool` registers `xdpool-*` at runtime — so installing the plugin is the
#: only way to get the route. Copying them from a donor profile (what `harness init` does)
#: cannot work on a fresh machine, where no donor has them yet.
ROUTE_BUNDLES = (
    "dsh-wtq-pool",
    "dsh-llm-agentrouter",
    "dsh-openai-codex-auth",
    "dsh-our-free-model",
)

#: The bundle that turns a profile into an ACP application, which is what `ding` drives.
#: A route-only profile cannot serve sessions without it.
ACP_BUNDLE = "@deepseek-ai/dsh-acp-app"


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

    # The kitchen profile is what `ding` drives sessions through. It need not exist yet —
    # `install` writes it — but a machine with no donor profile and no route bundles is
    # reported here, because that combination produces a profile nothing can run on.
    kitchen = paths.profile_dir(KITCHEN_PROFILE)
    if (kitchen / "package.json").is_file():
        missing = routes_present(KITCHEN_PROFILE)
        rows.append((f"kitchen profile ({KITCHEN_PROFILE})", True,
                     str(kitchen) if not missing
                     else f"{kitchen}; install adds {', '.join(missing)}"))
    else:
        rows.append((f"kitchen profile ({KITCHEN_PROFILE})", True,
                     f"not created yet; install writes it at {kitchen}"))
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


def _launcher_add(profile: str, spec: str) -> None:
    """Add one package spec to `profile` through the harness's own launcher.

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
    command = [str(executable), "plugin", "--profile", profile, "add", spec]
    try:
        # pnpm writes UTF-8; a Windows console's GBK default would fail to decode it and
        # raise inside the reader thread instead of reporting the real outcome.
        result = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        raise ValueError(f"dsh plugin add {spec} did not finish within 600s")
    except OSError as error:
        raise ValueError(f"cannot run the harness launcher: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(detail or f"dsh plugin add {spec} exited {result.returncode}")


def install_into(profile: str) -> None:
    """Add this package's bundle to `profile`."""
    _launcher_add(profile, f"link:{bundle_dir()}")


def profile_packages(profile: str) -> dict:
    """A profile's declared dependencies, empty when it has no manifest yet."""
    manifest = paths.profile_dir(profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    dependencies = data.get("dependencies")
    return dependencies if isinstance(dependencies, dict) else {}


def routes_present(profile: str) -> list[str]:
    """Which route bundles the profile has not declared yet."""
    have = profile_packages(profile)
    return [name for name in ROUTE_BUNDLES if name not in have]


def ensure_routes(profile: str) -> list[str]:
    """Install every route bundle the profile lacks. Returns the names it added.

    `harness init` copies a donor profile's plugins, which is the right thing on a machine
    that already runs the harness. On a fresh machine there is no donor to copy, so the
    kitchen profile comes out with no provider routes and no session can run. Naming the
    bundles here is what makes a first-time install self-sufficient.
    """
    added: list[str] = []
    for name in routes_present(profile):
        _launcher_add(profile, name)
        added.append(name)
    return added


def _settings_for_install():
    """The repo settings `harness init` needs, resolved from the current directory."""
    from dkitchen import config as config_module
    return config_module.load(Path.cwd())


def _init_profile(settings, profile: str) -> int:
    """Write the kitchen profile through `ding harness init`, the supported path."""
    from dkitchen import ding as ding_module
    return ding_module.harness_init(settings, profile=profile, force=False)


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
        print(f"\nding: would create profile {KITCHEN_PROFILE!r} if missing, add "
              f"{', '.join(routes_present(KITCHEN_PROFILE)) or 'no'} route bundle(s), "
              f"and install {bundle} into profile {target!r}", file=sys.stderr)
        return 0
    # 1. The kitchen profile, which `ding` drives sessions through. Asking the harness to
    #    compose it is the only supported way to write it.
    if not (paths.profile_dir(KITCHEN_PROFILE) / "package.json").is_file():
        print(f"ding: creating the {KITCHEN_PROFILE!r} profile", file=sys.stderr)
        code = _init_profile(_settings_for_install(), KITCHEN_PROFILE)
        if code:
            print(f"ding: could not create profile {KITCHEN_PROFILE!r}; see the message above.",
                  file=sys.stderr)
            return code
    # 2. The route bundles that profile needs, so a machine with no donor profile still
    #    has something to run. Without them the profile composes but cannot serve a session.
    try:
        added = ensure_routes(KITCHEN_PROFILE)
    except ValueError as error:
        print(f"\nding: could not add the route bundles: {error}", file=sys.stderr)
        return 1
    if added:
        print(f"ding: added route bundle(s) to {KITCHEN_PROFILE!r}: {', '.join(added)}",
              file=sys.stderr)
    else:
        print(f"ding: profile {KITCHEN_PROFILE!r} already has every route bundle", file=sys.stderr)
    # 3. The GUI half: the MCP bundle, in the profile a user opens conversations in.
    try:
        install_into(target)
    except ValueError as error:
        print(f"\nding: {error}", file=sys.stderr)
        print(_plugin_manager_hint(), file=sys.stderr)
        return 1
    print(f"\nding: installed {bundle} into profile {target!r}", file=sys.stderr)
    print(f"Restart the harness (or reload the profile) to load mcp__dkitchen__* tools.",
          file=sys.stderr)
    if target == DESKTOP_PROFILE:
        # The desktop application holds a process-lifetime lock on its profile, so the
        # composition cannot be dumped from here to prove the bundle loaded. Say where to
        # look instead of implying a verification that did not happen.
        print(f"The {DESKTOP_PROFILE!r} profile belongs to the desktop application, so its "
              "composition cannot be dumped from the command line; confirm the dkitchen "
              "row on its Plugins page.", file=sys.stderr)
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
