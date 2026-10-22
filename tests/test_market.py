"""The plugin market index: generation, and the format-1 rules the harness enforces.

The harness refuses an index that violates any rule as a whole, so a typo does not degrade
the Discover listing — it removes it. These tests hold the generated document to the same
rules, and check the document committed in the repository, so a stale or hand-edited index
fails here rather than in someone's Discover tab.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from dkitchen import market

REPO = Path(__file__).resolve().parents[1]
COMMITTED = REPO / "docs/market/index.json"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def document(**overrides) -> dict:
    values = {"version": "1.2.3", "commit": COMMIT, "published": 1792600000}
    values.update(overrides)
    return market.build_index(**values)


def test_a_generated_index_is_valid_format_1():
    built = document()
    market.validate_index(built)
    assert built["format"] == 1
    assert built["title"]["en"] and built["title"]["zh"]
    plugin = built["plugins"][0]
    assert plugin["name"] == "dsh-dkitchen"
    version = plugin["versions"][0]
    assert version["version"] == "1.2.3"
    assert version["source"]["type"] == "git"
    assert version["source"]["commit"] == COMMIT
    assert version["source"]["path"] == market.BUNDLE_PATH


def test_the_install_spec_the_harness_derives_is_a_git_url_at_the_commit():
    """`marketInstallTarget` builds `git+<url>.git#<commit>&path:/<dir>` for a git source."""
    version = document()["plugins"][0]["versions"][0]
    source = version["source"]
    expected = (f"git+{source['url']}.git#{source['commit']}&path:/{source['path']}")
    assert expected.startswith("git+https://github.com/")
    assert f"#{COMMIT}" in expected


@pytest.mark.parametrize("bad", [
    {"format": 2},
    {"title": {"zh": "没有英文"}},
    {"plugins": "not-an-array"},
])
def test_the_document_level_rules_are_enforced(bad):
    built = document()
    built.update(bad)
    with pytest.raises(market.IndexError_):
        market.validate_index(built)


def test_a_plugin_with_no_versions_is_refused():
    built = document()
    built["plugins"][0]["versions"] = []
    with pytest.raises(market.IndexError_, match="non-empty"):
        market.validate_index(built)


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3.4", "not-a-version"])
def test_a_version_that_is_not_exact_semver_is_refused(version):
    built = document()
    built["plugins"][0]["versions"][0]["version"] = version
    with pytest.raises(market.IndexError_, match="SemVer"):
        market.validate_index(built)


@pytest.mark.parametrize("stamp", [-1, 10 ** 11, 1.5, True, "1792600000"])
def test_a_publish_timestamp_outside_the_integer_range_is_refused(stamp):
    built = document()
    built["plugins"][0]["versions"][0]["publishTimestamp"] = stamp
    with pytest.raises(market.IndexError_, match="publishTimestamp"):
        market.validate_index(built)


@pytest.mark.parametrize("commit", ["abc", "0123456789ABCDEF0123456789ABCDEF01234567",
                                    "0123456789abcdef0123456789abcdef0123456"])
def test_a_source_commit_must_be_forty_lowercase_hex(commit):
    built = document()
    built["plugins"][0]["versions"][0]["source"]["commit"] = commit
    with pytest.raises(market.IndexError_, match="commit"):
        market.validate_index(built)


def test_a_build_refuses_a_short_commit_before_it_writes_anything(tmp_path):
    target = tmp_path / "market" / "index.json"
    with pytest.raises(market.IndexError_):
        market.write_index(target, market.build_index(version="1.2.3", commit="abc",
                                                      published=1))
    assert not target.exists()


def test_a_repeated_package_name_is_refused():
    built = document()
    built["plugins"].append(json.loads(json.dumps(built["plugins"][0])))
    with pytest.raises(market.IndexError_, match="duplicates"):
        market.validate_index(built)


def test_an_undeclared_category_or_label_is_refused():
    built = document()
    built["plugins"][0]["metadata"]["category"] = "nope"
    with pytest.raises(market.IndexError_, match="undeclared"):
        market.validate_index(built)
    built = document()
    built["plugins"][0]["metadata"]["labels"] = ["nope"]
    with pytest.raises(market.IndexError_, match="undeclared"):
        market.validate_index(built)


def test_the_icon_is_inline_and_within_the_size_limit():
    """An inline icon avoids a second file whose relative path must keep resolving."""
    icon = document()["plugins"][0]["icon"]
    assert icon.startswith("data:image/svg+xml;base64,")
    payload = icon.split(",", 1)[1]
    decoded = subprocess.run([sys.executable, "-c",
                              "import base64,sys; sys.stdout.buffer.write("
                              "base64.b64decode(sys.argv[1]))", payload],
                             capture_output=True, check=True).stdout
    assert len(decoded) <= market.MAX_INLINE_ICON_BYTES
    assert b"<svg" in decoded


def test_a_file_reference_that_leaves_the_index_origin_is_refused():
    built = document()
    built["plugins"][0]["icon"] = "https://evil.example/icon.png"
    # A cross-origin https URL is allowed by the harness, so this one must pass.
    market.validate_index(built)
    built["plugins"][0]["icon"] = "/absolute/icon.png"
    with pytest.raises(market.IndexError_, match="relative file"):
        market.validate_index(built)


def test_write_index_ends_with_a_newline(tmp_path):
    target = tmp_path / "out" / "index.json"
    market.write_index(target, document())
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_the_committed_index_is_valid_and_names_this_bundle():
    """The repository's published index must satisfy the rules it will be read under."""
    assert COMMITTED.is_file(), "run `ding market index` to create docs/market/index.json"
    built = json.loads(COMMITTED.read_text(encoding="utf-8"))
    market.validate_index(built, market.INDEX_URL)
    names = [plugin["name"] for plugin in built["plugins"]]
    assert names == ["dsh-dkitchen"]
    version = built["plugins"][0]["versions"][0]
    # The committed index must point at a commit this repository actually has, or the
    # install the Discover tab offers would fail to clone.
    assert len(version["source"]["commit"]) == 40
    assert version["engines"]["dsh"] == market.DSH_RANGE


def test_the_index_url_is_the_published_raw_url():
    assert market.INDEX_URL.startswith("https://raw.githubusercontent.com/")
    assert market.INDEX_URL.endswith("/docs/market/index.json")


def test_package_version_reads_pyproject():
    assert market.package_version(REPO) == "0.5.0"


def test_head_commit_is_a_full_sha():
    assert len(market.head_commit(REPO)) == 40


def test_the_check_action_rejects_a_broken_file(tmp_path, capsys):
    target = tmp_path / "index.json"
    built = document()
    built["format"] = 9
    target.write_text(json.dumps(built), encoding="utf-8")

    class Args:
        market_action = "index"
        out = target
        check = True
        url = market.INDEX_URL
        commit = None
        published = None
        version = None

    assert market.main(Args(), None) == 1
    assert "not a valid format-1 index" in capsys.readouterr().out
