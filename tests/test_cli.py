"""The `ding` command line: targets, delivery words, board columns, and refusals.

Each test drives `ding.main` against the fake harness through a real broker started inside
the test's own state root, so nothing here reaches a user session or the network.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from dkitchen import broker, ding, registry


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A broker in a thread plus a repository with a kitchen config."""
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(ding.config, "repository_root", lambda cwd=None: tmp_path / "repo")
    instance = broker.Broker({"default": "dkitchen", "approvals": "auto"})
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if broker.request("ping", timeout=1).get("ok"):
                break
        except broker.BrokerDown:
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("the broker did not start")
    try:
        yield tmp_path / "repo"
    finally:
        broker.request("shutdown", timeout=5)
        thread.join(timeout=15)


def run(argv):
    return ding.main(argv)


def test_new_prints_one_session_id(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--station", "executor",
                "--message", "hello"]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1
    session_id = printed[0]
    assert registry.get(session_id) is not None


def test_new_fills_the_session_name_template_for_a_ticket(live, capsys, monkeypatch):
    monkeypatch.setattr(ding.config.Config, "repository", lambda self: "owner/repo")
    assert run(["new", "--cwd", str(live), "--name", "Ablation", "--issue", "57",
                "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    assert registry.get(session_id).name == "【#57】Ablation"
    assert registry.get(session_id).issue == 57


def test_new_refuses_a_station_the_kitchen_does_not_run(live, monkeypatch, capsys):
    (live / ".dkitchen.toml").write_text('[kitchen]\nstations = ["orchestrator", "executor"]\n',
                                        encoding="utf-8")
    assert run(["new", "--cwd", str(live), "--name", "probe", "--station", "verifier",
                "--message", "hello"]) == 1
    assert "not one of this kitchen's stations" in capsys.readouterr().err


def test_new_requires_an_existing_directory(tmp_path, capsys):
    assert run(["new", "--cwd", str(tmp_path / "missing"), "--name", "x", "--message", "hi"]) == 1
    assert "existing directory" in capsys.readouterr().err


def test_new_refuses_an_empty_message(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "x", "--message", "   "]) == 1
    assert "must not be empty" in capsys.readouterr().err


def test_send_reports_started_and_records_it(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--message", "first"]) == 0
    session_id = capsys.readouterr().out.strip()
    # Wait for the first turn to settle, so the second message is a fresh start, not a queue.
    deadline = time.monotonic() + 20
    for _ in range(200):
        capsys.readouterr()
        run(["status", session_id])
        if json.loads(capsys.readouterr().out).get("type") == "idle":
            break
        time.sleep(0.1)
    assert run(["send", session_id, "--from", "Orchestrator", "--message", "second"]) == 0
    assert "started" in capsys.readouterr().err
    rows = [json.loads(line) for line in (Path(ding.paths.state("sent.jsonl"))).read_text(
        encoding="utf-8").splitlines()]
    assert rows[0]["event"] == "send_intent"
    assert rows[-1]["event"] == "send_result" and rows[-1]["outcome"] == "started"
    assert rows[-1]["message"].startswith("[from Orchestrator]")


def test_send_reports_a_queued_message_behind_active_work(live, capsys, monkeypatch):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--message", "first"]) == 0
    session_id = capsys.readouterr().out.strip()
    monkeypatch.setenv("FAKE_ACP_TURN_DELAY", "2")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if live.exists():
            break
        time.sleep(0.02)
    # The first turn is still running, so this one is queued and says so.
    run(["send", session_id, "--message", "second"])
    assert "queued_behind_active" in capsys.readouterr().err


def test_status_reports_the_harness_state(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    deadline = time.monotonic() + 15
    payload = {}
    for _ in range(60):
        capsys.readouterr()
        run(["status", session_id])
        payload = json.loads(capsys.readouterr().out)
        if payload.get("stopReason") == "end_turn":
            break
        time.sleep(0.1)
    assert payload["type"] == "idle" and payload["stopReason"] == "end_turn"


def test_info_reports_name_cwd_station_and_route(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--station", "executor",
                "--provider", "agentroute-codex", "--model", "gpt-6-astra",
                "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    run(["info", session_id])
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "probe"
    assert payload["station"] == "executor"
    assert payload["route"]["model"] == "gpt-6-astra"
    assert Path(payload["cwd"]).name == live.name


def test_approve_refuses_when_no_prompt_is_waiting(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    assert run(["approve", session_id, "--decision", "reject"]) == 1
    assert "no permission request is waiting" in capsys.readouterr().err


@pytest.fixture
def asking_live(tmp_path, monkeypatch):
    """A broker that waits for `ding approve`, with the fake armed to ask during a turn."""
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(ding.config, "repository_root", lambda cwd=None: tmp_path / "repo")
    monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
    instance = broker.Broker({"default": "dkitchen", "approvals": "ask"})
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if broker.request("ping", timeout=1).get("ok"):
                break
        except broker.BrokerDown:
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("the broker did not start")
    try:
        yield tmp_path / "repo", instance
    finally:
        try:
            broker.request("shutdown", timeout=5)
        except broker.BrokerDown:
            pass
        thread.join(timeout=15)


def test_approve_reject_denies_the_waiting_prompt(asking_live, capsys):
    """`ding approve --decision reject` must reach the harness as the reject option."""
    repo, instance = asking_live
    assert run(["new", "--cwd", str(repo), "--name", "probe", "--message", "approve-me"]) == 0
    session_id = capsys.readouterr().out.strip()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with instance._permission_lock:
            if session_id in instance._pending_permissions:
                break
        time.sleep(0.05)
    else:
        pytest.fail("the fake never asked for a permission")
    assert run(["approve", session_id, "--decision", "reject"]) == 0
    assert "reject sent for" in capsys.readouterr().out
    deadline = time.monotonic() + 20
    journal = ""
    while time.monotonic() < deadline:
        journal = broker.journal().read_text(encoding="utf-8")
        if '"permission_decision"' in journal and '"reject"' in journal:
            break
        time.sleep(0.05)
    assert '"permission_decision"' in journal
    assert '"decision": "reject"' in journal


def test_rename_changes_the_local_name(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "old", "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    assert run(["rename", session_id, "--name", "new name"]) == 0
    assert registry.get(session_id).name == "new name"
    assert registry.rename("missing", "x") is None


def test_rename_refuses_a_session_ding_did_not_create(tmp_path, monkeypatch, capsys):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(ding.config, "repository_root", lambda cwd=None: tmp_path / "repo")
    assert run(["rename", "11111111-2222-4333-8444-555555555555", "--name", "x"]) == 1
    assert "not created by ding" in capsys.readouterr().err


def test_issue_targets_resolve_through_the_local_record(live, capsys, monkeypatch):
    monkeypatch.setattr(ding.config.Config, "repository", lambda self: "owner/repo")
    monkeypatch.setattr(ding, "gh_available", lambda: False)
    assert run(["new", "--cwd", str(live), "--name", "Ablation", "--issue", "42",
                "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    assert ding.resolve_target("#42", None, ding.settings(live)) == session_id


def test_normalize_target_accepts_urls_and_repository_forms():
    assert ding.normalize_target("https://github.com/o/r/issues/42", None) == "o/r#42"
    assert ding.normalize_target("https://github.com/o/r/pull/42#issuecomment-1", None) == "o/r#42"
    assert ding.normalize_target("o/r#42", None) == "o/r#42"
    with pytest.raises(ValueError):
        ding.normalize_target("https://example.com/o/r/issues/42", None)


def test_board_prints_the_session_table(live, capsys):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--message", "hello"]) == 0
    capsys.readouterr()
    deadline = time.monotonic() + 15
    for _ in range(60):
        assert run(["board", "--no-start"]) == 0
        output = capsys.readouterr().out
        if "echo: hello" in output:
            break
        time.sleep(0.1)
    header = output.splitlines()[0]
    assert header.startswith("NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN")
    assert "probe" in output


def test_board_shows_a_ticket_number(live, capsys, monkeypatch):
    monkeypatch.setattr(ding.config.Config, "repository", lambda self: "owner/repo")
    monkeypatch.setattr(ding, "gh_available", lambda: False)
    assert run(["new", "--cwd", str(live), "--name", "Ablation", "--issue", "42",
                "--message", "hello"]) == 0
    capsys.readouterr()
    assert run(["board", "--no-start"]) == 0
    output = capsys.readouterr().out
    assert "#42" in output


def test_config_show_prints_the_effective_settings(live, capsys):
    assert run(["config", "show"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kitchen"]["stations"] == ["orchestrator", "designer", "verifier", "executor"]
    assert payload["harness"]["profile"] == "dkitchen"
    assert payload["sessions"]["owner_line"] == "DSH owner"


def test_config_enable_and_disable_write_the_file(live, capsys):
    assert run(["config", "disable", "designer"]) == 0
    text = (live / ".dkitchen.toml").read_text(encoding="utf-8")
    assert "designer" not in text.split("stations")[1]
    assert run(["config", "enable", "designer"]) == 0
    reread = ding.settings(live)
    assert "designer" in reread.kitchen.stations


def test_config_cannot_disable_the_last_station(live, capsys):
    assert run(["config", "disable", "orchestrator"]) == 0
    assert run(["config", "disable", "designer"]) == 0
    assert run(["config", "disable", "verifier"]) == 0
    assert run(["config", "disable", "executor"]) == 1
    assert "at least one station" in capsys.readouterr().err


def test_config_set_round_trips_a_value(live, capsys):
    assert run(["config", "set", "harness.profile", "lab"]) == 0
    assert ding.settings(live).harness.profile == "lab"
    assert run(["config", "set", "models.verifier.effort", "low"]) == 0
    assert ding.settings(live).route("verifier").effort == "low"


def test_config_models_lists_routes(live, capsys):
    assert run(["config", "models"]) == 0
    output = capsys.readouterr().out
    assert "agentroute-codex/gpt-6-astra" in output
    assert output.strip().splitlines()[-1].startswith("efforts")


def test_config_rejects_an_unknown_station(live, capsys):
    assert run(["config", "enable", "chef"]) == 1
    assert "unknown station" in capsys.readouterr().err


def test_down_reports_when_nothing_runs(live, capsys, monkeypatch):
    monkeypatch.setattr(broker, "running", lambda: False)
    assert run(["down"]) == 0
    assert "no broker" in capsys.readouterr().err


def test_executor_comment_heads_reads_a_status_block(live, monkeypatch):
    config_ = ding.settings(live)
    session_id = "11111111-2222-4333-8444-555555555555"
    body = ("Status: in progress\nWaiting on: Orchestrator\nKey numbers: 3/10\nNext: continue\n"
            f"DSH session: {session_id}")

    def fake_gh_json(arguments, cwd=None):
        if arguments[:2] == ["issue", "view"]:
            return {"comments": [{"body": body, "createdAt": "2026-01-01T00:00:00Z"}]}
        return {"nameWithOwner": "owner/repo"}

    monkeypatch.setattr(ding, "gh_available", lambda: True)
    monkeypatch.setattr(ding, "gh_json", fake_gh_json)
    heads = ding.executor_comment_heads({session_id: {42}}, config_.status.fields, config_)
    assert heads[session_id].startswith("Status: in progress")
    assert "Waiting on: Orchestrator" in heads[session_id]


def test_role_reads_both_signature_spellings(live):
    from dkitchen import watch

    config_ = ding.settings(live)
    assert watch.role("DSH session: x\nStatus: done", config_) == "Executor"
    assert watch.role("Codex session: x\nStatus: done", config_) == "Executor"
    assert watch.role("Orchestrator: merged", config_) == "Orchestrator"
    assert watch.role("plain words", config_) == "User"


def test_launch_and_logs_and_stop_round_trip(live, capsys, monkeypatch):
    assert run(["new", "--cwd", str(live), "--name", "probe", "--message", "hello"]) == 0
    session_id = capsys.readouterr().out.strip()
    monkeypatch.setattr(ding.launcher, "tmux_available", lambda: False)
    assert run(["launch", "job1", "--session", session_id,
                "--", __import__("sys").executable, "-c",
                "import time; print('hi', flush=True); time.sleep(0.2)"]) == 0
    assert "created job1" in capsys.readouterr().out
    deadline = time.monotonic() + 20
    state = ""
    while time.monotonic() < deadline:
        state = ding.launcher.job_state(ding.launcher.jobs()["job1"])
        if state.startswith("exited("):
            break
        time.sleep(0.1)
    assert state.startswith("exited(")
    assert run(["logs", "job1"]) == 0
    assert "hi" in capsys.readouterr().out


def test_launch_requires_a_calling_session(live, capsys, monkeypatch):
    monkeypatch.delenv("DSH_SESSION_ID", raising=False)
    monkeypatch.setattr(ding.launcher, "jobs", lambda: {})
    assert run(["launch", "job", "--", "echo", "hi"]) == 1
    assert "calling session" in capsys.readouterr().err


def test_launch_rejects_a_name_already_running(live, capsys, monkeypatch):
    from dkitchen import launcher

    monkeypatch.setattr(launcher, "jobs", lambda: {"job": launcher.Job(
        name="job", run_id="r", pid=1, record=Path("r.jsonl"), log=None, started_ns=1,
        command=["x"], backend="process")})
    monkeypatch.setattr(launcher, "job_state", lambda job, kill_check=True: "running")
    assert run(["launch", "job", "--session", "sid", "--", "echo", "hi"]) == 1
    assert "already running" in capsys.readouterr().err


def test_show_unwrapped_is_silent_without_findings(capsys):
    ding.show_unwrapped([])
    assert capsys.readouterr().out == ""
