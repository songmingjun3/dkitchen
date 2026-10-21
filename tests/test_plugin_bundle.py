"""The DeepSeek Harness plugin bundle: its manifest, its patch, and its display resources.

These tests read the bundle the way the harness does — manifest, then patch, then locale
and icon — so a broken bundle fails here instead of on someone's Plugins page. Nothing
here installs anything: the harness's `plugin_manager` owns installation.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

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
    # The command is `ding mcp-serve`: the MCP server this package provides.
    assert re.search(r"command:\s*ding\b", text)
    assert re.search(r"args:\s*\[\s*['\"]mcp-serve['\"]\s*\]", text)


def test_the_patch_does_not_stop_the_harness_when_ding_is_missing():
    """A missing `ding` must not take the whole profile down with it."""
    assert "failOnStartupError: false" in patch()


def test_every_locale_has_a_title_and_a_description():
    locales = sorted((BUNDLE / "locale").glob("*.json"))
    assert {path.stem for path in locales} >= {"en", "zh"}
    for path in locales:
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        assert meta["title"].strip(), f"{path.name} needs a title"
        assert meta["description"].strip(), f"{path.name} needs a description"


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


def test_dry_run_does_not_claim_to_have_installed(monkeypatch, capsys):
    monkeypatch.setattr(plugin, "install_into",
                        lambda profile: pytest.fail("a dry run must not install"))
    assert plugin.install(dry_run=True) == 0
    err = capsys.readouterr().err
    assert "would install" in err
    assert "ding: installed" not in err


def test_install_uses_the_harness_launcher_and_names_the_profile(monkeypatch, capsys):
    """The bundle goes in through `dsh plugin add link:<dir>`, not by editing the profile."""
    calls = {}
    monkeypatch.setattr(plugin, "install_into", lambda profile: calls.setdefault("profile", profile))
    assert plugin.install(profile="probe") == 0
    assert calls["profile"] == "probe"
    assert "installed" in capsys.readouterr().err


def test_the_default_profile_is_the_desktop_one():
    assert plugin.DEFAULT_PROFILE == "desktop"


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
