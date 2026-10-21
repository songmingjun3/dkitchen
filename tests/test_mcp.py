"""The MCP server: handshake, tool listing, and the forwarding of each tool to `ding`.

The server is driven in-process over pipes, so nothing here starts the real harness or
reaches a real session: `tests/conftest.py` points the CLI at the fake one, and every
`_run` is patched to record the command it would have executed.
"""

from __future__ import annotations

import io
import json

import pytest

from dkitchen import mcp


def exchange(*messages):
    """Feed JSON-RPC messages through the server and return the parsed replies."""
    stdin = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")
    stdout = io.StringIO()
    mcp.serve(stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_initialize_answers_with_a_protocol_and_a_server_name():
    replies = exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    result = replies[0]["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "dkitchen"
    assert result["serverInfo"]["version"], "serverInfo needs the package version"
    assert "tools" in result["capabilities"]


def test_tools_list_names_every_tool_with_a_schema():
    replies = exchange({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in replies[0]["result"]["tools"]}
    assert set(tools) == set(mcp.TOOLS)
    for name, tool in tools.items():
        assert tool["description"], f"{name} needs a description"
        assert tool["inputSchema"]["type"] == "object"


def test_the_session_tools_the_plugin_promises_are_all_present():
    """These are the three groups the plugin advertises: sessions, tickets, and long jobs."""
    for name in ("new_session", "send", "read", "board", "info", "status",
                 "relay", "merge", "log", "launch", "logs", "stop"):
        assert name in mcp.TOOLS, f"{name} is missing from the tool list"


def test_a_notification_gets_no_reply():
    replies = exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert replies == []


def test_an_unknown_method_is_a_jsonrpc_error():
    replies = exchange({"jsonrpc": "2.0", "id": 9, "method": "tools/nope"})
    assert replies[0]["error"]["code"] == -32601


def test_a_malformed_line_does_not_kill_the_server():
    stdin = io.StringIO('{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\nnot json\n')
    stdout = io.StringIO()
    mcp.serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines[0]["result"]["tools"]          # the good message still answered
    assert lines[1]["error"]["code"] == -32700  # and the bad one reported


@pytest.fixture
def recorded(monkeypatch):
    """Record the `ding` command each tool would run, instead of running it."""
    seen = {}

    def fake_run(args, *, timeout=120.0):
        seen["args"] = list(args)
        seen["timeout"] = timeout
        return '{"ok": true}\n'

    monkeypatch.setattr(mcp, "_run", fake_run)
    return seen


def test_new_session_forwards_the_station_and_message(recorded):
    mcp.call_tool("new_session", {"cwd": "/work/repo", "name": "probe", "station": "executor",
                                  "message": "hello"})
    args = recorded["args"]
    assert args[0] == "new"
    assert "--cwd" in args and "/work/repo" in args
    assert "--station" in args and "executor" in args
    assert args[-2:] == ["--message", "hello"]


def test_send_takes_a_target_or_an_issue(recorded):
    mcp.call_tool("send", {"target": "#42", "message": "go"})
    assert recorded["args"][:2] == ["send", "#42"]
    mcp.call_tool("send", {"issue": 42, "message": "go"})
    assert recorded["args"][:3] == ["send", "--issue", "42"]


def test_a_session_tool_without_a_target_is_refused():
    with pytest.raises(mcp.ToolError, match="target"):
        mcp.call_tool("read", {})


def test_approve_carries_the_decision(recorded):
    mcp.call_tool("approve", {"target": "probe", "decision": "reject"})
    assert recorded["args"] == ["approve", "probe", "--decision", "reject"]


def test_log_needs_at_least_one_item(recorded):
    with pytest.raises(mcp.ToolError, match="item"):
        mcp.call_tool("log", {})


def test_launch_passes_the_command_after_a_double_dash(recorded):
    mcp.call_tool("launch", {"name": "eval17", "command": ["python", "eval.py"]})
    args = recorded["args"]
    assert args[0] == "launch" and args[1] == "eval17"
    assert args[-2:] == ["python", "eval.py"]
    assert "--" in args


def test_an_unknown_tool_names_the_ones_that_exist():
    with pytest.raises(mcp.ToolError, match="unknown tool"):
        mcp.call_tool("nope", {})


def test_a_failed_tool_returns_is_error_and_keeps_the_turn_alive(monkeypatch):
    """A tool failure is a result the model reads, not a transport error."""
    def failing(args, *, timeout=120.0):
        raise mcp.ToolError("no permission request is waiting")

    monkeypatch.setattr(mcp, "_run", failing)
    replies = exchange({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "approve", "arguments": {"target": "probe"}}})
    result = replies[0]["result"]
    assert result["isError"] is True
    assert "no permission request is waiting" in result["content"][0]["text"]


def test_a_successful_tool_returns_json_text(monkeypatch):
    monkeypatch.setattr(mcp, "_run", lambda args, **kw: "probe-id\n")
    replies = exchange({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "new_session",
                                   "arguments": {"name": "probe", "message": "hi"}}})
    text = replies[0]["result"]["content"][0]["text"]
    assert json.loads(text) == {"session": "probe-id"}
    assert "isError" not in replies[0]["result"]


def test_run_decodes_output_as_utf8_not_the_console_codepage(monkeypatch):
    """A Windows console defaults to GBK; the CLI writes UTF-8 and must be read as such.

    Without this, the reader thread raises UnicodeDecodeError and the tool reports an
    empty result instead of the text it actually got.
    """
    seen = {}

    class Finished:
        returncode = 0
        stdout = "界" * 3
        stderr = ""

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        return Finished()

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)
    assert mcp._run(["board"]) == "界" * 3
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
