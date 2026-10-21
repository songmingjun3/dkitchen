"""Locks, state paths, the session registry, and the config vocabulary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from dkitchen import config, launcher, locks, paths, registry


def test_state_root_follows_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "explicit"))
    assert paths.state("sent.jsonl") == tmp_path / "explicit" / "sent.jsonl"


def test_state_root_falls_back_to_the_platform_convention(monkeypatch, tmp_path):
    monkeypatch.delenv("DKITCHEN_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = paths.state_root()
    if sys.platform == "win32":
        assert root == tmp_path / "local" / "dkitchen"
    else:
        assert root == Path.home() / ".local" / "state" / "ding"


def test_exclusive_lock_refuses_a_second_holder(tmp_path):
    path = tmp_path / "one.lock"
    with locks.exclusive(path):
        with pytest.raises(locks.LockBusy):
            with locks.exclusive(path):
                pass
    with locks.exclusive(path):
        pass


def test_lock_is_released_when_the_holder_exits(tmp_path):
    path = tmp_path / "two.lock"
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from dkitchen import locks\n"
        "import time\n"
        "with locks.exclusive(Path(%r)):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    ) % (str(Path(__file__).resolve().parents[1] / "src"), str(path))
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert locks.is_locked(path) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and locks.is_locked(path):
        time.sleep(0.05)
    assert locks.is_locked(path) is False


def test_registry_round_trip_keeps_issue_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    first = registry.Record(id="a", name="one", station="executor", cwd=str(tmp_path),
                            created_ns=1, issue=42, repo="o/r")
    registry.put(first)
    again = registry.Record(id="a", name="one-renamed", station="executor", cwd=str(tmp_path),
                            created_ns=2)
    registry.put(again)
    stored = registry.get("a")
    assert stored.name == "one-renamed"
    assert stored.issue == 42 and stored.repo == "o/r"
    assert [row.id for row in registry.find_by_issue("o/r", 42)] == ["a"]


def test_registry_rename_and_close(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    registry.put(registry.Record(id="b", name="old", station="executor", cwd=str(tmp_path),
                                 created_ns=1))
    assert registry.rename("b", "new").name == "new"
    registry.mark_closed("b")
    assert registry.get("b").closed is True
    assert registry.rename("missing", "x") is None


def test_registry_survives_a_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    paths.state("sessions.json").write_text("{not json", encoding="utf-8")
    assert registry.load() == {}


def write_config(root: Path, text: str) -> None:
    (root / ".dkitchen.toml").write_text(text, encoding="utf-8")


def test_defaults_name_the_harness_vocabulary(tmp_path):
    settings = config.load(tmp_path)
    assert settings.sessions.owner_line == "DSH owner"
    assert settings.sessions.session_line == "DSH session"
    assert settings.kitchen.stations == config.STATIONS
    assert settings.harness.profile == "dkitchen"
    assert settings.route("executor").provider == settings.harness.default_provider


def test_legacy_signature_lines_still_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    settings = config.load(tmp_path)
    assert "Codex session" in settings.sessions.session_patterns()
    assert "Codex owner" in settings.sessions.owner_patterns()


def test_station_selection_and_routes_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    write_config(tmp_path, """
[kitchen]
stations = ["orchestrator", "executor"]

[harness]
profile = "lab"
default_provider = "docode-claude"
default_model = "claude-sonnet-5"

[models.executor]
provider = "agentroute-codex"
model = "gpt-6-astra"
effort = "high"
""")
    settings = config.load(tmp_path)
    assert settings.kitchen.stations == ("orchestrator", "executor")
    assert settings.kitchen.includes("designer") is False
    assert settings.harness.profile == "lab"
    assert settings.route("executor").model == "gpt-6-astra"
    assert settings.route("executor").effort == "high"
    # A station without its own table inherits the harness default.
    assert settings.route("orchestrator").provider == "docode-claude"


def test_unknown_station_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    write_config(tmp_path, '[kitchen]\nstations = ["chef"]\n')
    with pytest.raises(ValueError) as error:
        config.load(tmp_path)
    assert "unknown stations" in str(error.value)


def test_repeated_station_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    write_config(tmp_path, '[kitchen]\nstations = ["executor", "executor"]\n')
    with pytest.raises(ValueError) as error:
        config.load(tmp_path)
    assert "twice" in str(error.value)


def test_approvals_must_be_known(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    write_config(tmp_path, '[harness]\napprovals = "maybe"\n')
    with pytest.raises(ValueError):
        config.load(tmp_path)


def test_unknown_model_station_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    write_config(tmp_path, '[models.chef]\nprovider = "x"\n')
    with pytest.raises(ValueError) as error:
        config.load(tmp_path)
    assert "unknown stations" in str(error.value)


def test_signature_line_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repository_root", lambda cwd=None: tmp_path)
    write_config(tmp_path, '[sessions]\nowner_line = "Kitchen owner"\nsession_line = "Kitchen session"\n')
    settings = config.load(tmp_path)
    assert settings.sessions.owner_patterns()[0] == "Kitchen owner"
    assert settings.sessions.session_patterns()[0] == "Kitchen session"


def test_launcher_jobs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    job = launcher.Job(name="train", run_id="r1", pid=1234, record=tmp_path / "r.jsonl",
                       log=tmp_path / "r.log", started_ns=1_700_000_000_000_000_000,
                       command=["python", "train.py"], backend="process", session="sid")
    launcher.record_job(job)
    found = launcher.jobs()["train"]
    assert found.pid == 1234 and found.session == "sid" and found.backend == "process"


def test_job_state_reads_the_record(tmp_path, monkeypatch):
    from dkitchen import ding

    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    record = tmp_path / "exit.jsonl"
    record.write_text("", encoding="utf-8")
    # No lock holder and no record rows: the wrapper is not running.
    assert ding.run_state_from_record(record, "run-1", None) == "lost"
    record.write_text(json.dumps({"event": "task_exit", "run_id": "run-1", "exit_code": 0}) + "\n"
                      + json.dumps({"event": "notification", "run_id": "run-1", "outcome": "started"}) + "\n",
                      encoding="utf-8")
    assert ding.run_state_from_record(record, "run-1", None) == "exited(0)"
    record.write_text(json.dumps({"event": "task_exit", "run_id": "run-1", "exit_code": 3}) + "\n"
                      + json.dumps({"event": "notification", "run_id": "run-1", "outcome": "failed"}) + "\n",
                      encoding="utf-8")
    assert ding.run_state_from_record(record, "run-1", None) == "notify_failed"
