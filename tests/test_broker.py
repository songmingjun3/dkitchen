"""The broker: request handling, queueing, recovery, and the CLI around it.

The broker is driven in-process against the fake harness, so each test owns one ACP process
and one queue. `broker.ensure` is covered separately, without starting a detached process.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from dkitchen import acp, broker, registry


@pytest.fixture
def served(fake_launcher, tmp_path):
    """A broker in a thread, listening on the isolated state root's endpoint."""
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
    else:  # pragma: no cover - a failed start is a real failure
        pytest.fail("the broker did not start listening")
    try:
        yield instance
    finally:
        instance.stop()
        try:
            broker.request("shutdown", timeout=5)
        except broker.BrokerDown:
            pass
        thread.join(timeout=15)


def create(broker_instance, tmp_path, **overrides):
    payload = {"action": "create", "cwd": str(tmp_path), "name": "probe", "station": "executor",
               "issue": None, "repo": None, "route": {}, "message": None}
    payload.update(overrides)
    return broker_instance.handle(payload)


def test_ping_reports_the_process(served):
    reply = broker.request("ping", timeout=5)
    assert reply["ok"] is True and isinstance(reply["pid"], int)


def test_create_registers_the_session_and_applies_the_route(served, tmp_path):
    reply = create(served, tmp_path, route={"provider": "agentroute-codex", "model": "gpt-6-astra"})
    assert reply["ok"] is True
    session_id = reply["session"]
    assert reply["route"]["model"] == "gpt-6-astra"
    stored = registry.get(session_id)
    assert stored is not None and stored.station == "executor" and stored.model == "gpt-6-astra"
    # The fake echoes the selected route back in its option state.
    assert acp.current_value(reply["options"], acp.MODEL_OPTION) == acp.encode_route(
        "agentroute-codex", "gpt-6-astra")


def test_an_unavailable_route_is_recorded_not_fatal(served, tmp_path, monkeypatch):
    """A route nobody publishes is reported after the settle window, not treated as fatal."""
    monkeypatch.setattr(broker, "ROUTE_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(broker, "ROUTE_SETTLE_INTERVAL", 0.1)
    reply = create(served, tmp_path, route={"provider": "nobody", "model": "nothing"})
    assert reply["ok"] is True
    assert reply["route_selected"] is False
    journals = broker.journal().read_text(encoding="utf-8")
    assert "route_unavailable" in journals
    assert "route_waiting" in journals, "it should have retried before giving up"


def test_a_route_the_profile_publishes_late_is_still_selected(served, tmp_path, monkeypatch):
    """A plugin that registers its routes after startup must not lose the station's route.

    `session/new` fixes the option list, so the only way to see a late route is to drop the
    session and open another one. The fake publishes `late/nothing-else` only on its second
    `session/new`, which is what a runtime-registering plugin looks like from here.
    """
    monkeypatch.setattr(broker, "ROUTE_SETTLE_SECONDS", 5.0)
    monkeypatch.setattr(broker, "ROUTE_SETTLE_INTERVAL", 0.05)
    monkeypatch.setenv("FAKE_ACP_LATE_ROUTE", "late/nothing-else")
    reply = create(served, tmp_path, route={"provider": "late", "model": "nothing-else"})
    assert reply["ok"] is True
    assert reply["route_selected"] is True
    assert acp.current_value(reply["options"], acp.MODEL_OPTION) == acp.encode_route(
        "late", "nothing-else")


def test_send_starts_a_turn_and_status_follows_it(served, tmp_path):
    session_id = create(served, tmp_path)["session"]
    reply = served.handle({"action": "send", "session": session_id, "message": "hello", "cwd": str(tmp_path)})
    assert reply == {"ok": True, "outcome": "started", "busy": False}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = served.handle({"action": "status", "session": session_id})
        if status.get("stop_reason") == "end_turn":
            break
        time.sleep(0.05)
    assert status["state"] == "idle" and status["stop_reason"] == "end_turn"


def test_a_second_message_while_busy_is_queued(served, tmp_path, monkeypatch):
    session_id = create(served, tmp_path)["session"]
    monkeypatch.setenv("FAKE_ACP_TURN_DELAY", "1.0")
    first = served.handle({"action": "send", "session": session_id, "message": "one", "cwd": str(tmp_path)})
    assert first["outcome"] == "started"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if served.handle({"action": "status", "session": session_id}).get("in_flight"):
            break
        time.sleep(0.02)
    second = served.handle({"action": "send", "session": session_id, "message": "two", "cwd": str(tmp_path)})
    assert second["outcome"] == "queued_behind_active" and second["busy"] is True


def test_unknown_action_is_refused(served):
    reply = served.handle({"action": "nonsense"})
    assert reply["ok"] is False and "unknown action" in reply["error"]


def test_missing_field_is_refused(served):
    reply = served.handle({"action": "send"})
    assert reply["ok"] is False and "missing field" in reply["error"]


def test_close_forgets_the_session(served, tmp_path):
    session_id = create(served, tmp_path)["session"]
    assert served.handle({"action": "close", "session": session_id}) == {"ok": True}
    assert session_id not in served.sessions
    assert registry.get(session_id).closed is True


def test_status_of_an_unloaded_session_is_not_loaded(served):
    assert served.handle({"action": "status", "session": "nope"}) == {"ok": True, "state": "notLoaded"}


def test_models_lists_routes_and_efforts(served, tmp_path):
    reply = served.handle({"action": "models", "cwd": str(tmp_path)})
    assert reply["ok"] is True
    assert {row["model"] for row in reply["models"]} >= {"gpt-6-astra", "claude-sonnet-5"}
    assert "max" in {row["value"] for row in reply["efforts"]}


def test_a_lost_connection_is_reported_and_the_session_reopened(served, tmp_path):
    session_id = create(served, tmp_path)["session"]
    # Kill the harness process under the broker: the next message must be delivered after the
    # broker notices and reopens the session, not silently dropped.
    served.connection.process.kill()
    served.connection.process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and served.connection.alive:
        time.sleep(0.02)
    assert served.connection.alive is False
    reply = served.handle({"action": "send", "session": session_id, "message": "after crash",
                           "cwd": str(tmp_path)})
    assert reply["ok"] is True
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = served.handle({"action": "status", "session": session_id})
        if status.get("stop_reason") == "end_turn":
            break
        time.sleep(0.05)
    journal = broker.journal().read_text(encoding="utf-8")
    assert "connection_lost" in journal
    assert "recovered" in journal
    assert status["stop_reason"] == "end_turn"


def test_shutdown_action_stops_the_broker(served):
    assert served.handle({"action": "shutdown"}) == {"ok": True}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not served.stopping.is_set():
        time.sleep(0.02)
    assert served.stopping.is_set()


PERMISSION = {"toolCall": {"toolCallId": "call-1", "title": "run a command"},
              "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                          {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}


@pytest.fixture
def asking():
    """A broker whose permission prompts wait for `ding approve`; no listener is started."""
    return broker.Broker({"default": "dkitchen", "approvals": "ask"})


def answer_later(instance, session, decision):
    """Answer the pending prompt from another thread, as `ding approve` does."""
    records = {}

    def wait_and_answer():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with instance._permission_lock:
                if session in instance._pending_permissions:
                    break
            time.sleep(0.02)
        records["reply"] = instance.handle({"action": "approve", "session": session,
                                            "decision": decision})

    thread = threading.Thread(target=wait_and_answer, daemon=True)
    thread.start()
    return thread, records


def test_an_approval_answers_the_pending_prompt(asking):
    thread, records = answer_later(asking, "s1", "allow")
    outcome = asking._on_permission("s1", dict(PERMISSION))
    thread.join(timeout=15)
    assert records["reply"] == {"ok": True}
    assert outcome == {"outcome": "selected", "optionId": "allow-once"}


def test_a_rejection_selects_the_reject_option(asking):
    """`ding approve --decision reject` must refuse the call, not merely record the wish."""
    thread, records = answer_later(asking, "s1", "reject")
    outcome = asking._on_permission("s1", dict(PERMISSION))
    thread.join(timeout=15)
    assert records["reply"] == {"ok": True}
    assert outcome == {"outcome": "selected", "optionId": "reject-once"}


def test_the_auto_policy_answers_at_once_without_waiting(served):
    outcome = served._on_permission("s1", dict(PERMISSION))
    assert outcome == {"outcome": "selected", "optionId": "allow-once"}
    assert served._pending_permissions == {}


def test_approving_without_a_pending_prompt_is_refused(asking):
    reply = asking.handle({"action": "approve", "session": "s1", "decision": "allow"})
    assert reply == {"ok": False, "error": "no permission request is waiting"}


def test_a_prompt_without_a_usable_option_is_cancelled(asking):
    """Nothing to select means nothing to wait for: answer now, do not block for an hour."""
    bare = {"toolCall": {"title": "run a command"}, "options": []}
    started = time.monotonic()
    assert asking._on_permission("s1", bare) == {"outcome": "cancelled"}
    assert time.monotonic() - started < 5
    assert asking._pending_permissions == {}


def test_an_unanswered_prompt_is_cancelled_when_it_times_out(asking, monkeypatch):
    """A forgotten `ding approve` must never become a silent yes."""
    monkeypatch.setattr(broker, "PERMISSION_TIMEOUT_SECONDS", 0.2)
    assert asking._on_permission("s1", dict(PERMISSION)) == {"outcome": "cancelled"}
    assert asking._pending_permissions == {}


def test_endpoint_and_pid_file_live_under_the_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    endpoint = broker.endpoint()
    assert broker.endpoint() == endpoint
    if endpoint.startswith("\\\\"):
        assert "dkitchen-" in endpoint
    else:
        assert endpoint.endswith("broker.sock")
    assert broker.pid_file() == tmp_path / "broker.pid"


def test_no_broker_means_broker_down(monkeypatch, tmp_path):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    if broker.running():
        pytest.skip("a real broker is running for this user")
    with pytest.raises(broker.BrokerDown):
        broker.request("ping", timeout=1)
