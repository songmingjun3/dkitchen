"""The DeepSeek Harness plugin market index: generate it, and check it before publishing.

The harness's Discover tab reads a JSON document the harness validates with
`@deepseek-ai/dsh-plugin-market-index`. That package is format 1, and its rules are strict:
`format` must be 1, the title must carry an `en` key, every plugin needs a name and at
least one version, and every version needs an exact SemVer, an integer publish timestamp,
and a source. An index that violates any rule is refused as a whole, so an editor typo
does not degrade the listing — it removes it.

This module reproduces those rules in Python rather than generating and hoping. The
validator here is deliberately a second implementation: it lets the generator fail in CI,
where a broken index is cheap to fix, instead of after publishing, where the only symptom
is a Discover tab that reports a failed read.

The version's `source` is `git`, not `npm`. `dsh-dkitchen` is `private` and not published to
a registry, and the bundle is pure YAML with no build step, so installing it from a pinned
commit of this repository works: the harness turns `{url, commit, path}` into
`git+<url>.git#<commit>&path:/<dir>`, which pnpm can clone and install directly.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

#: The index URL a user adds to Discover. Relative icon paths resolve against it, and it is
#: the document the repository publishes, so the value is part of the deliverable.
#:
#: This names the repository and branch the commits are pushed to. A Discover entry installs
#: a pinned `git+https://…#<commit>` clone, so an index whose URL differs from the push
#: target offers an install nobody can fetch — the two must be changed together. It points at
#: the feature branch while the bundle is only there; fold it back to `main` with the merge.
INDEX_URL = ("https://raw.githubusercontent.com/songmingjun3/dkitchen/feat/dsh-harness"
             "/docs/market/index.json")

#: Where the bundle sits inside this repository, which is what a git source installs.
BUNDLE_PATH = "src/dkitchen/harness/dsh-dkitchen"

REPOSITORY_URL = "https://github.com/songmingjun3/dkitchen"

#: The dsh range the bundle declares, restated so the index cannot drift from the manifest.
DSH_RANGE = ">=0.2.0-rc.1 <0.3.0"

_LANGUAGE_ID = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
_PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_NUMERIC = r"(?:0|[1-9]\d*)"
_PRERELEASE = rf"(?:{_NUMERIC}|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
_SEMVER = re.compile(
    rf"^{_NUMERIC}\.{_NUMERIC}\.{_NUMERIC}"
    rf"(?:-{_PRERELEASE}(?:\.{_PRERELEASE})*)?"
    rf"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$")
_DATA_ICON = re.compile(
    r"^data:(?:image/svg\+xml|image/png|image/jpeg|image/webp);base64,([A-Za-z0-9+/]*={0,2})$")

#: Largest decoded payload the harness accepts for an inline `data:` icon.
MAX_INLINE_ICON_BYTES = 32 * 1024

#: The harness rejects any publish timestamp at or above this.
MAX_PUBLISH_TIMESTAMP = 10 ** 11


class IndexError_(ValueError):
    """A document that violates format 1, carrying the JSON path of the offending value."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path} {reason}")
        self.path = path
        self.reason = reason


def _fail(path: str, reason: str) -> None:
    raise IndexError_(path, reason)


def _object(value, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _string(value, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a string with a non-whitespace character")
    return value


def _localized(value, path: str) -> dict:
    """A localized string: either a plain string, or a language map that must contain `en`."""
    if isinstance(value, str):
        return {"en": _string(value, path)}
    mapping = _object(value, path)
    text: dict[str, str] = {}
    for language, member in mapping.items():
        if not isinstance(language, str) or not _LANGUAGE_ID.match(language):
            _fail(f"{path}.{language}", "must be keyed by a lowercase language id")
        text[language] = _string(member, f"{path}.{language}")
    if "en" not in text:
        _fail(path, "must contain en")
    return text


def _declared_id(value, path: str, declared: dict) -> str:
    identifier = _string(value, path)
    if identifier not in declared:
        _fail(path, f"references undeclared id {identifier!r}")
    return identifier


def _url(value, path: str, schemes: tuple[str, ...]) -> str:
    text = _string(value, path)
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", text)
    if not match:
        _fail(path, "must be an absolute URL")
    if f"{match.group(1).lower()}:" not in schemes:
        _fail(path, f"must use {', '.join(schemes)}")
    parts = text.split("/")
    if len(parts) >= 3 and "@" in parts[2]:
        _fail(path, "must not contain credentials")
    return text


def _repository(value, path: str) -> dict:
    mapping = _object(value, path)
    url = _url(mapping.get("url"), f"{path}.url", ("https:", "http:"))
    result: dict = {"url": url}
    directory = mapping.get("directory")
    if directory is not None:
        result["directory"] = _package_path(directory, f"{path}.directory")
    return result


def _package_path(value, path: str) -> str:
    text = _string(value, path)
    for segment in PurePosixPath(text).parts:
        if segment in (".", "..") or not re.match(r"^[A-Za-z0-9._@+-]+$", segment):
            _fail(path, "must be a relative directory without . or .. segments")
    return text


def _source(value, path: str) -> dict:
    mapping = _object(value, path)
    kind = mapping.get("type")
    if kind == "npm":
        result: dict = {"type": "npm"}
        registry = mapping.get("registry")
        if registry is not None:
            result["registry"] = _url(registry, f"{path}.registry", ("https:", "http:"))
        return result
    if kind == "git":
        commit = _string(mapping.get("commit"), f"{path}.commit")
        if not _COMMIT.match(commit):
            _fail(f"{path}.commit", "must be a lowercase 40-hex-digit commit SHA")
        url = _url(mapping.get("url"), f"{path}.url", ("https:",))
        result = {"type": "git", "url": url, "commit": commit}
        directory = mapping.get("path")
        if directory is not None:
            result["path"] = _package_path(directory, f"{path}.path")
        return result
    _fail(f"{path}.type", 'must be "npm" or "git"')
    raise AssertionError("unreachable")


def _icon(value, path: str, index_url: str) -> str:
    text = _string(value, path)
    if text.startswith("data:"):
        match = _DATA_ICON.match(text)
        if not match or not match.group(1):
            _fail(path, "must be a base64 SVG, PNG, JPEG, or WebP data URL")
        payload = match.group(1)
        padding = 2 if payload.endswith("==") else 1 if payload.endswith("=") else 0
        if len(payload) / 4 * 3 - padding > MAX_INLINE_ICON_BYTES:
            _fail(path, f"must not exceed {MAX_INLINE_ICON_BYTES // 1024} KiB of decoded bytes")
        return text
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", text):
        if not text.lower().startswith("https:"):
            _fail(path, "must resolve to https or the index origin")
        return text
    # A relative reference: the harness resolves it against the index URL and accepts it
    # only when the result stays on that origin, so a plain file name beside the index is
    # the shape that works.
    if text.startswith("//") or text.startswith("/"):
        _fail(path, "must be a relative file reference beside the index")
    return text


def _versions(value, path: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        _fail(path, "must be a non-empty array")
    seen: set[str] = set()
    versions: list[dict] = []
    for position, entry in enumerate(value):
        at = f"{path}[{position}]"
        mapping = _object(entry, at)
        version = _string(mapping.get("version"), f"{at}.version")
        if not _SEMVER.match(version):
            _fail(f"{at}.version", "must be an exact SemVer version")
        if version in seen:
            _fail(f"{at}.version", f"duplicates {version}")
        seen.add(version)
        stamp = mapping.get("publishTimestamp")
        if (not isinstance(stamp, int) or isinstance(stamp, bool)
                or stamp < 0 or stamp >= MAX_PUBLISH_TIMESTAMP):
            _fail(f"{at}.publishTimestamp",
                  "must be an integer number of Unix seconds in [0, 10^11)")
        parsed: dict = {"version": version, "publishTimestamp": stamp,
                        "source": _source(mapping.get("source"), f"{at}.source")}
        engines = mapping.get("engines")
        if engines is not None:
            engine_map = _object(engines, f"{at}.engines")
            dsh = engine_map.get("dsh")
            if dsh is not None:
                range_text = _string(dsh, f"{at}.engines.dsh")
                if range_text.startswith("workspace:"):
                    _fail(f"{at}.engines.dsh", "must not be a workspace: range")
                parsed["engines"] = {"dsh": range_text}
            else:
                parsed["engines"] = {}
        else:
            parsed["engines"] = {}
        versions.append(parsed)
    return versions


def _plugin_metadata(value, path: str, categories: dict, labels: dict) -> dict:
    mapping = _object(value, path)
    result: dict = {"keywords": [], "labels": [], "sortKeys": {}}
    category = mapping.get("category")
    if category is not None:
        result["category"] = _declared_id(category, f"{path}.category", categories)
    keywords = mapping.get("keywords")
    if keywords is not None:
        if not isinstance(keywords, list):
            _fail(f"{path}.keywords", "must be an array")
        result["keywords"] = [_string(word, f"{path}.keywords[{index}]")
                              for index, word in enumerate(keywords)]
    chosen = mapping.get("labels")
    if chosen is not None:
        if not isinstance(chosen, list):
            _fail(f"{path}.labels", "must be an array")
        ids = [_declared_id(label, f"{path}.labels[{index}]", labels)
               for index, label in enumerate(chosen)]
        if len(set(ids)) != len(ids):
            _fail(f"{path}.labels", "must not repeat a label")
        result["labels"] = ids
    return result


def _plugin(value, path: str, categories: dict, labels: dict, index_url: str) -> dict:
    mapping = _object(value, path)
    name = _string(mapping.get("name"), f"{path}.name")
    if len(name) > 214 or not _PACKAGE_NAME.match(name):
        _fail(f"{path}.name", "must be a valid npm package name")
    result: dict = {"name": name}
    for field in ("title", "description"):
        member = mapping.get(field)
        if member is not None:
            result[field] = _localized(member, f"{path}.{field}")
    icon = mapping.get("icon")
    if icon is not None:
        result["icon"] = _icon(icon, f"{path}.icon", index_url)
    author = mapping.get("author")
    if author is not None:
        result["author"] = _string(author, f"{path}.author")
    homepage = mapping.get("homepage")
    if homepage is not None:
        result["homepage"] = _url(homepage, f"{path}.homepage", ("https:", "http:"))
    repo = mapping.get("repository")
    if repo is not None:
        result["repository"] = _repository(repo, f"{path}.repository")
    result["metadata"] = _plugin_metadata(mapping.get("metadata") or {}, f"{path}.metadata",
                                          categories, labels)
    result["versions"] = _versions(mapping.get("versions"), f"{path}.versions")
    return result


def _declarations(value, path: str, *, sort_keys: bool = False) -> dict:
    if value is None:
        return {}
    mapping = _object(value, path)
    result: dict = {}
    for identifier, member in mapping.items():
        if identifier == "":
            _fail(path, "must not declare an empty id")
        entry = _object(member, f"{path}.{identifier}")
        record = {"title": _localized(entry.get("title"), f"{path}.{identifier}.title")}
        if sort_keys:
            order = entry.get("order")
            if order not in ("asc", "desc"):
                _fail(f"{path}.{identifier}.order", 'must be "asc" or "desc"')
            record["order"] = order
        result[identifier] = record
    return result


def validate_index(document, index_url: str = INDEX_URL) -> None:
    """Raise `IndexError_` for the first rule the document violates."""
    if not re.match(r"^https?://", index_url):
        _fail("$", f"index URL {index_url!r} must be an absolute URL")
    mapping = _object(document, "$")
    if mapping.get("format") != 1:
        _fail("$.format", "must be 1")
    metadata = _object(mapping.get("metadata") or {}, "$.metadata")
    categories = _declarations(metadata.get("categories"), "$.metadata.categories")
    labels = _declarations(metadata.get("labels"), "$.metadata.labels")
    _declarations(metadata.get("sortKeys"), "$.metadata.sortKeys", sort_keys=True)
    plugins = mapping.get("plugins")
    if not isinstance(plugins, list):
        _fail("$.plugins", "must be an array")
    names: set[str] = set()
    for position, entry in enumerate(plugins):
        path = f"$.plugins[{position}]"
        parsed = _plugin(entry, path, categories, labels, index_url)
        if parsed["name"] in names:
            _fail(f"{path}.name", f"duplicates {parsed['name']}")
        names.add(parsed["name"])
    _localized(mapping.get("title"), "$.title")
    description = mapping.get("description")
    if description is not None:
        _localized(description, "$.description")


def bundle_icon() -> str:
    """The bundle's icon as an inline `data:` URL.

    An inline icon avoids a second file whose relative path must keep resolving against the
    index's published URL, and the harness accepts a decoded payload up to 32 KiB, which
    this SVG is far under.
    """
    svg = (Path(__file__).resolve().parent / "harness" / "dsh-dkitchen"
           / "icon.svg").read_bytes()
    if len(svg) > MAX_INLINE_ICON_BYTES:
        raise ValueError(f"the bundle icon is {len(svg)} bytes, over the "
                         f"{MAX_INLINE_ICON_BYTES}-byte inline limit")
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def build_index(*, version: str, commit: str, published: int,
                index_url: str = INDEX_URL) -> dict:
    """The format-1 document listing this repository's bundle at one version."""
    if not _COMMIT.match(commit):
        raise IndexError_("$.plugins[0].versions[0].source.commit",
                          "must be a lowercase 40-hex-digit commit SHA")
    document = {
        "format": 1,
        "title": {"en": "dkitchen", "zh": "dkitchen 厨房"},
        "description": {
            "en": "A kitchen of DeepSeek Harness sessions: stations, tickets, long jobs, "
                  "and the bell that wakes them.",
            "zh": "用 DeepSeek Harness 会话开一间厨房：安排岗位、分发票、跑长任务，再读回结果。",
        },
        "metadata": {
            "categories": {
                "workflow": {"title": {"en": "Workflow", "zh": "工作流"}},
            },
            "labels": {
                "official": {"title": {"en": "Official", "zh": "官方"}},
            },
        },
        "plugins": [
            {
                "name": "dsh-dkitchen",
                "title": {"en": "dkitchen", "zh": "dkitchen 厨房"},
                "description": {
                    "en": "Call dkitchen's tools from any session: create stations, hand out "
                          "tickets, launch long jobs, and read what came back.",
                    "zh": "在任何会话里调用 dkitchen 的工具：安排岗位、分发票、跑长任务，再读回结果。",
                },
                "icon": bundle_icon(),
                "author": "917Dhj",
                "homepage": REPOSITORY_URL,
                "repository": {"url": REPOSITORY_URL, "directory": BUNDLE_PATH},
                "metadata": {
                    "category": "workflow",
                    "keywords": ["dsh-plugin", "deepseek-harness", "dkitchen", "agent",
                                 "orchestration", "multi-agent"],
                    "labels": ["official"],
                },
                "versions": [
                    {
                        "version": version,
                        "publishTimestamp": published,
                        "source": {"type": "git", "url": REPOSITORY_URL,
                                   "commit": commit, "path": BUNDLE_PATH},
                        "engines": {"dsh": DSH_RANGE},
                    },
                ],
            },
        ],
    }
    validate_index(document, index_url)
    return document


def write_index(path: Path, document: dict) -> None:
    """Write the document with a trailing newline, so a diff of it stays readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# -- git facts -------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip()
                         or f"git {' '.join(args)} exited {result.returncode}")
    return result.stdout.strip()


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def commit_time(repo: Path, commit: str) -> int:
    return int(_git(repo, "show", "-s", "--format=%ct", commit))


def package_version(repo: Path) -> str:
    """The version in `pyproject.toml`, read without building anything."""
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise ValueError("pyproject.toml has no version line")
    return match.group(1)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(args, config_) -> int:
    action = getattr(args, "market_action", None) or "index"
    if action != "index":
        raise ValueError(f"unknown market action: {action}")
    repo = _repo_root()
    out = Path(args.out)
    if not out.is_absolute():
        out = repo / out
    index_url = args.url or INDEX_URL
    if args.check:
        if not out.is_file():
            print(f"ding: {out} does not exist; run `ding market index` first")
            return 1
        document = json.loads(out.read_text(encoding="utf-8"))
        try:
            validate_index(document, index_url)
        except IndexError_ as error:
            print(f"ding: {out} is not a valid format-1 index: {error}")
            return 1
        listed = ", ".join(plugin["name"] for plugin in document["plugins"])
        print(f"ding: {out} is a valid format-1 index ({listed})")
        return 0
    commit = args.commit or head_commit(repo)
    published = args.published if args.published is not None else commit_time(repo, commit)
    version = args.version or package_version(repo)
    document = build_index(version=version, commit=commit, published=published,
                           index_url=index_url)
    write_index(out, document)
    print(f"ding: wrote {out} (dsh-dkitchen {version} at {commit[:12]}, "
          f"published {published})")
    print(f"ding: add this URL in the harness's Plugins > Discover tab:\n  {index_url}")
    return 0
