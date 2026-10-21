"""dkitchen's own record of the sessions it created.

ACP has no rename and no "what did I call this session" surface, so the kitchen keeps its
own table: one JSON document under the state root, mapping each harness session id to the
station that owns it, the ticket it serves, its working directory, and the route it runs on.

`ding board`, `ding info`, and `ding rename` read this table; `ding new` and the broker write
it. A session the user created in the DeepSeek Harness GUI simply has no row here, and every
command falls back to what the session log itself says.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import time

from dkitchen import paths


@dataclass
class Record:
    """One kitchen session."""

    id: str
    name: str
    station: str
    cwd: str
    created_ns: int
    issue: int | None = None
    repo: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    closed: bool = False

    @property
    def created(self) -> float:
        return self.created_ns / 1_000_000_000


def path() -> Path:
    return paths.state("sessions.json")


def load() -> dict[str, Record]:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return {}
    records: dict[str, Record] = {}
    for session_id, value in sessions.items():
        if not isinstance(value, dict):
            continue
        try:
            records[session_id] = Record(
                id=session_id,
                name=str(value.get("name") or session_id),
                station=str(value.get("station") or "executor"),
                cwd=str(value.get("cwd") or ""),
                created_ns=int(value.get("created_ns") or 0),
                issue=value.get("issue") if isinstance(value.get("issue"), int) else None,
                repo=value.get("repo") if isinstance(value.get("repo"), str) else None,
                provider=value.get("provider") if isinstance(value.get("provider"), str) else None,
                model=value.get("model") if isinstance(value.get("model"), str) else None,
                effort=value.get("effort") if isinstance(value.get("effort"), str) else None,
                closed=bool(value.get("closed")),
            )
        except (TypeError, ValueError):
            continue
    return records


def save(records: dict[str, Record]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "sessions": {key: asdict(value) for key, value in records.items()}}
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def put(record: Record) -> Record:
    """Insert or replace one row, preserving the rest."""
    records = load()
    previous = records.get(record.id)
    if previous is not None and not record.issue and previous.issue:
        record.issue, record.repo = previous.issue, previous.repo
    records[record.id] = record
    save(records)
    return record


def get(session_id: str) -> Record | None:
    return load().get(session_id)


def rename(session_id: str, name: str) -> Record | None:
    records = load()
    record = records.get(session_id)
    if record is None:
        return None
    record.name = name
    save(records)
    return record


def mark_closed(session_id: str) -> None:
    records = load()
    record = records.get(session_id)
    if record is not None and not record.closed:
        record.closed = True
        save(records)


def find_by_issue(repo: str, issue: int) -> list[Record]:
    return [record for record in load().values()
            if record.issue == issue and (record.repo in (None, repo))]
