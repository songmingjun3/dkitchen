"""Run records: the exit/notification evidence behind `ding board`'s RUN column.

The vocabulary is unchanged from the Codex era — `running`, `exited(CODE)`, `notify_failed`,
`lost`, `unknown` — but the evidence is now read from `ding run`'s record file plus the
lock its wrapper holds, so these tests drive that pair directly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from dkitchen import ding, locks, paths

THREAD = "00000000-0000-4000-8000-000000000001"


def append(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def state(record: Path, run_id: str, *, pid: int | None = None, kill_check: bool = True) -> str:
    return ding.run_state_from_record(record, run_id, pid, kill_check=kill_check)


def test_a_record_with_no_exit_is_running_only_while_the_wrapper_holds_its_lock(tmp_path):
    record = tmp_path / "exit.jsonl"
    record.write_text("", encoding="utf-8")
    run_id = "r1"
    # No lock holder: the wrapper died before the job exited.
    assert state(record, run_id) == "lost"
    with locks.exclusive(paths.state("run-locks", f"{run_id}.lock"), blocking=True):
        assert state(record, run_id) == "running"


@pytest.mark.parametrize("outcome, expected", [
    ("started", "exited(7)"), ("queued_behind_active", "exited(7)"),
    ("skipped_active", "exited(7)"), ("queue_failed", "notify_failed"),
    ("failed", "notify_failed"), ("queued", "notify_failed"), ("unknown", "notify_failed"),
])
def test_exit_and_notification_outcomes_map_to_the_documented_states(tmp_path, outcome, expected):
    record = tmp_path / "exit.jsonl"
    append(record, {"event": "task_exit", "run_id": "r", "thread_id": THREAD, "exit_code": 7})
    append(record, {"event": "notification", "run_id": "r", "thread_id": THREAD, "outcome": outcome})
    assert state(record, "r") == expected


def test_rows_belonging_to_another_run_are_ignored(tmp_path):
    record = tmp_path / "exit.jsonl"
    append(record, {"event": "task_exit", "run_id": "older", "exit_code": 0})
    append(record, {"event": "notification", "run_id": "older", "outcome": "started"})
    assert state(record, "current") == "lost"


def test_an_exit_without_a_notification_reports_notify_failed_once_the_wrapper_is_gone(tmp_path):
    record = tmp_path / "exit.jsonl"
    append(record, {"event": "task_exit", "run_id": "r", "thread_id": THREAD, "exit_code": 0})
    assert state(record, "r") == "notify_failed"
    with locks.exclusive(paths.state("run-locks", "r.lock"), blocking=True):
        assert state(record, "r") == "exited(0)"


def test_a_missing_record_is_lost(tmp_path):
    assert state(tmp_path / "missing.jsonl", "r") == "lost"


@pytest.mark.parametrize("evidence, expected", [
    ('{"event":', "lost"),
    ("{}\n", "lost"),
    ('bad\n{"event": "task_exit", "run_id": "r", "exit_code": 0}\n', "unknown"),
    ('{"event": "task_exit", "run_id": "r", "exit_code": "x"}\n', "lost"),
])
def test_corrupt_evidence_is_reported_as_unknown_not_guessed(tmp_path, evidence, expected):
    record = tmp_path / "exit.jsonl"
    record.write_text(evidence, encoding="utf-8")
    assert state(record, "r") == expected


def test_an_incomplete_final_line_keeps_the_receipts_before_it(tmp_path):
    record = tmp_path / "exit.jsonl"
    append(record, {"event": "task_exit", "run_id": "r", "exit_code": 0})
    append(record, {"event": "notification", "run_id": "r", "outcome": "started"})
    with record.open("ab") as handle:
        handle.write(b'{"event":')
    assert state(record, "r") == "exited(0)"


def test_a_non_integer_exit_code_is_not_trusted(tmp_path):
    record = tmp_path / "exit.jsonl"
    append(record, {"event": "task_exit", "run_id": "r", "exit_code": True})
    assert state(record, "r") == "lost"


def test_a_notification_without_an_exit_is_unknown(tmp_path):
    record = tmp_path / "exit.jsonl"
    append(record, {"event": "notification", "run_id": "r", "outcome": "started"})
    assert state(record, "r") == "unknown"


def test_run_writes_its_record_and_reaches_a_grandchild(tmp_path, monkeypatch):
    """`ding run` exports the run id, and the wrapped job's own children inherit it."""
    monkeypatch.setattr(ding, "call_broker", lambda *args, **kwargs: {"ok": True, "outcome": "started"})
    observed = tmp_path / "child.json"
    record = tmp_path / "exit.jsonl"
    script = (
        "import json, os, subprocess, sys\n"
        "grandchild = subprocess.check_output([sys.executable, '-c', "
        "\"import os; print(os.environ['DKITCHEN_RUN_ID'])\"], text=True).strip()\n"
        "with open(sys.argv[1], 'w') as f: json.dump([os.environ['DKITCHEN_RUN_ID'], grandchild], f)\n"
    )
    before = dict(os.environ)
    assert ding.main(["run", THREAD, "--record", str(record), "--message", "done",
                      "--", sys.executable, "-c", script, str(observed)]) == 0
    run_id, echoed = json.loads(observed.read_text(encoding="utf-8"))
    assert run_id == echoed and run_id
    events = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["task_exit", "notification"]
    assert {event["run_id"] for event in events} == {run_id}
    assert dict(os.environ) == before


def test_run_keeps_the_jobs_own_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "call_broker", lambda *args, **kwargs: {"ok": True, "outcome": "started"})
    record = tmp_path / "exit.jsonl"
    code = ding.main(["run", THREAD, "--record", str(record), "--message", "done",
                      "--", sys.executable, "-c", "raise SystemExit(7)"])
    assert code == 7
    task_exit = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
    assert task_exit["exit_code"] == 7


def test_run_records_a_missing_command_as_127(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "call_broker", lambda *args, **kwargs: {"ok": True, "outcome": "started"})
    record = tmp_path / "exit.jsonl"
    assert ding.main(["run", THREAD, "--record", str(record), "--message", "done",
                      "--", "definitely-not-a-program"]) == 127
    task_exit = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
    assert task_exit["error"] and "No such file" in task_exit["error"] or task_exit["error"]


def test_run_writes_the_jobs_output_to_its_log(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "call_broker", lambda *args, **kwargs: {"ok": True, "outcome": "started"})
    record = tmp_path / "exit.jsonl"
    log = tmp_path / "job.log"
    assert ding.main(["run", THREAD, "--record", str(record), "--message", "done",
                      "--log", str(log), "--", sys.executable, "-c",
                      "import sys; print('out'); print('err', file=sys.stderr)"]) == 0
    # Both streams share one file; their order is the child's own, so compare as a set.
    assert sorted(log.read_text(encoding="utf-8").splitlines()) == ["err", "out"]


def test_run_registry_is_append_only_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "call_broker", lambda *args, **kwargs: {"ok": True, "outcome": "started"})
    registry = paths.state("runs.jsonl")
    for index in range(3):
        assert ding.main(["run", THREAD, "--record", str(tmp_path / f"{index}.jsonl"),
                          "--message", "done", "--", sys.executable, "-c", "pass"]) == 0
    rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert all(row["event"] == "run_start" and row["session_id"] == THREAD for row in rows)
    assert all(os.path.isabs(row["record"]) and os.path.isabs(row["cwd"]) for row in rows)


def test_concurrent_runs_keep_every_record_and_the_whole_message(tmp_path, monkeypatch):
    """Two processes appending at once must not interleave or truncate a record."""
    script = (
        "from dkitchen import ding\nimport sys\n"
        "ding.call_broker = lambda *a, **k: {'ok': True, 'outcome': 'started'}\n"
        "for index in range(3):\n"
        "    message = sys.argv[1] + ':' + str(index) + '\\n' + '界' * 4000\n"
        "    record = sys.argv[2] + sys.argv[1] + str(index) + '.jsonl'\n"
        "    assert ding.main(['run', %r, '--record', record,"
        " '--message', message, '--', sys.executable, '-c', 'pass']) == 0\n"
    ) % THREAD
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: subprocess.run(
            [sys.executable, "-c", script, name, str(tmp_path / "rec-")],
            capture_output=True, text=True, timeout=120), ["first", "second"]))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    rows = [json.loads(line) for line in paths.state("runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6
    assert len({row["run_id"] for row in rows}) == 6
    messages = []
    for name in ("first", "second"):
        for index in range(3):
            record = tmp_path / f"rec-{name}{index}.jsonl"
            messages.append(json.loads(record.read_text(encoding="utf-8").splitlines()[0])["message"])
    assert sorted(messages) == sorted(f"{name}:{index}\n" + "界" * 4000
                                      for name in ("first", "second") for index in range(3))


def test_a_second_wrapper_for_one_run_is_refused(tmp_path):
    """The run lock is what tells `board` a wrapper is alive, so two may not share it."""
    run_id = "shared"
    lock = paths.state("run-locks", f"{run_id}.lock")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from dkitchen import locks\n"
        "with locks.exclusive(Path(%r), blocking=True):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.read()\n"
    ) % (str(Path(__file__).resolve().parents[1] / "src"), str(lock))
    child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(locks.LockBusy):
            with locks.exclusive(lock):
                pass
        assert locks.is_locked(lock) is True
    finally:
        child.kill()
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
    assert locks.is_locked(lock) is False
