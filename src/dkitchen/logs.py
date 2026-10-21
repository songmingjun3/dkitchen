"""Read DeepSeek Harness session logs, without asking the harness anything.

`ding read`, `ding board`, and `ding info` need a session's latest reply and its working
directory. The harness persists every durable event of a session as one JSONL log, either
raw or as checksummed Zstandard frames, at:

    $DSH_HOME/sessions/<project>/<session-id>/session.v<N>.jsonl[.zstd]

Reading that file is read-only and works even while another DeepSeek Harness process owns
the session, which is exactly the case dkitchen must survive: the GUI may have the same
conversation open. The shapes below are the durable event vocabulary; a log written by a
newer format than this module knows is reported as unreadable rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
import time

from dkitchen import paths

#: Highest session log format this module understands.
SUPPORTED_VERSION = 4

_LOG_NAME = re.compile(r"^session(?:\.v(\d+))?\.jsonl(?:\.zstd)?$")


class Unreadable(ValueError):
    """The log cannot be read: a newer format, a corrupt frame, or a torn tail."""


@dataclass(frozen=True)
class Session:
    """What `ding` needs to know about one persisted harness session."""

    id: str
    path: Path
    cwd: str | None
    created_at: float | None
    reply: str | None
    turns: int
    last_event_at: float | None


def _newest_log(directory: Path) -> Path | None:
    candidates = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = _LOG_NAME.match(entry.name)
        if match and entry.is_file():
            candidates.append((int(match[1] or 0), entry))
    if not candidates:
        return None
    return max(candidates)[1]


def session_directories() -> list[Path]:
    """Every per-session directory under the harness sessions root."""
    root = paths.sessions_root()
    try:
        projects = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []
    found = []
    for project in projects:
        try:
            found.extend(entry for entry in project.iterdir() if entry.is_dir())
        except OSError:
            continue
    return found


def locate(session_id: str) -> Path | None:
    """The newest log belonging to `session_id`, wherever the harness put it."""
    root = paths.sessions_root()
    try:
        projects = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return None
    for project in projects:
        directory = project / session_id
        if directory.is_dir():
            found = _newest_log(directory)
            if found is not None:
                return found
    return None


def _open(path: Path):
    if path.suffix == ".zstd":
        try:
            import zstandard
        except ImportError as error:  # pragma: no cover - declared dependency
            raise Unreadable("reading compressed harness logs needs the zstandard package; "
                             "reinstall dkitchen with its dependencies") from error
        return zstandard.ZstdDecompressor().stream_reader(path.open("rb"), read_across_frames=True)
    return path.open("rb")


def events(path: Path):
    """Yield each durable event of a log, skipping an incomplete trailing row."""
    try:
        with _open(path) as handle:
            # The zstd stream reader is not sized for `readline`, so read fixed blocks and
            # split them here; the plain-file case goes through the same path.
            pending = b""
            while True:
                block = handle.read(1 << 16)
                if not block:
                    break
                pending += block
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    value = _decode(raw)
                    if value is not None:
                        yield value
            for raw in pending.split(b"\n"):
                value = _decode(raw)
                if value is not None:
                    yield value
    except Unreadable:
        raise
    except OSError as error:
        raise Unreadable(f"cannot read {path}: {error}") from error


def _decode(raw: bytes) -> dict | None:
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        # A writer killed mid-append leaves a torn final row; the next flush rewrites it.
        return None
    return value if isinstance(value, dict) else None


def header(path: Path) -> dict:
    for event in events(path):
        if event.get("type") != "session":
            raise Unreadable(f"{path} does not start with a session header")
        version = event.get("version")
        if not isinstance(version, int) or version > SUPPORTED_VERSION:
            raise Unreadable(f"{path} uses session format {version}; this dkitchen reads up to "
                             f"{SUPPORTED_VERSION}. Read it in the DeepSeek Harness GUI instead.")
        if version < SUPPORTED_VERSION:
            raise Unreadable(f"{path} uses session format {version}; open it once in the DeepSeek "
                             "Harness GUI so the harness migrates it, or read it there")
        return event
    raise Unreadable(f"{path} is empty")


def _message_text(message: object) -> str | None:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = [block.get("text") for block in content
             if isinstance(block, dict) and block.get("type") == "text"
             and isinstance(block.get("text"), str)]
    if not parts:
        return None
    return "\n\n".join(parts)


def read(path: Path) -> Session:
    """Fold one log into the facts `ding` prints."""
    session_id = path.parent.name
    cwd, created, reply, turns, last_time = None, None, None, 0, None
    seen_header = False
    for event in events(path):
        kind = event.get("type")
        if kind == "session" and not seen_header:
            seen_header = True
            version = event.get("version")
            if not isinstance(version, int) or version > SUPPORTED_VERSION:
                raise Unreadable(f"{path} uses session format {version}; this dkitchen reads up to "
                                 f"{SUPPORTED_VERSION}. Read it in the DeepSeek Harness GUI instead.")
            if version < SUPPORTED_VERSION:
                raise Unreadable(f"{path} uses session format {version}; open it once in the DeepSeek "
                                 "Harness GUI so the harness migrates it, or read it there")
            cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
            created_ms = event.get("createdAt")
            created = created_ms / 1000 if isinstance(created_ms, int) else None
            continue
        time_ms = event.get("time")
        if isinstance(time_ms, int):
            last_time = time_ms / 1000
        if kind == "turn/end":
            turns += 1
        elif kind == "assistant/message":
            data = event.get("data")
            text = _message_text(data.get("message") if isinstance(data, dict) else None)
            if text:
                reply = text
    if not seen_header:
        raise Unreadable(f"{path} is empty")
    return Session(id=session_id, path=path, cwd=cwd, created_at=created, reply=reply,
                   turns=turns, last_event_at=last_time)


#: One-entry memo keyed by path, size, and mtime: `board` asks for the same log repeatedly.
_cache: dict[tuple[str, int, int], Session] = {}


def load(session_id: str) -> Session | None:
    path = locate(session_id)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    session = read(path)
    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = session
    return session


def known_sessions() -> dict[str, Session]:
    """Every session the harness has persisted, keyed by id, newest first."""
    found: dict[str, Session] = {}
    for directory in session_directories():
        path = _newest_log(directory)
        if path is None:
            continue
        try:
            found[path.parent.name] = load(path.parent.name) or read(path)
        except Unreadable:
            continue
    return dict(sorted(found.items(), key=lambda item: item[1].created_at or 0, reverse=True))


def newest_mtime() -> float:
    """The most recent write among persisted sessions, for cheap change detection."""
    latest = 0.0
    for directory in session_directories():
        path = _newest_log(directory)
        if path is None:
            continue
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest or time.time()
