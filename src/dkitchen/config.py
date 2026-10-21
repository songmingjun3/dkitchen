"""Per-repo kitchen vocabulary, stations, routes, and harness settings.

Read from `.dkitchen.toml` at the repository root. Every key is optional; a repository
without the file gets the English defaults below, which match the templates in `kitchen/`:

    repo = "owner/name"                  # default: the repository gh sees here

    [kitchen]
    stations = ["orchestrator", "designer", "verifier", "executor"]
                                         # the stations this kitchen runs; at least one

    [stations]
    orchestrator = "Orchestrator"
    designer = "Designer"
    verifier = "Verifier"
    executor = "Executor"
    user = "User"
    separator = ": "                     # written after a station name to sign a comment

    [harness]
    profile = "dkitchen"                 # the DeepSeek Harness profile `ding` boots
    default_provider = "deepseek-official"
    default_model = "deepseek-v4-flash"
    approvals = "auto"                   # auto answers permission prompts; ask waits for `ding approve`
    permission = "workspace-write"       # DSH_PERMISSION_MODE for the booted profile

    [models.executor]                    # one table per station, all keys optional
    provider = "deepseek-official"
    model = "deepseek-v4-flash"
    effort = "high"                      # reasoning_effort, when the model declares one

    [status]
    fields = ["Status", "Waiting on", "Key numbers", "Next"]   # first: the state; second: who it waits on
    in_progress = "in progress"
    waiting_on_design = "waiting on design"
    blocked = "blocked"
    done = "done"
    none = "none"                        # a Waiting on value naming nobody

    [labels]
    prefix = "status:"
    awaiting_user = "status:awaiting-user"

    [log]
    issue = 1                            # the pinned orchestrator log ticket
    pending = "Waiting for the user"

    [sessions]
    owner_line = "DSH owner"             # the line that claims a ticket
    session_line = "DSH session"         # the line that signs a comment
    name = "【#{issue}】{name}"           # `ding new --issue N --name NAME`; also {repo}

The two signature lines are read leniently: a ticket written before the harness migration
says `Codex owner:` and still resolves.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields as dataclass_fields, replace
from pathlib import Path
import re
import string
import subprocess
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

FILE = ".dkitchen.toml"

#: The status block that opens an executor comment, in order: the state, then who it waits on.
DEFAULT_STATUS_FIELDS = ("Status", "Waiting on", "Key numbers", "Next")

#: The stations a kitchen may run, in the order rules and reports mention them.
STATIONS = ("orchestrator", "designer", "verifier", "executor")

#: Signature lines older than the harness migration; they still resolve a ticket.
LEGACY_SESSION_LINES = ("Codex session",)
LEGACY_OWNER_LINES = ("Codex owner",)


@dataclass(frozen=True)
class Stations:
    orchestrator: str = "Orchestrator"
    designer: str = "Designer"
    verifier: str = "Verifier"
    executor: str = "Executor"
    user: str = "User"
    separator: str = ": "

    def signature(self, name: str) -> str:
        return f"{name}{self.separator}"

    def name(self, station: str) -> str:
        return getattr(self, station)


@dataclass(frozen=True)
class Kitchen:
    """Which stations this repository runs.

    A station that is not listed is not opened, and its duties fall to the orchestrator as
    the rules describe; listing one is the only thing that makes the setup skill install it.
    """

    stations: tuple[str, ...] = STATIONS

    def includes(self, station: str) -> bool:
        return station in self.stations

    @property
    def minimal(self) -> bool:
        return not self.includes("designer") and not self.includes("verifier")


@dataclass(frozen=True)
class Route:
    """The model route one station runs on; every field may be omitted."""

    provider: str | None = None
    model: str | None = None
    effort: str | None = None

    def merged(self, fallback: "Route") -> "Route":
        return Route(provider=self.provider or fallback.provider,
                     model=self.model or fallback.model,
                     effort=self.effort or fallback.effort)

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "effort": self.effort}


@dataclass(frozen=True)
class Harness:
    profile: str = "dkitchen"
    default_provider: str = "deepseek-official"
    default_model: str = "deepseek-v4-flash"
    approvals: str = "auto"
    permission: str = "workspace-write"

    @property
    def default_route(self) -> Route:
        return Route(provider=self.default_provider, model=self.default_model)


@dataclass(frozen=True)
class Models:
    """Per-station routes, keyed by station name."""

    stations: dict[str, Route] = field(default_factory=dict)

    def route(self, station: str, harness: Harness) -> Route:
        return self.stations.get(station, Route()).merged(harness.default_route)


@dataclass(frozen=True)
class Status:
    fields: tuple[str, ...] = DEFAULT_STATUS_FIELDS
    in_progress: str = "in progress"
    waiting_on_design: str = "waiting on design"
    blocked: str = "blocked"
    done: str = "done"
    none: str = "none"


@dataclass(frozen=True)
class Labels:
    prefix: str = "status:"
    awaiting_user: str = "status:awaiting-user"


@dataclass(frozen=True)
class Log:
    issue: int | None = None
    pending: str = "Waiting for the user"


@dataclass(frozen=True)
class Sessions:
    name: str = "【#{issue}】{name}"
    owner_line: str = "DSH owner"
    session_line: str = "DSH session"

    def session_name(self, *, issue: int, name: str, repo: str) -> str:
        return self.name.format(issue=issue, name=name, repo=repo)

    def owner_patterns(self) -> tuple[str, ...]:
        return (self.owner_line, *LEGACY_OWNER_LINES)

    def session_patterns(self) -> tuple[str, ...]:
        return (self.session_line, *LEGACY_SESSION_LINES)


SESSION_FIELDS = ("issue", "name", "repo")


def session_template(value: object) -> str:
    """A session name template using only SESSION_FIELDS, checked when the config is read."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{FILE}: [sessions] name must be a nonempty string")
    allowed = ", ".join(f"{{{field}}}" for field in SESSION_FIELDS)
    try:
        for _, field_name, spec, conversion in string.Formatter().parse(value):
            if field_name is None:
                continue
            if field_name not in SESSION_FIELDS:
                raise ValueError(f"{FILE}: [sessions] name uses {{{field_name}}}; use only {allowed}")
            if conversion or "{" in spec:
                raise ValueError(f"{FILE}: [sessions] name: write {{{field_name}}} without a conversion or nested field")
        Sessions(value).session_name(issue=1, name="name", repo="owner/name")
    except ValueError as exc:
        if str(exc).startswith(FILE):
            raise
        raise ValueError(f"{FILE}: [sessions] name is not a valid template: {exc}") from exc
    return value


@dataclass(frozen=True)
class Config:
    root: Path
    repo: str | None = None
    kitchen: Kitchen = Kitchen()
    stations: Stations = Stations()
    harness: Harness = Harness()
    models: Models = Models()
    status: Status = Status()
    labels: Labels = Labels()
    log: Log = Log()
    sessions: Sessions = Sessions()

    def repository(self) -> str:
        """The configured repository, or the one gh sees from the repository root."""
        if self.repo:
            return self.repo
        try:
            result = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                                    cwd=self.root, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            raise ValueError(f"the gh CLI is not installed; install it, or set repo in {FILE}") from exc
        repo = result.stdout.strip()
        if result.returncode or not repo:
            raise ValueError(f"cannot tell the GitHub repository; set repo in {FILE}: {result.stderr.strip()}")
        return repo

    def route(self, station: str) -> Route:
        """The route one station runs on, falling back to the harness default."""
        return self.models.route(station, self.harness)

    def session_name(self, *, issue: int, name: str, repo: str) -> str:
        return self.sessions.session_name(issue=issue, name=name, repo=repo)


def section(cls, values: object, name: str):
    if not isinstance(values, dict):
        raise ValueError(f"{FILE}: [{name}] must be a table")
    known = {item.name: item for item in dataclass_fields(cls)}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"{FILE}: unknown keys in [{name}]: {', '.join(unknown)}")
    result = cls()
    for key, value in values.items():
        if key == "fields":
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError(f"{FILE}: [{name}] fields must be a nonempty list of field names")
            value = tuple(item.strip() for item in value)
        elif key == "name" and cls is Sessions:
            value = session_template(value)
        elif key == "issue":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{FILE}: [{name}] issue must be a positive issue number")
        elif not isinstance(value, str) or (not value.strip() and key != "separator"):
            raise ValueError(f"{FILE}: [{name}] {key} must be a nonempty string")
        result = replace(result, **{key: value})
    return result


def kitchen_section(values: object) -> Kitchen:
    if not isinstance(values, dict):
        raise ValueError(f"{FILE}: [kitchen] must be a table")
    unknown = sorted(set(values) - {"stations"})
    if unknown:
        raise ValueError(f"{FILE}: unknown keys in [kitchen]: {', '.join(unknown)}")
    stations = values.get("stations")
    if stations is None:
        return Kitchen()
    if (not isinstance(stations, list) or not stations
            or any(not isinstance(item, str) for item in stations)):
        raise ValueError(f"{FILE}: [kitchen] stations must be a nonempty list of station names")
    unknown = sorted(set(stations) - set(STATIONS))
    if unknown:
        raise ValueError(f"{FILE}: [kitchen] unknown stations: {', '.join(unknown)}; "
                         f"use any of {', '.join(STATIONS)}")
    if len(set(stations)) != len(stations):
        raise ValueError(f"{FILE}: [kitchen] stations names a station twice")
    return Kitchen(stations=tuple(name for name in STATIONS if name in stations))


def harness_section(values: object) -> Harness:
    result = section(Harness, values, "harness")
    if result.approvals not in {"auto", "ask"}:
        raise ValueError(f"{FILE}: [harness] approvals must be 'auto' or 'ask'")
    return result


def models_section(values: object) -> Models:
    if not isinstance(values, dict):
        raise ValueError(f"{FILE}: [models] must be a table of per-station tables")
    unknown = sorted(set(values) - set(STATIONS))
    if unknown:
        raise ValueError(f"{FILE}: [models] unknown stations: {', '.join(unknown)}; "
                         f"use any of {', '.join(STATIONS)}")
    return Models(stations={station: section(Route, table, f"models.{station}")
                            for station, table in values.items()})


def repository_root(cwd: Path | None = None) -> Path:
    """The Git work tree holding cwd, or cwd itself outside Git."""
    cwd = cwd or Path.cwd()
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else cwd


def load(cwd: Path | None = None) -> Config:
    root = repository_root(cwd)
    path = root / FILE
    if not path.exists():
        return Config(root=root)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    sections = {"stations": Stations, "status": Status, "labels": Labels, "log": Log,
                "sessions": Sessions}
    special = {"kitchen", "harness", "models"}
    unknown = sorted(set(data) - {"repo", *sections, *special})
    if unknown:
        raise ValueError(f"{FILE}: unknown keys: {', '.join(unknown)}")
    repo = data.get("repo")
    if repo is not None and (not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)):
        raise ValueError(f"{FILE}: repo must look like owner/name")
    loaded = {name: section(cls, data[name], name) for name, cls in sections.items() if name in data}
    if "kitchen" in data:
        loaded["kitchen"] = kitchen_section(data["kitchen"])
    if "harness" in data:
        loaded["harness"] = harness_section(data["harness"])
    if "models" in data:
        loaded["models"] = models_section(data["models"])
    return Config(root=root, repo=repo, **loaded)
