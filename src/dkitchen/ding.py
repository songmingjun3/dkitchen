"""Message DeepSeek Harness agent sessions, and wake them when their long jobs end.

Every kitchen session is a DeepSeek Harness session: created in the harness, persisted by
it, and visible in its GUI. `ding` creates sessions, delivers messages, reads replies, and
reports who is working on what.

The moving parts:

- a **broker** (`dkitchen.broker`) that owns one harness ACP connection and one message
  queue per session, started on demand;
- the harness's own **session logs**, read directly, so `ding read` works even while
  another harness process has the session open;
- a **launch registry**, so a long job's exit wakes the session that started it on both
  tmux and Windows.

Targets accept a session id, an exact session name, `'#42'`, `OWNER/REPO#42`, or an issue
or pull-request URL.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib import metadata

from dkitchen import acp, broker, config, launcher, locks, logs, paths, registry

ISSUE_REPOSITORY = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
STATUS_FIELDS = "Status,Waiting on,Key numbers,Next"
SHELL_PROGRAMS = {"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh"}
WINDOWS = paths.WINDOWS


class DingError(ValueError):
    """A refusal `ding` reports as one line on stderr."""


# -- session vocabulary -------------------------------------------------------------------

def status_fields(value: str) -> tuple[str, ...]:
    fields = tuple(field.strip() for field in value.split(","))
    if not all(fields):
        raise argparse.ArgumentTypeError("must be comma-separated nonempty field names")
    return fields


def settings(cwd: Path | None = None) -> config.Config:
    return config.load(cwd)


def owner_line(name: str, session_id: str) -> str:
    return f"{name}: {session_id}"


def ownership_note(config_: config.Config, repo: str, number: int, session_id: str) -> str:
    """The first paragraph asking a new session to claim its ticket."""
    line = owner_line(config_.sessions.owner_line, session_id)
    return (f"You own ticket {repo}#{number}. Before anything else, post a comment on it "
            f"with the line `{line}`.\n\n")


def session_references(body: str | None, label: str) -> list[str]:
    """Every `<label>: <session id>` line in a body."""
    if body is None:
        return []
    if not isinstance(body, str):
        raise ValueError("Malformed gh issue body.")
    return [value.strip().strip("`") for value in re.findall(
        rf"(?m)^[ \t]*{re.escape(label)}:[ \t]*([^\r\n]*)$", body.replace("\r\n", "\n"))]


def all_references(body: str | None, config_: config.Config, *, owner: bool) -> list[str]:
    """Owner or signature lines under any accepted spelling, old vocabulary included."""
    patterns = config_.sessions.owner_patterns() if owner else config_.sessions.session_patterns()
    found: list[str] = []
    for pattern in patterns:
        found.extend(session_references(body, pattern))
    return found


# -- GitHub ------------------------------------------------------------------------------

def gh_available() -> bool:
    from shutil import which
    return which("gh") is not None


def gh_json(arguments: list[str], *, cwd: Path | None = None):
    try:
        result = subprocess.run(["gh", *arguments], capture_output=True, text=True, timeout=60,
                                cwd=str(cwd) if cwd else None)
    except FileNotFoundError as error:
        raise ValueError("the gh CLI is not installed; install it to use tickets") from error
    if result.returncode:
        raise ValueError(f"gh failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def gh_records(resource: str) -> list[dict]:
    pages = gh_json(["api", "--paginate", "--slurp", resource])
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("Malformed gh pagination result.")
    records = [record for page in pages for record in page]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Malformed gh record.")
    return records


def github_repository() -> str:
    result = gh_json(["repo", "view", "--json", "nameWithOwner"])
    repo = result.get("nameWithOwner") if isinstance(result, dict) else None
    if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Malformed gh repository name.")
    return repo


def issue_session(number: int, repo: str | None = None, config_: config.Config | None = None) -> str:
    """The session a ticket belongs to: dkitchen's own record first, then the ticket's owner line."""
    config_ = config_ or settings()
    repo = repo if repo is not None else github_repository()
    known = registry.find_by_issue(repo, number)
    if len(known) == 1:
        return known[0].id
    issue = gh_json(["api", f"repos/{repo}/issues/{number}"])
    if not isinstance(issue, dict):
        raise ValueError("Malformed gh issue.")
    comments = gh_records(f"repos/{repo}/issues/{number}/comments?per_page=100")
    owners: list[str] = []
    candidates: list[str] = []
    for record in [issue, *comments]:
        body = record.get("body")
        found = all_references(body, config_, owner=True)
        if found:
            owners.extend(found)
        candidates.extend(all_references(body, config_, owner=False))
    if owners:
        return owners[-1]
    unique = sorted(set(candidates))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        raise ValueError(f"No session line in issue #{number}. Candidates: none.")
    listing = "\n".join(f"  {value}" for value in unique)
    raise ValueError(f"Issue #{number} references {len(unique)} different sessions without an "
                     f"owner line. Candidates:\n{listing}\nSpecify a session id or exact name instead.")


def normalize_target(target: str | None, issue: int | None, config_: config.Config | None = None) -> str | None:
    if issue is not None:
        target = f"#{issue}"
    if target and target.startswith("#"):
        if not re.fullmatch(r"#[1-9][0-9]*", target):
            raise ValueError("Issue target must be # followed by a positive issue number.")
        try:
            if config_ is not None:
                target = f"{config_.repository()}{target}"
            else:
                target = github_repository() + target
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise ValueError(
                f"'{target}' needs a GitHub repository in the current directory; "
                f"elsewhere use OWNER/REPO{target} or the issue URL. ({' '.join(str(error).split())})"
            ) from error
    if target and ("://" in target or target.startswith(("http:", "https:"))):
        match = re.fullmatch(
            rf"https://github\.com/({ISSUE_REPOSITORY})/(?:issues|pull)/([1-9][0-9]*)/?(?:\?[^#\s]*)?(?:#[^\r\n]*)?",
            target)
        if not match:
            raise ValueError("Issue URL must be https://github.com/OWNER/REPO/issues/NUMBER or /pull/NUMBER.")
        target = f"{match[1]}#{match[2]}"
    if target and re.match(r"[^\s/]*/", target) and "#" in target:
        match = re.fullmatch(rf"({ISSUE_REPOSITORY})#([1-9][0-9]*)", target)
        if not match or any(segment in {".", ".."} for segment in match[1].split("/")):
            raise ValueError("Issue target must be OWNER/REPO# followed by a positive issue number.")
    return target


def resolve_target(target: str | None, issue: int | None, config_: config.Config | None = None) -> str:
    """The session id a target names."""
    config_ = config_ or settings()
    target = normalize_target(target, issue, config_)
    if not target:
        raise ValueError("Name a session, a ticket, or an issue URL")
    if re.fullmatch(r"(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", target):
        return acp.session_id_from(target)
    match = re.fullmatch(rf"({ISSUE_REPOSITORY})#([1-9][0-9]*)", target)
    if match:
        return issue_session(int(match[2]), match[1], config_)
    records = registry.load()
    matches = [record.id for record in records.values() if display_cell(record.name) == target]
    if len(matches) == 1:
        return matches[0]
    known = [session for session in logs.known_sessions().values()]
    candidates = matches or [record.id for record in records.values()] or [session.id for session in known]
    listing = "\n".join(f"  {value}" for value in candidates) or "  none"
    raise ValueError(f"Found {len(matches)} sessions named {target!r}. Candidates:\n{listing}")


def display_cell(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


def reply_head(text: str) -> str:
    return re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n"), maxsplit=1)[0].strip()


# -- broker helpers ----------------------------------------------------------------------

def broker_profiles(config_: config.Config) -> dict:
    """What the broker needs from the repository's configuration."""
    return {
        "default": config_.harness.profile,
        "approvals": config_.harness.approvals,
        "permission": config_.harness.permission,
    }


def ensure_broker(config_: config.Config, *, start: bool = True) -> bool:
    if not start:
        return broker.running()
    return broker.ensure(profiles=broker_profiles(config_))


def call_broker(config_: config.Config, action: str, *, start: bool = True, **payload) -> dict:
    if start:
        ensure_broker(config_)
    try:
        reply = broker.request(action, **payload)
    except broker.BrokerDown as error:
        raise DingError(f"{error}; run `ding up` to start it") from error
    if not reply.get("ok"):
        raise DingError(str(reply.get("error") or "the broker refused the request"))
    return reply


def session_record(session_id: str) -> registry.Record | None:
    return registry.get(session_id)


def session_cwd(session_id: str) -> str:
    """A session's working directory: dkitchen's record, else its persisted log."""
    record = session_record(session_id)
    if record is not None and record.cwd:
        return record.cwd
    try:
        persisted = logs.load(session_id)
    except logs.Unreadable:
        persisted = None
    if persisted is not None and persisted.cwd:
        return persisted.cwd
    return str(Path.cwd())


# -- commands ----------------------------------------------------------------------------

def new_task(args, config_: config.Config) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise DingError("--cwd must name an existing directory.")
    message = message_text(args.message, args.message_file, None)
    if not message.strip():
        raise DingError("The first message (--message or --message-file) must not be empty.")
    station = args.station or "executor"
    if not config_.kitchen.includes(station):
        raise DingError(f"{station} is not one of this kitchen's stations "
                        f"({', '.join(config_.kitchen.stations)}); add it in {config.FILE} first")
    name = args.name
    repo = None
    if args.issue is not None:
        repo = config_.repository()
        name = config_.session_name(issue=args.issue, name=args.name, repo=repo)
    route = config_.route(station)
    if args.provider or args.model:
        route = config.Route(provider=args.provider or route.provider,
                             model=args.model or route.model, effort=route.effort)
    ensure_broker(config_)
    reply = call_broker(config_, "create", cwd=str(cwd), name=name, station=station,
                        issue=args.issue, repo=repo, route=route.as_dict(),
                        message=None)
    session_id = reply["session"]
    if args.issue is not None:
        note = ownership_note(config_, repo, args.issue, session_id)
        message = note + message
        call_broker(config_, "send", session=session_id, message=message, cwd=str(cwd),
                    route=route.as_dict())
    else:
        call_broker(config_, "send", session=session_id, message=message, cwd=str(cwd),
                    route=route.as_dict())
    print(session_id)
    return 0


def send_task(args, config_: config.Config) -> int:
    state = paths.state("sent.jsonl")
    state.parent.mkdir(parents=True, exist_ok=True)
    target = args.thread_id if args.issue is None else f"#{args.issue}"
    event = {"event": "send_intent", "sender": args.sender, "target": target, "thread_id": None,
             "message": args.message, "outcome": "failed", "error": None}
    try:
        event["target"] = normalize_target(args.thread_id, args.issue, config_)
        event["thread_id"] = resolve_target(event["target"], None, config_)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        event["error"] = str(error) or type(error).__name__
    if event["error"] is None:
        event["outcome"] = "pending"
    event["timestamp_ns"] = time.time_ns()
    _append(state, event)
    if event["error"]:
        print(f"ding: {event['error']}", file=sys.stderr)
        return 1
    cwd = session_cwd(event["thread_id"])
    record = session_record(event["thread_id"])
    route = record and {"provider": record.provider, "model": record.model, "effort": record.effort}
    try:
        reply = call_broker(config_, "send", session=event["thread_id"], message=args.message,
                            cwd=cwd, route=route or {})
        event["outcome"] = str(reply.get("outcome") or "started")
    except DingError as error:
        event["error"] = str(error)
        event["outcome"] = "failed"
    event.update(event="send_result", timestamp_ns=time.time_ns())
    _append(state, event)
    if event["error"]:
        print(f"ding: {event['error']}; record: {state}", file=sys.stderr)
        return 1
    print(f"ding: {event['outcome']}; record: {state}", file=sys.stderr)
    return 0


def _append(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_task(args, config_: config.Config) -> int:
    session_id = resolve_target(args.thread_id, args.issue, config_)
    try:
        session = logs.load(session_id)
    except logs.Unreadable as error:
        raise DingError(str(error)) from error
    if session is None:
        raise DingError(f"session {session_id} has no persisted log yet; it may not have started")
    if not session.reply:
        raise DingError(f"session {session_id} has no final reply yet")
    print(reply_head(session.reply) if args.head else session.reply)
    return 0


def status_task(args, config_: config.Config) -> int:
    session_id = resolve_target(args.thread_id, args.issue, config_)
    if not broker.running():
        print(json.dumps({"type": "unavailable"}))
        return 0
    try:
        reply = broker.request("status", session=session_id)
    except broker.BrokerDown:
        print(json.dumps({"type": "unavailable"}))
        return 0
    print(json.dumps({"type": reply.get("state", "unknown"),
                      "inFlight": bool(reply.get("in_flight")),
                      "queued": reply.get("queued", 0),
                      "stopReason": reply.get("stop_reason"),
                      "error": reply.get("last_error")}, ensure_ascii=False))
    return 0


def info_task(args, config_: config.Config) -> int:
    session_id = resolve_target(args.thread_id, args.issue, config_)
    record = session_record(session_id)
    persisted = None
    try:
        persisted = logs.load(session_id)
    except logs.Unreadable:
        persisted = None
    cwd = (record.cwd if record else None) or (persisted.cwd if persisted else None)
    state = "notLoaded"
    if broker.running():
        try:
            state = str(broker.request("status", session=session_id).get("state") or state)
        except broker.BrokerDown:
            state = "unavailable"
    print(json.dumps({"id": session_id,
                      "name": record.name if record else None,
                      "station": record.station if record else None,
                      "cwd": cwd,
                      "status": {"type": state},
                      "route": {"provider": record.provider, "model": record.model,
                                "effort": record.effort} if record else None},
                     ensure_ascii=False))
    return 0


def rename_task(args, config_: config.Config) -> int:
    session_id = resolve_target(args.thread_id, args.issue, config_)
    record = registry.get(session_id)
    if record is None:
        raise DingError(f"session {session_id} was not created by ding, so ding cannot rename it; "
                        "rename it in the DeepSeek Harness GUI")
    registry.rename(session_id, args.name)
    print(session_id)
    return 0


def run_state_from_record(record: Path, run_id: str, pid: int | None, *,
                          kill_check: bool = True) -> str:
    """`running`, `exited(CODE)`, `notify_failed`, `lost`, or `unknown` for one wrapped job."""
    alive = True
    if kill_check:
        alive = locks.is_locked(paths.state("run-locks", f"{run_id}.lock"))
    try:
        raw = Path(record).read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError:
        return "unknown"
    lines = raw.splitlines()
    task_exit, notification = None, None
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            if index == len(lines) - 1 and not raw.endswith(b"\n"):
                break
            return "unknown"
        if not isinstance(event, dict) or event.get("run_id") != run_id:
            continue
        if event.get("event") == "task_exit" and type(event.get("exit_code")) is int:
            task_exit = event
        elif event.get("event") == "notification" and isinstance(event.get("outcome"), str):
            notification = event
    if task_exit is None:
        if notification is not None:
            return "unknown"
        return "running" if alive else "lost"
    if notification is not None:
        if notification.get("outcome") in {"queue_failed", "failed", "unknown", "queued"}:
            return "notify_failed"
    elif not alive:
        return "notify_failed"
    return f"exited({task_exit['exit_code']})"


def run_task(args, command: list[str], config_: config.Config) -> int:
    record = Path(args.record)
    run_id = os.environ.get("DKITCHEN_RUN_ID") or str(__import__("uuid").uuid4())
    lock = paths.state("run-locks", f"{run_id}.lock")
    with locks.exclusive(lock, blocking=True):
        _append(paths.state("runs.jsonl"), {
            "event": "run_start", "run_id": run_id, "session_id": args.thread_id,
            "record": str(record.resolve()), "pid": os.getpid(), "started_ns": time.time_ns(),
            "cwd": str(Path.cwd()),
        })
        child_env = os.environ.copy()
        child_env["DKITCHEN_RUN_ID"] = run_id
        code, error = task_exit_code(command, env=child_env, log=Path(args.log) if args.log else None)
        message_id = str(__import__("uuid").uuid4())
        _append(record, {
            "event": "task_exit", "run_id": run_id, "thread_id": args.thread_id,
            "client_message_id": message_id, "message": args.message, "exit_code": code,
            "error": error, "timestamp_ns": time.time_ns(),
        })
        notification = {"event": "notification", "run_id": run_id, "thread_id": args.thread_id,
                        "client_message_id": message_id, "outcome": "failed", "error": None}
        try:
            record_ = session_record(args.thread_id)
            route = record_ and {"provider": record_.provider, "model": record_.model,
                                 "effort": record_.effort}
            reply = call_broker(config_, "send", session=args.thread_id, message=args.message,
                                cwd=session_cwd(args.thread_id), route=route or {})
            notification["outcome"] = str(reply.get("outcome") or "started")
        except (DingError, broker.BrokerDown) as exc:
            notification["error"] = str(exc) or type(exc).__name__
        notification["timestamp_ns"] = time.time_ns()
        _append(record, notification)
        warning = "; do not blindly resend" if notification["outcome"] in {"queued", "unknown"} else ""
        detail = f"; {notification['error']}" if notification["error"] else ""
        print(f"ding: {notification['outcome']}{warning}{detail}; record: {record}", file=sys.stderr)
        return code


def task_exit_code(command: list[str], *, env: dict | None = None,
                   log: Path | None = None) -> tuple[int, str | None]:
    """Run the wrapped command and return `(exit code, error)`.

    When `log` names a file, the wrapper pumps the child's stdout and stderr into it. The
    wrapper does not hand the child a redirected handle: a wrapper started detached on
    Windows inherits a file handle its own child cannot use, so the output would be lost.
    Pumping also keeps the log live for `ding logs` while the job runs.
    """
    if log is None:
        try:
            child = subprocess.Popen(command, env=env)
        except OSError as error:
            return (127 if isinstance(error, FileNotFoundError) else 126), str(error)
        return _wait_for(child)

    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("ab") as sink:
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert child.stdout is not None
            reader = threading.Thread(target=_pump, args=(child.stdout, sink), daemon=True)
            reader.start()
            code, error = _wait_for(child)
            reader.join(timeout=10)
        return code, error
    except OSError as error:
        return (127 if isinstance(error, FileNotFoundError) else 126), str(error)


def _pump(stream, sink) -> None:
    """Copy a child's output into the job log as it appears."""
    try:
        for block in iter(lambda: stream.read(4096), b""):
            sink.write(block)
            sink.flush()
    except (OSError, ValueError):
        pass


def _wait_for(child) -> tuple[int, str | None]:
    try:
        code = child.wait()
    except KeyboardInterrupt:
        if WINDOWS:
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.send_signal(signal.SIGINT)
        try:
            code = child.wait(timeout=5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            child.kill()
            child.wait()
            code = -signal.SIGINT
    return (128 - code if code < 0 else code), None


def launch_task(args, command: list[str], config_: config.Config) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.name):
        raise DingError("launch name must be 1-64 letters, digits, underscores or hyphens, "
                        "starting with a letter or digit")
    session_id = args.session or os.environ.get("DSH_SESSION_ID")
    if not session_id:
        raise DingError("launch needs the calling session: run it from a DeepSeek Harness session, "
                        "or pass --session <id>")
    if launcher.jobs().get(args.name) is not None and launcher.job_state(launcher.jobs()[args.name]) == "running":
        raise DingError(f"a job named {args.name} is already running; pick another name")
    record = Path(args.record).resolve() if args.record else (
        launcher.launch_records() / f"{__import__('uuid').uuid4()}.jsonl")
    message = message_text(args.message or f"Task {args.name} exited; inspect {record}",
                           args.message_file, args.sender)
    job = launcher.launch(args.name, session_id, command, record=record, message=message,
                          socket=args.socket, cwd=Path.cwd())
    print(f"ding: created {job.name} via {job.backend}; {launcher.describe(job)}")
    return 0


def board_rows(config_: config.Config, *, all_sessions: bool) -> list[list[str]]:
    roots = [] if all_sessions else repository_roots()
    records = registry.load()
    known = logs.known_sessions()
    ids: list[str] = []
    for session_id in records:
        if session_id not in known and registry.get(session_id) is not None:
            ids.append(session_id)
    for session_id, session in known.items():
        cwd = session.cwd or (records[session_id].cwd if session_id in records else None)
        if all_sessions or (cwd and _under(cwd, roots)):
            if session_id not in ids:
                ids.append(session_id)
    for session_id in list(ids):
        record = records.get(session_id)
        cwd = (record.cwd if record else None) or (known[session_id].cwd if session_id in known else None)
        if not all_sessions and record is None and not (cwd and _under(cwd, roots)):
            ids.remove(session_id)
    states: dict[str, dict] = {}
    if broker.running():
        try:
            states = {entry: {} for entry in broker.request("ping").get("sessions", [])}
        except broker.BrokerDown:
            states = {}
    rows = []
    for session_id in ids:
        record = records.get(session_id)
        session = known.get(session_id)
        name = record.name if record else session_id
        cwd = (record.cwd if record else None) or (session.cwd if session else "")
        if session_id in states:
            state = "active" if states[session_id] else "idle"
        elif session is None:
            state = "unavailable"
        else:
            state = "notLoaded"
        issue = f"#{record.issue}" if record and record.issue else ""
        head = reply_head(session.reply) if session and session.reply else ""
        rows.append([name, cwd or "", state, issue, " ".join(head.splitlines()), session_id])
    return rows


def _under(path: str, roots: list[Path]) -> bool:
    try:
        candidate = Path(path).resolve()
    except OSError:
        return False
    return any(candidate.is_relative_to(root) for root in roots)


def repository_roots() -> list[Path]:
    cwd = Path.cwd().resolve()
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return [cwd]
    if root.returncode:
        return [cwd]
    try:
        result = subprocess.run(["git", "-c", "core.quotePath=false", "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, timeout=10, check=True)
    except subprocess.SubprocessError:
        return [cwd]
    paths_found = [field[len("worktree "):] for field in result.stdout.splitlines()
                   if field.startswith("worktree ")]
    roots = []
    for path in paths_found:
        try:
            roots.append(Path(path).resolve())
        except OSError:
            continue
    return roots or [cwd]


def executor_comment_heads(mapping: dict[str, set[int]], fields: tuple[str, ...],
                           config_: config.Config) -> dict[str, str]:
    """The status block from each session's latest ticket comment."""
    numbers = sorted({number for issues in mapping.values() for number in issues})
    if not numbers:
        return {}
    if not gh_available():
        return {}
    try:
        repo = github_repository()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    latest: dict[str, tuple] = {}
    for number in numbers:
        try:
            result = gh_json(["issue", "view", str(number), "--repo", repo, "--json", "comments"])
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
        comments = result.get("comments") if isinstance(result, dict) else None
        if not isinstance(comments, list):
            continue
        for ordinal, comment in enumerate(comments):
            if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
                continue
            ids = set()
            for value in all_references(comment["body"], config_, owner=False):
                ids.add(value)
            ids = {value for value in ids if number in mapping.get(value, set())}
            if not ids:
                continue
            order = comment.get("createdAt", ""), ordinal
            for session_id in ids:
                if session_id not in latest or order >= latest[session_id][0]:
                    latest[session_id] = order, comment["body"]
    heads = {}
    for session_id, (_, body) in latest.items():
        lines = body.replace("\r\n", "\n").lstrip().splitlines()
        if len(lines) >= len(fields) and all(
                re.match(rf"^[ \t]*{re.escape(label)}:[ \t]*\S", line)
                for label, line in zip(fields, lines)):
            heads[session_id] = " ".join(line.strip() for line in lines[:len(fields)])
    return heads


def issue_map(config_: config.Config) -> dict[str, set[int]]:
    """Which tickets each session owns or has signed, from GitHub and dkitchen's records."""
    mapping: dict[str, set[int]] = {}
    for record in registry.load().values():
        if record.issue:
            mapping.setdefault(record.id, set()).add(record.issue)
    if not gh_available():
        return mapping
    try:
        repo = github_repository()
        issues = gh_records(f"repos/{repo}/issues?state=all&per_page=100")
        comments = gh_records(f"repos/{repo}/issues/comments?per_page=100")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return mapping
    records = {issue["number"]: (issue, []) for issue in issues if "pull_request" not in issue}
    for comment in comments:
        number = int(comment["issue_url"].rsplit("/", 1)[-1])
        if number in records:
            records[number][1].append(comment)
    for number, (issue, thread) in records.items():
        owner: str | None = None
        for record in [issue, *thread]:
            found = all_references(record.get("body"), config_, owner=True)
            if found:
                owner = found[-1]
        values = [owner] if owner else []
        for record in thread:
            values.extend(all_references(record.get("body"), config_, owner=False))
        for value in values:
            mapping.setdefault(value, set()).add(number)
    return mapping


def show_board(config_: config.Config, *, all_sessions: bool, status_fields_: tuple[str, ...] | None) -> None:
    rows = board_rows(config_, all_sessions=all_sessions)
    mapping = issue_map(config_)
    heads = (executor_comment_heads({row[5]: mapping.get(row[5], set()) for row in rows},
                                    status_fields_, config_) if status_fields_ else {})
    jobs = launcher.jobs()
    print("NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN\tSESSION")
    for name, cwd, state, issue, head, session_id in rows:
        job = jobs.get(session_id)
        run = launcher.job_state(job, kill_check=False) if job is not None else ""
        print("\t".join(display_cell(cell) for cell in
                        [name, cwd, state, issue, heads.get(session_id, head), run, session_id]))


def show_jobs() -> None:
    jobs = launcher.jobs()
    if not jobs:
        return
    print("\nJOB\tSTATE\tBACKEND\tSTARTED\tCOMMAND")
    for name, job in sorted(jobs.items()):
        state = launcher.job_state(job)
        started = datetime.fromtimestamp(job.started).strftime("%Y-%m-%d %H:%M")
        print("\t".join(display_cell(cell) for cell in
                        [name, state, job.backend, started, " ".join(job.command)]))


# -- harness profile and skills -----------------------------------------------------------

PROFILE_TEMPLATE = """\
# The dkitchen profile: the ACP application used by `ding` to create and drive kitchen
# sessions. It is written by `ding harness init`; edit it to change the composition, then
# restart the broker (`ding down` and let the next command start it again).
{extra}
"""


def harness_init(config_: config.Config, *, profile: str | None, force: bool) -> int:
    """Write the profile `ding` boots: the ACP application over the user's own providers.

    The kitchen profile is not just the shipped ACP template. A bare `base + acp-app`
    composition knows one provider route and none of the user's sign-ins, so every station
    would fail on its first turn. This profile therefore starts from the profile the user
    already runs (its plugin bundles and its `llm-pi-ai` provider routes), swaps the web
    application for the ACP one, and reuses that profile's installed plugins instead of
    downloading a second copy of every native addon.
    """
    name = profile or config_.harness.profile
    target = paths.profile_dir(name)
    manifest = target / "package.json"
    if manifest.exists() and not force:
        print(f"ding: profile {name} already exists at {target}; pass --force to rewrite it",
              file=sys.stderr)
        return 1
    donor = _donor_profile()
    target.mkdir(parents=True, exist_ok=True)
    package = json.loads(json.dumps(donor["package"]))
    package["name"] = f"dsh-profile-{name}"
    profile_block = package.setdefault("dsh", {}).setdefault("profile", {})
    bundles = [bundle for bundle in profile_block.get("bundles", [])
               if bundle not in _WEB_APPLICATIONS]
    bundles = ["@deepseek-ai/dsh-base", *[bundle for bundle in bundles
                                          if bundle != "@deepseek-ai/dsh-base"],
               "@deepseek-ai/dsh-acp-app"]
    profile_block["bundles"] = bundles
    manifest.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    (target / "cordis.yml").write_text(
        "# dsh profile root — the tree is composed from the bundles in package.json.\n[]\n",
        encoding="utf-8")
    (target / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")
    (target / "cordis.patch.yml").write_text(
        PROFILE_TEMPLATE.format(extra=donor["patch"]), encoding="utf-8")
    if donor["link"]:
        _link_dependencies(target, donor["path"])
    print(f"ding: wrote profile {name} at {target} with bundles {', '.join(bundles)}")
    try:
        executable = paths.dsh_executable()
    except ValueError as error:
        print(f"ding: {error}", file=sys.stderr)
        return 1
    result = subprocess.run([executable, "--profile", name, "--help"], capture_output=True, text=True,
                            timeout=300)
    if result.returncode:
        print(f"ding: `dsh --profile {name} --help` failed:\n{(result.stderr or result.stdout).strip()}",
              file=sys.stderr)
        return 1
    print(f"ding: `dsh --profile {name}` boots")
    return 0


#: Profiles dkitchen may copy provider routes and plugin dependencies from, best first.
DONOR_PROFILES = ("desktop", "web", "tui")

#: Bundles that mount a browser application; the kitchen profile runs ACP instead.
_WEB_APPLICATIONS = {"@deepseek-ai/dsh-web-app", "@deepseek-ai/dsh-acp-app",
                     "@deepseek-ai/dsh-headless", "@deepseek-ai/dsh-sdk-app"} 


def _donor_profile() -> dict:
    """The profile whose providers and plugins the kitchen profile should inherit.

    A dkitchen profile that composed only the shipped bundles would have exactly one
    provider route (`deepseek-official`) and none of the user's credentials, which is not a
    usable kitchen. Copying the signed-in profile's dependencies and its `llm-pi-ai` routes
    gives the stations the same models the user already runs everywhere else.
    """
    for name in DONOR_PROFILES:
        directory = paths.profile_dir(name)
        manifest = directory / "package.json"
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        patch = ""
        try:
            patch = _llm_pi_ai_block((directory / "cordis.patch.yml").read_text(encoding="utf-8"))
        except OSError:
            patch = ""
        link = (directory / "node_modules").is_dir()
        return {"name": name, "path": directory, "package": package, "patch": patch, "link": link}
    return {"name": None, "path": None, "package": {"private": True, "dependencies": {}},
            "patch": "", "link": False}


def _link_dependencies(target: Path, donor: Path) -> None:
    """Point the new profile at the donor's installed plugins.

    Installing a second copy of every plugin would need another pnpm run and another copy of
    every native addon; a directory junction (Windows) or symlink (POSIX) reuses the donor's
    `node_modules` instead. A failure here is reported, never fatal: plugins are optional.
    """
    link = target / "node_modules"
    if link.exists():
        return
    try:
        if paths.WINDOWS:
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(donor / "node_modules")],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise OSError((result.stderr or result.stdout).strip())
        else:
            link.symlink_to(donor / "node_modules", target_is_directory=True)
    except OSError as error:
        print(f"ding: could not link {donor / 'node_modules'} into {target}: {error}", file=sys.stderr)


def _llm_pi_ai_block(text: str) -> str:
    """The `llm-pi-ai` row of a patch file, verbatim, when it configures providers."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if re.match(r"^\s*-\s+id:\s*llm-pi-ai\s*$", line)), None)
    if start is None:
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^-\s", lines[index]) or re.match(r"^#", lines[index]):
            end = index
            break
    block = "\n".join(lines[start:end]).rstrip()
    if not block:
        return ""
    header = ("# Provider routes carried over from an existing profile, so the kitchen's stations\n"
              "# have models to run. `ding config models` lists what they offer.\n")
    return header + block + "\n"


def skill_install(args) -> int:
    """Copy the bundled setup skill into a repository's DSH skill root."""
    source = Path(__file__).resolve().parent / "skills" / "setup-dkitchen"
    if not source.is_dir():
        print("ding: this installation ships no setup skill", file=sys.stderr)
        return 1
    root = Path(args.repo).expanduser().resolve() if args.repo else config.repository_root()
    destination = root / ".agents" / "skills" / "setup-dkitchen"
    if destination.exists() and not args.force:
        print(f"ding: {destination} exists; pass --force to replace it", file=sys.stderr)
        return 1
    import shutil

    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"ding: installed the setup skill at {destination}")
    return 0


# -- configuration editing ----------------------------------------------------------------

def config_show(config_: config.Config) -> None:
    payload = {
        "repo": config_.repo,
        "kitchen": {"stations": list(config_.kitchen.stations)},
        "harness": {"profile": config_.harness.profile,
                    "default_provider": config_.harness.default_provider,
                    "default_model": config_.harness.default_model,
                    "approvals": config_.harness.approvals,
                    "permission": config_.harness.permission},
        "models": {station: config_.route(station).as_dict() for station in config_.kitchen.stations},
        "sessions": {"owner_line": config_.sessions.owner_line,
                     "session_line": config_.sessions.session_line,
                     "name": config_.sessions.name},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def config_write(config_: config.Config, mutations: dict[str, object]) -> None:
    """Rewrite `.dkitchen.toml` with a few changed values, keeping everything else."""
    path = config_.root / config.FILE
    data = _read_toml(path)
    for dotted, value in mutations.items():
        parts = dotted.split(".")
        table = data
        for part in parts[:-1]:
            nested = table.get(part)
            if not isinstance(nested, dict):
                nested = {}
                table[part] = nested
            table = nested
        table[parts[-1]] = value
    path.write_text(_dump_toml(data), encoding="utf-8")
    print(f"ding: wrote {path}", file=sys.stderr)


def _read_toml(path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _dump_toml(data: dict) -> str:
    """A deliberately small TOML writer: scalars, arrays of strings, and one level of tables."""
    lines: list[str] = []

    def scalar(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(json.dumps(str(item), ensure_ascii=False) for item in value) + "]"
        return json.dumps(str(value), ensure_ascii=False)

    def emit(table: dict, prefix: str) -> None:
        simple = {key: value for key, value in table.items() if not isinstance(value, dict)}
        nested = {key: value for key, value in table.items() if isinstance(value, dict)}
        if prefix:
            lines.append(f"[{prefix}]")
        for key, value in simple.items():
            lines.append(f"{key} = {scalar(value)}")
        lines.append("")
        for key, value in nested.items():
            emit(value, f"{prefix}.{key}" if prefix else key)

    emit(data, "")
    return "\n".join(lines).rstrip() + "\n"


def config_command(args, config_: config.Config) -> int:
    action = getattr(args, "config_action", None) or "show"
    if action == "show":
        config_show(config_)
        return 0
    if action in {"enable", "disable"}:
        if args.station not in config.STATIONS:
            raise DingError(f"unknown station {args.station!r}; use any of {', '.join(config.STATIONS)}")
        current = list(config_.kitchen.stations)
        if action == "enable" and args.station not in current:
            current.append(args.station)
        if action == "disable":
            if args.station not in current:
                raise DingError(f"{args.station} is already disabled")
            if len(current) == 1:
                raise DingError("the kitchen needs at least one station")
            current.remove(args.station)
        ordered = [name for name in config.STATIONS if name in current]
        config_write(config_, {"kitchen.stations": ordered})
        print(f"ding: stations are now {', '.join(ordered)}")
        return 0
    if action == "set":
        config_write(config_, {args.key: _coerce(args.value)})
        print(f"ding: set {args.key}")
        return 0
    if action == "models":
        reply = call_broker(config_, "models", cwd=str(config_.root))
        for row in reply.get("models") or []:
            print(f"{row.get('provider')}/{row.get('model')}\t{row.get('name')}")
        efforts = reply.get("efforts") or []
        if efforts:
            print("efforts\t" + ", ".join(str(item.get("value")) for item in efforts))
        return 0
    raise DingError(f"unknown config action: {action}")


def _coerce(value: str) -> object:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return value


def message_text(message: str | None, message_file: str | None, sender: str | None) -> str:
    if message_file is not None:
        with Path(message_file).open(encoding="utf-8", newline="") as handle:
            message = handle.read()
    elif message == "-":
        message = sys.stdin.read()
    if message is None:
        message = ""
    if sender is not None:
        if not sender.strip() or "\n" in sender or "\r" in sender:
            raise ValueError("--from must be a non-empty single line.")
        message = f"[from {sender}]\n{message}"
    return message


# -- entry point ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ding", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    try:
        installed = metadata.version("dkitchen")
    except metadata.PackageNotFoundError:
        installed = "unknown"
    parser.add_argument("--version", action="version", version=f"ding {installed}")
    commands = parser.add_subparsers(dest="action", required=True)

    sub = commands.add_parser("new", help="Create a DeepSeek Harness session and start its first message")
    sub.add_argument("--cwd", required=True, help="Existing working directory for the session")
    sub.add_argument("--name", required=True,
                     help="Session title; with --issue, filled into the [sessions] name template")
    sub.add_argument("--issue", type=int,
                     help="Ticket this session owns: formats --name with the [sessions] name template "
                          "and asks the session to post its owner line")
    sub.add_argument("--station", choices=config.STATIONS,
                     help="Station this session serves (default: executor)")
    sub.add_argument("--provider", help="Provider to select for this session, overriding [models]")
    sub.add_argument("--model", help="Model to select for this session, overriding [models]")
    message = sub.add_mutually_exclusive_group(required=True)
    message.add_argument("--message", help="First message text, or '-' to read stdin")
    message.add_argument("--message-file", help="UTF-8 file holding the first message")

    board = commands.add_parser("board", help="List sessions, their tickets, replies, and jobs")
    board.add_argument("--all", action="store_true", help="Include sessions in every directory")
    board.add_argument("--status", action="store_true", help="Show the latest ticket status block")
    board.add_argument("--status-fields", type=status_fields,
                       help="Comma-separated field names that open a status block (default: "
                            "$DING_STATUS_FIELDS, then [status] fields, then "
                            f"'{STATUS_FIELDS}')")
    board.add_argument("--no-start", action="store_true", help="Do not start a broker to refresh states")

    for action, summary in (
            ("read", "Print a session's latest reply"),
            ("status", "Print a session's status as JSON"),
            ("info", "Print a session's id, name, working directory, status, and route as JSON"),
            ("rename", "Change a dkitchen session's name"),
            ("send", "Send a message to a session")):
        sub = commands.add_parser(action, help=summary)
        target = sub.add_mutually_exclusive_group(required=True)
        target.add_argument("thread_id", nargs="?",
                            help="Session id, exact name, '#NUMBER', OWNER/REPO#NUMBER, or issue URL")
        target.add_argument("--issue", type=int, help="Issue's owner line, or its unique session")
        if action == "read":
            sub.add_argument("--head", action="store_true", help="Print only the first paragraph")
        if action == "rename":
            sub.add_argument("--name", required=True, help="New session name")
        if action == "send":
            message = sub.add_mutually_exclusive_group(required=True)
            message.add_argument("--message", help="Message text, or '-' to read stdin")
            message.add_argument("--message-file", help="UTF-8 message file")
            sub.add_argument("--from", dest="sender",
                             help="Prefix the message with '[from NAME]' on its own line")

    sub = commands.add_parser("run", help="Run a command and message a session when it exits")
    run_target = sub.add_mutually_exclusive_group(required=True)
    run_target.add_argument("thread_id", nargs="?")
    run_target.add_argument("--issue", type=int)
    message = sub.add_mutually_exclusive_group(required=True)
    message.add_argument("--message", help="Message text to deliver when the job exits")
    message.add_argument("--message-file", help="UTF-8 message file")
    sub.add_argument("--record", required=True, help="Append-only local exit/notification JSONL")
    sub.add_argument("--log", help="File the wrapped command's own output goes to")
    sub.add_argument("--from", dest="sender")

    sub = commands.add_parser("launch", help="Run a long job in the background and wake this session when it exits")
    sub.add_argument("name", help="Job name, 1-64 letters, digits, underscores or hyphens")
    message = sub.add_mutually_exclusive_group()
    message.add_argument("--message", help="Wake-up text, or '-' to read stdin")
    message.add_argument("--message-file", help="UTF-8 wake-up message file")
    sub.add_argument("--from", dest="sender")
    sub.add_argument("--record", help="Where to write the job's record (default: under the state root)")
    sub.add_argument("--session", help="Session to wake (default: $DSH_SESSION_ID)")
    sub.add_argument("--socket", help="Accepted for compatibility; unused")

    sub = commands.add_parser("stop", help="End a launched job and everything it started")
    sub.add_argument("name")
    sub = commands.add_parser("logs", help="Print a launched job's latest output")
    sub.add_argument("name")
    sub.add_argument("--lines", type=int, default=40)

    commands.add_parser("up", help="Start the broker that owns the DeepSeek Harness connection")
    commands.add_parser("down", help="Stop the broker")

    sub = commands.add_parser("approve", help="Answer a pending permission request")
    sub.add_argument("thread_id", nargs="?")
    sub.add_argument("--issue", type=int)
    sub.add_argument("--decision", choices=["allow", "reject"], default="allow")

    sub = commands.add_parser("config", help="Show or change this repository's kitchen settings")
    config_sub = sub.add_subparsers(dest="config_action")
    config_sub.add_parser("show", help="Print the effective settings")
    for name, summary in (("enable", "Enable a station"), ("disable", "Disable a station")):
        entry = config_sub.add_parser(name, help=summary)
        entry.add_argument("station")
    entry = config_sub.add_parser("set", help="Set one key, such as harness.profile")
    entry.add_argument("key")
    entry.add_argument("value")
    config_sub.add_parser("models", help="List the provider/model routes the harness offers")

    harness = commands.add_parser("harness", help="Install or inspect the DeepSeek Harness profile ding boots")
    harness_sub = harness.add_subparsers(dest="harness_action", required=True)
    init = harness_sub.add_parser("init", help="Write the profile under $DSH_HOME/profiles")
    init.add_argument("--profile", help="Profile name (default: [harness] profile)")
    init.add_argument("--force", action="store_true", help="Rewrite an existing profile")

    skill = commands.add_parser("skill", help="Install dkitchen's setup skill into a repository")
    skill_sub = skill.add_subparsers(dest="skill_action", required=True)
    install = skill_sub.add_parser("install", help="Copy the skill into <repo>/.agents/skills")
    install.add_argument("--repo", help="Repository root (default: this work tree)")
    install.add_argument("--force", action="store_true")

    sub = commands.add_parser("relay", help="Send a message to a ticket's executor, then leave it on the ticket")
    sub.add_argument("ticket", type=int, help="Ticket number in this repository")
    message = sub.add_mutually_exclusive_group(required=True)
    message.add_argument("--message", help="What the executor receives, or '-' to read stdin")
    message.add_argument("--message-file", help="UTF-8 message file")
    sub.add_argument("--comment", help="Ticket comment (default: the message itself)")
    sub.add_argument("--no-comment", action="store_true", help="Send only; leave no ticket comment")
    sub.add_argument("--to", help="Executor session id or name, when the ticket has no owner line")

    sub = commands.add_parser("merge", help="Merge a reviewed PR at the reviewed HEAD")
    sub.add_argument("pr", type=int, help="PR number")
    sub.add_argument("sha", help="The HEAD that was reviewed (full or prefix)")
    ticket = sub.add_mutually_exclusive_group()
    ticket.add_argument("--ticket", type=int,
                        help="The PR's ticket, whose executor must be waiting on the orchestrator")
    ticket.add_argument("--no-ticket", action="store_true", help="Merge a PR that belongs to no ticket")

    sub = commands.add_parser("log", help="Post an entry to the orchestrator log ticket")
    sub.add_argument("--item", action="append", required=True, help="One automatic action; repeat for more")
    sub.add_argument("--pending", action="append", default=[],
                     help="One matter waiting for the user; repeat for more")
    sub.add_argument("--issue", type=int, help="Log ticket (default: [log] issue in .dkitchen.toml)")

    sub = commands.add_parser("watch", help="Wait for GitHub activity the orchestrator must handle, then exit")
    sub.add_argument("--state", type=Path, help="JSON file holding the last reported time")
    sub.add_argument("--since", help="Start time for the first run (ISO, UTC; default: now)")
    sub.add_argument("--skip-issue", type=int, action="append", default=[],
                     help="Ticket to ignore; the orchestrator log ticket is always ignored")
    sub.add_argument("--checkout", type=Path, help="Checkout where `ding board` lists ticket sessions")
    sub.add_argument("--interval", type=float, default=90, help="Seconds between polls (default: 90)")
    sub.add_argument("--settle", type=float, default=300,
                     help="After the first trigger, seconds to wait to batch more (default: 300)")
    sub.add_argument("--stale-hours", type=float, default=6,
                     help="Executor silence that counts as stale (default: 6)")
    sub.add_argument("--stale-every", type=float, default=3600,
                     help="Seconds between stale checks (default: 3600)")
    sub.add_argument("--max-errors", type=int, default=10,
                     help="Consecutive failed polls before exiting (default: 10)")

    sub = commands.add_parser("check-refs", help="Check file:line and sha256 references in a draft comment")
    sub.add_argument("draft", help="Draft comment file")
    where = sub.add_mutually_exclusive_group()
    where.add_argument("--ref", default="origin/main", help="Git revision the references point into")
    where.add_argument("--root", type=Path, help="Directory to read files from instead")
    sub.add_argument("--tolerance", type=int, default=0,
                     help="Lines around a reference that may hold its name (default: 0)")

    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if argv and argv[0] in {"run", "launch"} and "--" in argv:
        separator = argv.index("--")
        command = argv[separator + 1:]
        argv = argv[:separator]
    args = parser.parse_args(argv)
    if getattr(args, "issue", None) is not None and args.issue <= 0:
        parser.error("--issue must be a positive issue number")
    if args.action == "merge" and getattr(args, "ticket", None) is not None and args.ticket <= 0:
        parser.error("--ticket must be a positive issue number")
    if args.action in {"run", "launch"} and not command:
        parser.error(f"{args.action} requires a task command after --")
    try:
        settings_ = settings()
        if args.action == "new":
            return new_task(args, settings_)
        if args.action == "send":
            args.message = message_text(args.message, args.message_file, args.sender)
            return send_task(args, settings_)
        if args.action == "read":
            return read_task(args, settings_)
        if args.action == "status":
            return status_task(args, settings_)
        if args.action == "info":
            return info_task(args, settings_)
        if args.action == "rename":
            if not args.name.strip():
                parser.error("--name must contain non-whitespace text")
            return rename_task(args, settings_)
        if args.action == "run":
            args.message = message_text(args.message, args.message_file, args.sender)
            return run_task(args, command, settings_)
        if args.action == "launch":
            return launch_task(args, command, settings_)
        if args.action == "stop":
            ok, detail = launcher.stop(args.name)
            print(f"ding: {detail}", file=sys.stderr)
            return 0 if ok else 1
        if args.action == "logs":
            print(launcher.log_tail(args.name, lines=args.lines))
            return 0
        if args.action == "up":
            if ensure_broker(settings_):
                print(f"ding: broker running (pid {broker.request('ping').get('pid')})")
                return 0
            raise DingError("the broker did not start; see " + str(broker.log_file()))
        if args.action == "down":
            if not broker.running():
                print("ding: no broker is running", file=sys.stderr)
                return 0
            try:
                broker.request("shutdown", timeout=10)
            except broker.BrokerDown:
                pass
            for _ in range(100):
                if not broker.running():
                    print("ding: broker stopped")
                    return 0
                time.sleep(0.1)
            raise DingError("the broker did not stop in time")
        if args.action == "approve":
            session_id = resolve_target(args.thread_id, args.issue, settings_)
            call_broker(settings_, "approve", session=session_id, decision=args.decision)
            print(f"ding: {args.decision} sent for {session_id}")
            return 0
        if args.action == "board":
            fields = args.status_fields
            if args.status and fields is None:
                environment = os.environ.get("DING_STATUS_FIELDS")
                fields = status_fields(environment) if environment else settings_.status.fields
            if not args.no_start:
                try:
                    ensure_broker(settings_)
                except DingError:
                    pass
            show_board(settings_, all_sessions=args.all, status_fields_=fields if args.status else None)
            show_jobs()
            if not WINDOWS:
                show_unwrapped(unwrapped_tasks())
            return 0
        if args.action == "config":
            return config_command(args, settings_)
        if args.action == "harness":
            return harness_init(settings_, profile=args.profile, force=args.force)
        if args.action == "skill":
            return skill_install(args)
        if args.action == "check-refs":
            from dkitchen import refs
            return refs.check_refs(args)
        if args.action in {"relay", "merge", "log", "watch"}:
            from dkitchen import orchestrate, watch
            handler = watch.watch if args.action == "watch" else getattr(orchestrate, args.action)
            return handler(args, settings_)
        parser.error(f"unknown command: {args.action}")
        return 2
    except DingError as error:
        print(f"ding: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, acp.AcpError) as error:
        print(f"ding: {error}", file=sys.stderr)
        return 1


def show_unwrapped(findings: list[dict]) -> None:
    if not findings:
        return
    print("\nUNWRAPPED_PID\tCPU%\tAGE_SECONDS\tTMUX_SESSION\tTMUX_WINDOW\tCOMMAND")
    for process in findings:
        cells = [str(process["pid"]), f"{process['cpu']:.1f}", f"{process['age']:.0f}",
                 process["session"], process["window"],
                 " ".join(process["argv"]) if WINDOWS else _shell_join(process["argv"])]
        print("\t".join(display_cell(cell) for cell in cells))


def _shell_join(argv: list[str]) -> str:
    import shlex
    return shlex.join(argv)


def unwrapped_tasks(*, min_age: float = 600, min_cpu: float = 10) -> list[dict]:
    """Long jobs nobody was woken for; POSIX only, because it reads /proc."""
    if WINDOWS:
        return []
    return _unwrapped_processes(_process_snapshot(), _tmux_panes(), min_age=min_age, min_cpu=min_cpu)


def _process_snapshot(proc: Path = Path("/proc")) -> dict[int, dict]:
    import math
    try:
        uptime = float((proc / "uptime").read_text().split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
        directories = list(proc.iterdir())
    except (OSError, ValueError, IndexError, AttributeError):
        return {}
    processes = {}
    for directory in directories:
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            age = uptime - int(fields[19]) / ticks
            cpu = (int(fields[11]) + int(fields[12])) / ticks / age * 100 if age > 0 else 0
            raw = (directory / "cmdline").read_bytes()
            parts = raw.split(b"\0") if raw else []
            if parts and parts[-1] == b"":
                parts.pop()
            argv = [part.decode("utf-8", errors="replace") for part in parts]
            processes[int(directory.name)] = {
                "pid": int(directory.name), "ppid": int(fields[1]), "state": fields[0],
                "age": age, "cpu": cpu, "argv": argv, "uid": directory.stat().st_uid,
            }
        except (OSError, ValueError, IndexError, ArithmeticError):
            continue
    return processes


def _tmux_panes() -> dict[int, dict]:
    import shutil
    tmux = shutil.which("tmux")
    if tmux is None:
        return {}
    try:
        result = subprocess.run([tmux, "list-panes", "-a", "-F",
                                 "#{pane_pid}\t#{pane_id}\t#{session_name}\t#{window_name}"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    panes = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 4 and fields[0].isdecimal():
                panes[int(fields[0])] = {"pane_id": fields[1], "session": fields[2],
                                         "window": fields[3]}
    return panes


def _unwrapped_processes(processes: dict[int, dict], panes: dict[int, dict], *,
                         min_age: float, min_cpu: float) -> list[dict]:
    """Long-running processes that no `ding run` wraps, each reported once."""
    pane_ids = {pane["pane_id"]: pane for pane in panes.values()}
    wrapper_parents, other_parents = set(), set()
    for child in processes.values():
        if child["state"] not in {"Z", "X", "x"}:
            parents = wrapper_parents if _is_run_wrapper(child["argv"]) else other_parents
            parents.add(child["ppid"])
    wrapper_only = wrapper_parents - other_parents
    findings, pane_findings = [], {}
    for pid, process in sorted(processes.items()):
        if (process["state"] in {"Z", "X", "x"} or not process["argv"] or process["age"] <= 0
                or _infrastructure(process["argv"])):
            continue
        ancestors, seen, current, wrapped, complete = [], set(), pid, False, False
        pane = None
        pane_chain: list[int] = []
        while current and current not in seen:
            seen.add(current)
            ancestor = processes.get(current)
            if ancestor is None:
                break
            ancestors.append(current)
            if _is_run_wrapper(ancestor["argv"]):
                wrapped = True
                break
            if pane is None and current in panes:
                pane = panes[current]
                pane_chain = ancestors.copy()
            if current == 1 or ancestor["ppid"] == 0:
                complete = True
                break
            current = ancestor["ppid"]
        if wrapped or not complete:
            continue
        fallback = pane is None
        if pane is not None:
            for depth, ancestor_pid in enumerate(reversed(pane_chain)):
                candidate = processes[ancestor_pid]
                if (not candidate["argv"] or _interactive_shell(candidate["argv"])
                        or (ancestor_pid in wrapper_only
                            and Path(candidate["argv"][0]).name.lstrip("-") in SHELL_PROGRAMS)
                        or _infrastructure(candidate["argv"])):
                    continue
                process = candidate
                break
            else:
                continue
            if process["age"] < min_age:
                continue
            rank = (fallback, depth, process["pid"])
            previous = pane_findings.get(pane["pane_id"])
            if previous is None or rank < previous[0]:
                pane_findings[pane["pane_id"]] = (rank, {**process, "session": pane["session"],
                                                         "window": pane["window"]})
        elif process["age"] >= min_age and process["cpu"] >= min_cpu:
            findings.append({**process, "session": "", "window": ""})
    findings.extend(row for _, row in pane_findings.values())
    return sorted(findings, key=lambda row: row["pid"])


def _is_run_wrapper(argv: list[str]) -> bool:
    program, arguments = _process_command(argv)
    name = Path(program).name
    return (name in {"ding", "ding.py", "dkitchen"} or "dkitchen.ding" in " ".join(arguments[:2])) \
        and "run" in arguments


def _process_command(argv: list[str]) -> tuple[str, list[str]]:
    if not argv:
        return "", []
    executable = Path(argv[0]).name
    if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable):
        index = 1
        while index < len(argv):
            value = argv[index]
            if value == "--":
                index += 1
                break
            if not value.startswith("-"):
                break
            if value in {"-m", "-c"}:
                return (argv[index + 1], argv[index + 2:]) if index + 1 < len(argv) else (argv[0], [])
            index += 1
        if index < len(argv):
            return argv[index], argv[index + 1:]
    return argv[0], argv[1:]


def _infrastructure(argv: list[str]) -> bool:
    name = Path(argv[0]).name if argv else ""
    return name.startswith("tmux:") or name in {
        "tmux", "sshd", "autossh", "cloudflared", "dsh", "dsh.cmd", "code", "code-server",
        "vscode-server", "vim", "nvim", "emacs", "zed", "clangd", "gopls", "rust-analyzer",
        "pylsp", "pyright", "tsserver", "node",
    } or bool(re.fullmatch(r"dsh-[A-Za-z0-9_-]+", name))


def _interactive_shell(argv: list[str]) -> bool:
    if not argv or Path(argv[0]).name.lstrip("-") not in SHELL_PROGRAMS:
        return False
    for value in argv[1:]:
        if value in {"--rcfile", "--init-file"}:
            continue
        if not value.startswith(("-", "+")) or value == "-":
            return False
        if value.startswith("-") and not value.startswith("--") and "c" in value[1:]:
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
