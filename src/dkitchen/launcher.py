"""Start a long job and wake the session that started it when the job exits.

`ding launch` wraps a command so that its exit wakes an agent session, and keeps the output
where a human can read it. Two backends implement that:

- **tmux**, on POSIX. The job gets its own session and window, keeps running after the
  launcher exits, and stays readable with `tmux attach`; the pane stays open after exit.
- **a detached process**, on Windows, where tmux is not part of the system. The job runs in
  its own process group with stdout and stderr appended to a log file, and `ding stop` ends
  the whole tree.

Both record the same facts in the run registry, so `ding board` shows the same RUN column
and the same failure vocabulary on either platform. The one thing Windows cannot offer is
`board`'s local unwrapped-process audit, which reads `/proc`; that section is POSIX-only and
says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from dkitchen import paths

#: Job states `ding board` prints, identical on both platforms.
RUNNING = "running"
LOST = "lost"


@dataclass(frozen=True)
class Job:
    """One launched job, as recorded for `ding board`, `ding logs`, and `ding stop`."""

    name: str
    run_id: str
    pid: int | None
    record: Path
    log: Path | None
    started_ns: int
    command: list[str]
    backend: str
    session: str | None = None

    @property
    def started(self) -> float:
        return self.started_ns / 1_000_000_000


def registry() -> Path:
    return paths.state("launch-jobs.jsonl")


def launch_records() -> Path:
    return paths.state("launch-records")


def tmux_available() -> bool:
    return sys.platform != "win32" and shutil.which("tmux") is not None


def backend() -> str:
    return "tmux" if tmux_available() else "process"


def record_job(job: Job) -> None:
    target = registry()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**job.__dict__, "record": str(job.record),
                                 "log": str(job.log) if job.log else None},
                                ensure_ascii=False) + "\n")


def jobs() -> dict[str, Job]:
    """Every job this user launched, newest wins per name."""
    found: dict[str, Job] = {}
    try:
        lines = registry().read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        try:
            found[value["name"]] = Job(
                name=value["name"], run_id=str(value["run_id"]), pid=value.get("pid"),
                record=Path(value["record"]), log=Path(value["log"]) if value.get("log") else None,
                started_ns=int(value["started_ns"]), command=list(value.get("command") or []),
                backend=str(value.get("backend") or "process"),
                session=value.get("session") if isinstance(value.get("session"), str) else None,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return found


def job_state(job: Job, *, kill_check: bool = True) -> str:
    """`running`, `exited(CODE)`, `notify_failed`, `lost`, or `unknown`, from the record."""
    from dkitchen import ding

    return ding.run_state_from_record(job.record, job.run_id, job.pid, kill_check=kill_check)


def launch(name: str, session_id: str, command: list[str], *, record: Path, message: str,
           socket: str | None, cwd: Path) -> Job:
    """Start `command` in the background, wrapped so its exit wakes `session_id`."""
    launch_records().mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    log = launch_records() / f"{run_id}.log"
    inner = [os.path.abspath(sys.executable), "-m", "dkitchen.ding", "run", session_id,
             "--record", str(record), "--message", message]
    if not tmux_available():
        # A detached wrapper passes no usable handle down to its child on Windows, so the
        # wrapper opens the log itself.
        inner.extend(["--log", str(log)])
    if socket:
        inner.extend(["--socket", socket])
    inner.extend(["--", *command])
    if tmux_available():
        job = _launch_tmux(name, inner, record=record, command=command, run_id=run_id, cwd=cwd,
                           session_id=session_id)
    else:
        job = _launch_process(name, inner, record=record, command=command, run_id=run_id, cwd=cwd,
                              session_id=session_id, log=log)
    record_job(job)
    return job


def _launch_tmux(name: str, inner: list[str], *, record: Path, command: list[str],
                 run_id: str, cwd: Path, session_id: str) -> Job:
    tmux = shutil.which("tmux")
    if tmux is None:  # pragma: no cover - guarded by tmux_available
        raise ValueError("tmux executable not found in the caller's PATH")
    environment = {
        "DSH_SESSION_ID": session_id,
        "DKITCHEN_RUN_ID": run_id,
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME") or str(Path.home()),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    arguments = [str(Path(tmux).absolute()), "new-session", "-d", "-s", f"ding-{name}",
                 "-n", name, "-c", str(cwd)]
    for key, value in environment.items():
        arguments.extend(["-e", f"{key}={value}"])
    prelude = (f'{shlex.quote(str(Path(tmux).absolute()))} set-option -p -t "$TMUX_PANE" '
               f'remain-on-exit on && exec {shlex.join(inner)}')
    arguments.extend(["-P", "-F", "#{session_name}\t#{window_name}", "/bin/sh", "-c", prelude])
    # tmux treats a trailing semicolon as a command separator even in argv values.
    arguments = [value[:-1] + r"\;" if value.endswith(";") else value for value in arguments]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"tmux launch failed: {result.stderr.strip() or result.returncode}")
    return Job(name=name, run_id=run_id, pid=None, record=record, log=None,
               started_ns=time.time_ns(), command=command, backend="tmux", session=session_id)


def _launch_process(name: str, inner: list[str], *, record: Path, command: list[str],
                    run_id: str, cwd: Path, session_id: str, log: Path) -> Job:
    """Start the wrapper detached, keeping its diagnostics beside the job's own output.

    The wrapper's streams go to a separate `.wrapper.log`: writing them into `log` would
    give two handles on one file, and on Windows the child's pumped output then lands on top
    of the wrapper's lines (or the other way round), losing one of the two.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    wrapper_log = log.with_suffix(".wrapper.log")
    environment = os.environ.copy()
    environment["DKITCHEN_RUN_ID"] = run_id
    environment["DSH_SESSION_ID"] = session_id
    handle = wrapper_log.open("ab")
    try:
        kwargs: dict = {
            "cwd": str(cwd), "env": environment, "stdin": subprocess.DEVNULL,
            "stdout": handle, "stderr": subprocess.STDOUT,
        }
        if paths.WINDOWS:
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(inner, **kwargs)
    finally:
        handle.close()
    return Job(name=name, run_id=run_id, pid=process.pid, record=record, log=log,
               started_ns=time.time_ns(), command=command, backend="process", session=session_id)


def stop(name: str) -> tuple[bool, str]:
    """End a launched job and everything it started."""
    found = jobs().get(name)
    if found is None:
        return False, f"no launched job named {name}"
    if found.backend == "tmux":
        tmux = shutil.which("tmux")
        if tmux is None:
            return False, "tmux is not on PATH"
        result = subprocess.run([tmux, "kill-session", "-t", f"=ding-{name}"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            return False, f"tmux could not end ding-{name}: {(result.stderr or '').strip()}"
        return True, f"ended tmux session ding-{name}"
    pid = found.pid
    if pid is None:
        return False, f"job {name} recorded no process id"
    if paths.WINDOWS:
        result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode and "not found" not in (result.stderr or "").lower():
            return False, f"taskkill failed: {(result.stderr or result.stdout).strip()}"
        return True, f"ended process tree {pid}"
    try:
        os.killpg(os.getpgid(pid), 15)
    except (ProcessLookupError, PermissionError) as error:
        return False, f"cannot stop process group {pid}: {error}"
    return True, f"ended process group {pid}"


def log_tail(name: str, *, lines: int = 40) -> str:
    """The last lines of a detached job's output, or an explanation of why there are none."""
    found = jobs().get(name)
    if found is None:
        return f"no launched job named {name}"
    if found.log is None:
        tmux = shutil.which("tmux")
        if tmux is None:
            return f"job {name} runs in tmux, which is not on PATH"
        result = subprocess.run([tmux, "capture-pane", "-p", "-t", f"=ding-{name}", "-S", f"-{lines}"],
                                capture_output=True, text=True, timeout=30)
        return result.stdout if result.returncode == 0 else f"tmux could not read ding-{name}"
    try:
        text = found.log.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"cannot read {found.log}: {error}"
    return "\n".join(text.splitlines()[-lines:])


def describe(job: Job) -> str:
    """The one line `ding launch` prints, naming where the output and the record are."""
    where = f"record: {job.record}"
    if job.log is not None:
        where += f"; log: {job.log}"
    else:
        where += f"; tmux session: ding-{job.name}"
    return where


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")
