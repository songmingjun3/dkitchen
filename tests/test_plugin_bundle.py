"""The DeepSeek Harness plugin bundle: its manifest, its patch, and its display resources.

These tests read the bundle the way the harness does — manifest, then patch, then locale
and icon — so a broken bundle fails here instead of on someone's Plugins page. Nothing
here installs anything: the harness's `plugin_manager` owns installation.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import pytest

from dkitchen import plugin

BUNDLE = Path(__file__).resolve().parents[1] / "src/dkitchen/harness/dsh-dkitchen"

#: The MCP client ships with the harness, so a bundle naming it declares no dependency.
SHIPPED_CLIENT = "@deepseek-ai/dsh-mcp-client"


def manifest() -> dict:
    return json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))


def patch() -> str:
    return (BUNDLE / "cordis.patch.yml").read_text(encoding="utf-8")


def test_the_manifest_points_at_a_patch_the_harness_can_read():
    data = manifest()
    assert data["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert (BUNDLE / "cordis.patch.yml").is_file()
    assert data["private"] is True


def test_the_bundle_declares_no_dependencies():
    """Everything it names ships with the harness; a dependency would need a second copy."""
    data = manifest()
    assert not data.get("dependencies"), "a declarative bundle needs no dependencies"
    assert not data.get("devDependencies")


def test_the_patch_inserts_the_shipped_mcp_client():
    text = patch()
    assert SHIPPED_CLIENT in text
    assert "serverName: dkitchen" in text
    assert "transport: stdio" in text
    assert re.search(r"args:\s*\[\s*['\"]mcp-serve['\"]\s*\]", text)


def test_the_patch_lets_the_environment_name_the_ding_executable():
    """The harness scrubs the environment, so a pipx `ding` may not be on that PATH.

    `DKITCHEN_DING` is how a user points at one, and the bare name stays the default.
    """
    text = patch()
    assert re.search(r"command:\s*!!js\s+process\.env\.DKITCHEN_DING\s*\|\|\s*'ding'", text)


def test_the_patch_does_not_stop_the_harness_when_ding_is_missing():
    """A missing `ding` must not take the whole profile down with it."""
    assert "failOnStartupError: false" in patch()


def test_the_bundle_ships_a_client_half_for_its_page():
    """The Plugins page shows a section only for a bundle whose client half registers one."""
    data = manifest()
    exports = data["exports"]
    assert exports["./client"] == "./client.js"
    client = data["dsh"]["client"]
    assert client["platform"] == "web"
    assert "@deepseek-ai/dsh-client-ui-plugin-manager" in client["inject"]
    assert (BUNDLE / "client.js").is_file()
    assert (BUNDLE / "index.js").is_file()


def test_the_client_half_registers_into_the_detail_section_slot():
    """`plugins.bundle.config` is a settings form; a read-only report belongs in a section."""
    source = (BUNDLE / "client.js").read_text(encoding="utf-8")
    assert "ctx.slots.inject('plugins.detail.section'" in source
    assert "ctx.slots.inject('plugins.bundle.config'" not in source
    assert "subject.kind !== 'bundle'" in source


def test_the_manifest_files_list_covers_the_client_half():
    """`files` is what a packed bundle keeps; a missing entry silently drops a module."""
    files = manifest()["files"]
    assert "index.js" in files
    assert "client.js" in files


def test_every_locale_has_a_title_and_a_description():
    locales = sorted((BUNDLE / "locale").glob("*.json"))
    assert {path.stem for path in locales} >= {"en", "zh"}
    for path in locales:
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        assert meta["title"].strip(), f"{path.name} needs a title"
        assert meta["description"].strip(), f"{path.name} needs a description"


def test_every_locale_defines_the_keys_the_page_asks_for():
    """A missing key renders as its own name, so the page would show `installPython`."""
    source = (BUNDLE / "client.js").read_text(encoding="utf-8")
    wanted = set(re.findall(r"t\('([a-zA-Z]+)'\)", source))
    assert wanted, "the page should ask the dictionary for its copy"
    for path in sorted((BUNDLE / "locale").glob("*.json")):
        dictionary = json.loads(path.read_text(encoding="utf-8"))
        missing = sorted(wanted - set(dictionary))
        assert not missing, f"{path.name} is missing {', '.join(missing)}"


def test_the_page_names_pip_as_the_default_installer():
    """`pipx` is not installed on every machine; `pip` into the user's interpreter is."""
    for path in sorted((BUNDLE / "locale").glob("*.json")):
        dictionary = json.loads(path.read_text(encoding="utf-8"))
        assert "pip install dkitchen" in dictionary["installPython"]
        assert dictionary["installPython"].split()[:3] == ["python", "-m", "pip"]
        assert dictionary["installFinish"] == "ding plugin install"


def test_the_agent_message_says_what_to_run_and_to_wait_for_consent():
    """The page cannot start an install, but a session's shell tool can; this is its brief."""
    for path in sorted((BUNDLE / "locale").glob("*.json")):
        message = json.loads(path.read_text(encoding="utf-8"))["agentMessage"]
        assert "pip install dkitchen" in message
        assert "ding plugin install" in message
        assert "restart" in message.lower() or "重启" in message


def test_the_icon_is_an_svg_the_harness_accepts():
    """The harness takes SVG/PNG/JPEG/WebP up to 256 KiB, inside the package."""
    icon = BUNDLE / "icon.svg"
    assert icon.is_file()
    raw = icon.read_bytes()
    assert len(raw) <= 256 * 1024
    assert b"<svg" in raw


def test_the_manifest_exports_the_locale_and_the_icon():
    exports = manifest()["exports"]
    assert "./locale/*.json" in exports
    assert "./icon" in exports


def test_the_manifest_files_list_covers_every_shipped_resource():
    """`files` is what a packed bundle keeps; a missing entry silently drops a resource."""
    files = manifest()["files"]
    assert "cordis.patch.yml" in files
    assert any("locale" in entry for entry in files)
    assert any("icon" in entry for entry in files)


def test_the_engines_range_admits_the_installed_harness():
    """The bundle was written against the 0.2 line; a narrower range would refuse to load."""
    engines = manifest()["engines"]["dsh"]
    assert "0.2" in engines


def test_bundle_dir_finds_the_package_copy():
    found = plugin.bundle_dir()
    assert (found / "package.json").is_file()
    assert found == BUNDLE


def test_check_reports_every_prerequisite(monkeypatch):
    rows = plugin.check()
    names = [name for name, _, _ in rows]
    assert "dsh launcher" in names
    assert "plugin bundle" in names
    assert "state root" in names
    assert "gh (ticket tools)" in names
    # The bundle row must pass: it is the thing this package ships.
    assert dict((name, ok) for name, ok, _ in rows)["plugin bundle"] is True


def test_a_missing_launcher_blocks_installation(monkeypatch):
    monkeypatch.setattr(plugin.paths, "dsh_executable",
                        lambda: (_ for _ in ()).throw(ValueError("no dsh launcher found")))
    assert plugin.install(dry_run=True) == 1


def test_dry_run_names_every_step_and_installs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(plugin, "install_into",
                        lambda profile: pytest.fail("a dry run must not install"))
    monkeypatch.setattr(plugin, "ensure_routes",
                        lambda profile: pytest.fail("a dry run must not add bundles"))
    assert plugin.install(dry_run=True) == 0
    err = capsys.readouterr().err
    assert "would create profile" in err
    assert "route bundle(s)" in err
    assert "ding: installed" not in err


def test_the_route_bundles_are_the_ones_that_publish_routes():
    """Each named bundle carries a provider route in its own patch, or at runtime."""
    assert "dsh-wtq-pool" in plugin.ROUTE_BUNDLES          # xdpool-*, at runtime
    assert "dsh-llm-agentrouter" in plugin.ROUTE_BUNDLES   # agentrouter, in its patch
    assert plugin.ACP_BUNDLE == "@deepseek-ai/dsh-acp-app"


def test_routes_present_reports_only_what_the_profile_lacks(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.paths, "dsh_home", lambda: tmp_path)
    profile = tmp_path / "profiles" / "kitchen"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps({"dependencies": {"dsh-wtq-pool": "^1.0.4"}}), encoding="utf-8")
    missing = plugin.routes_present("kitchen")
    assert "dsh-wtq-pool" not in missing
    assert set(missing) == set(plugin.ROUTE_BUNDLES) - {"dsh-wtq-pool"}


def test_routes_present_reports_everything_for_a_profile_that_does_not_exist(
        tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.paths, "dsh_home", lambda: tmp_path)
    assert set(plugin.routes_present("absent")) == set(plugin.ROUTE_BUNDLES)


def test_ensure_routes_adds_only_the_missing_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.paths, "dsh_home", lambda: tmp_path)
    profile = tmp_path / "profiles" / "kitchen"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps({"dependencies": {plugin.ROUTE_BUNDLES[0]: "*"}}), encoding="utf-8")
    added = {}
    monkeypatch.setattr(plugin, "_launcher_add",
                        lambda target, spec: added.setdefault(spec, target))
    assert plugin.ensure_routes("kitchen") == list(plugin.ROUTE_BUNDLES[1:])
    assert set(added) == set(plugin.ROUTE_BUNDLES[1:])


def test_install_creates_the_kitchen_profile_and_its_routes(monkeypatch, capsys):
    """A machine with no donor profile must still come out with something runnable."""
    calls = []
    monkeypatch.setattr(plugin.paths, "profile_dir", lambda name: Path("/nowhere") / name)
    monkeypatch.setattr(plugin, "_settings_for_install", lambda: object())
    monkeypatch.setattr(plugin, "_init_profile",
                        lambda settings, profile: calls.append(("init", profile)) or 0)
    monkeypatch.setattr(plugin, "ensure_routes",
                        lambda profile: calls.append(("routes", profile)) or ["dsh-wtq-pool"])
    monkeypatch.setattr(plugin, "install_into",
                        lambda profile: calls.append(("bundle", profile)))
    assert plugin.install(profile="desktop") == 0
    assert ("init", plugin.KITCHEN_PROFILE) in calls
    assert ("routes", plugin.KITCHEN_PROFILE) in calls
    assert ("bundle", "desktop") in calls
    err = capsys.readouterr().err
    assert "creating the 'dkitchen' profile" in err


def test_a_failed_kitchen_profile_stops_before_installing_the_gui_bundle(
        monkeypatch, capsys):
    """A profile that cannot be composed makes every other step pointless."""
    monkeypatch.setattr(plugin.paths, "profile_dir", lambda name: Path("/nowhere") / name)
    monkeypatch.setattr(plugin, "_settings_for_install", lambda: object())
    monkeypatch.setattr(plugin, "_init_profile", lambda settings, profile: 1)
    monkeypatch.setattr(plugin, "ensure_routes",
                        lambda profile: pytest.fail("must not add routes to a broken profile"))
    monkeypatch.setattr(plugin, "install_into",
                        lambda profile: pytest.fail("must not install the bundle"))
    assert plugin.install(profile="desktop") == 1
    assert "could not create profile" in capsys.readouterr().err


def test_an_existing_kitchen_profile_is_not_reinitialized(monkeypatch, capsys):
    """`harness init` refuses to overwrite without `--force`, so only create when absent."""
    monkeypatch.setattr(plugin.paths, "profile_dir", lambda name: BUNDLE)
    monkeypatch.setattr(plugin, "_init_profile",
                        lambda settings, profile: pytest.fail("must not rewrite a profile"))
    monkeypatch.setattr(plugin, "ensure_routes", lambda profile: [])
    monkeypatch.setattr(plugin, "install_into", lambda profile: None)
    assert plugin.install(profile="desktop") == 0
    err = capsys.readouterr().err
    assert "creating the" not in err


def test_install_uses_the_harness_launcher_and_names_the_profile(monkeypatch, capsys):
    """The bundle goes in through `dsh plugin add link:<dir>`, not by editing the profile."""
    calls = {}
    monkeypatch.setattr(plugin, "install_into", lambda profile: calls.setdefault("profile", profile))
    assert plugin.install(profile="probe") == 0
    assert calls["profile"] == "probe"
    assert "installed" in capsys.readouterr().err


def test_the_default_profile_is_the_desktop_one():
    """That is the profile the application boots, so the one a user means by 'my harness'."""
    assert plugin.DEFAULT_PROFILE == plugin.DESKTOP_PROFILE == "desktop"


def test_installing_into_desktop_warns_that_verification_is_on_the_plugins_page(
        monkeypatch, capsys):
    """The app locks its profile, so the composition cannot be dumped from the CLI."""
    monkeypatch.setattr(plugin, "install_into", lambda profile: None)
    assert plugin.install(profile="desktop") == 0
    err = capsys.readouterr().err
    assert "installed" in err
    assert "Plugins page" in err


def test_a_custom_profile_gets_no_desktop_warning(monkeypatch, capsys):
    monkeypatch.setattr(plugin, "install_into", lambda profile: None)
    assert plugin.install(profile="kitchen") == 0
    assert "Plugins page" not in capsys.readouterr().err


def test_install_into_forwards_a_link_spec_to_the_launcher(monkeypatch):
    seen = {}

    class Finished:
        returncode = 0
        stdout = "Done"
        stderr = ""

    def fake_run(command, **kwargs):
        seen["command"] = command
        return Finished()

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    plugin.install_into("probe")
    command = seen["command"]
    assert command[1:4] == ["plugin", "--profile", "probe"]
    assert command[4] == "add"
    assert command[5].startswith("link:")
    assert command[5].endswith("dsh-dkitchen")


def test_a_failed_launcher_reports_its_own_message(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "ERR_PNPM_NO_MATCHING_VERSION no matching version"

    monkeypatch.setattr(plugin.subprocess, "run", lambda command, **kw: Failed())
    with pytest.raises(ValueError, match="ERR_PNPM_NO_MATCHING_VERSION"):
        plugin.install_into("probe")


def test_installed_profiles_reads_the_profile_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.paths, "dsh_home", lambda: tmp_path)
    (tmp_path / "profiles" / "probe").mkdir(parents=True)
    (tmp_path / "profiles" / "probe" / "package.json").write_text(
        json.dumps({"dependencies": {"dsh-dkitchen": "link:x"}}), encoding="utf-8")
    (tmp_path / "profiles" / "other").mkdir()
    (tmp_path / "profiles" / "other" / "package.json").write_text(
        json.dumps({"dependencies": {}}), encoding="utf-8")
    assert plugin.installed_profiles() == ["probe"]


def test_package_data_ships_every_bundle_file_the_harness_loads():
    """`package-data` is the wheel's allowlist: a `.js` left out ships a bundle that cannot run.

    This is a regression test: the client half was added to the bundle after the pattern
    list was written, so the wheel carried a manifest that named a `client.js` the install
    did not contain.
    """
    pyproject = (BUNDLE.parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    lines = pyproject[pyproject.index("[tool.setuptools.package-data]"):].splitlines()
    # The values start on the line that assigns `dkitchen = [...]` and end at the first
    # line whose last character closes that list.
    body = []
    started = False
    for line in lines:
        if line.lstrip().startswith("dkitchen"):
            started = True
        if started:
            body.append(line)
            if line.rstrip().endswith("]"):
                break
    patterns = re.findall(r'"([^"]+)"', "\n".join(body))
    assert patterns, "could not read the package-data patterns"
    for name in ("index.js", "client.js", "cordis.patch.yml", "package.json", "icon.svg",
                 "locale/en.json"):
        suffix = name.rsplit(".", 1)[-1]
        covered = any(pattern.endswith(f"*.{suffix}") or pattern.endswith(name)
                      for pattern in patterns)
        assert covered, f"package-data has no pattern covering {name}"


def test_an_unbooted_desktop_profile_gets_a_sentence_not_a_stack_trace(monkeypatch):
    """The harness's own words assume the reader knows what "initialize its profile" means."""
    class Failed:
        returncode = 1
        stdout = ""
        stderr = ("Error: Open DeepSeek Harness Desktop once to initialize its profile, then "
                  "fully quit it before running dsh plugin --profile desktop.")

    monkeypatch.setattr(plugin.subprocess, "run", lambda command, **kw: Failed())
    with pytest.raises(ValueError) as caught:
        plugin.install_into("desktop")
    message = str(caught.value)
    assert "start DeepSeek Harness once" in message
    # The harness's own text stays, so the reader can match it against other reports.
    assert "initialize its profile" in message


def test_an_unrelated_launcher_failure_is_passed_through_unchanged():
    assert plugin._explain_launcher_error("ERR_PNPM_FETCH_404") is None


def test_check_calls_the_manifest_a_declaration_not_an_installation(monkeypatch, capsys):
    """Reading `dependencies` proves a declaration; the harness reports load failures itself."""
    monkeypatch.setattr(plugin, "installed_profiles", lambda: ["desktop"])
    plugin.main(_Args("check"), None)
    out = capsys.readouterr().out
    assert "declared in profile: desktop" in out
    assert "installed in profile" not in out


class _Args:
    def __init__(self, action, **rest):
        self.plugin_action = action
        self.__dict__.update(rest)


def test_status_is_json_the_configuration_page_can_render(monkeypatch, capsys):
    monkeypatch.setattr(plugin, "installed_profiles", lambda: ["desktop"])
    monkeypatch.setattr(plugin.shutil, "which", lambda name: "/usr/bin/ding"
                        if name == "ding" else None)
    assert plugin.main(_Args("status"), None) == 0
    payload = json.loads(capsys.readouterr().out)
    for key in ("available", "ding", "python", "kitchen_profile", "kitchen_exists",
                "missing_routes", "installed_profiles", "checks"):
        assert key in payload, f"{key} is missing from the status document"
    assert payload["available"] is True
    assert payload["ding"] == "/usr/bin/ding"
    assert payload["installed_profiles"] == ["desktop"]
    assert all({"name", "ok", "detail"} <= set(row) for row in payload["checks"])


def test_check_names_the_interpreter_the_mcp_server_will_use(monkeypatch):
    rows = dict((name, (ok, detail)) for name, ok, detail in plugin.check())
    ok, detail = rows["python for the MCP server"]
    assert ok is True
    assert sys.executable in detail


def test_a_ding_from_another_environment_is_named(monkeypatch):
    """A pipx `ding` beside a different interpreter is what `DKITCHEN_DING` exists for."""
    monkeypatch.setattr(plugin, "_ding_on_path", lambda: "/elsewhere/bin/ding")
    ok, detail = dict((name, (ok, detail))
                      for name, ok, detail in plugin.check())["python for the MCP server"]
    assert ok is True
    assert "DKITCHEN_DING" in detail


@pytest.mark.parametrize("ding, python", [
    # Windows keeps the interpreter at the root and console scripts in Scripts/ below it.
    (r"D:\env\Scripts\ding.EXE", r"D:\env\python.exe"),
    # POSIX keeps both in bin/.
    ("/home/u/.venvs/k/bin/ding", "/home/u/.venvs/k/bin/python"),
])
def test_a_ding_beside_its_own_interpreter_is_not_reported_as_odd(ding, python):
    assert plugin._same_environment(ding, python) is True


@pytest.mark.parametrize("ding, python", [
    ("/other/venv/bin/ding", "/home/u/.venvs/k/bin/python"),
    (r"C:\other\Scripts\ding.exe", r"D:\env\python.exe"),
])
def test_a_ding_from_another_environment_is_distinguishable(ding, python):
    assert plugin._same_environment(ding, python) is False


def test_no_ding_on_path_is_not_called_a_mismatch():
    assert plugin._same_environment(None, sys.executable) is False
