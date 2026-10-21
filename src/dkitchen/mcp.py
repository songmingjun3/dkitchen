"""An MCP server over stdio, so a DeepSeek Harness session can drive dkitchen.

The harness already has an MCP client (`@deepseek-ai/dsh-mcp-client`): it starts this
process, asks for the tool list, and registers what it gets on `ctx.tools`. Tools appear
to the model as `mcp__dkitchen__<name>`, which is why nothing here needs to know about
ACP, profiles, or the harness's own protocol.

The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout — the same shape
`acp.py` speaks — so it needs no dependency beyond the standard library. Every tool
forwards to the `ding` command line rather than reimplementing it, so the CLI and the
plugin can never disagree about what a session is.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

#: The protocol revision this server implements. The client picks the newest revision it
#: supports and this one is accepted as a fallback, so a harness upgrade does not need a
#: matching release here.
PROTOCOL_VERSION = "2025-06-18"

SERVER_NAME = "dkitchen"


def _version() -> str:
    """The installed package version, so `serverInfo` never drifts from the release."""
    try:
        from importlib.metadata import version
        return version("dkitchen")
    except Exception:  # a source checkout with no installed metadata
        return "0"


class ToolError(Exception):
    """A tool call that failed in a way the model should see and can act on."""


def _run(args: list[str], *, timeout: float = 120.0) -> str:
    """Run `ding` with `args` and return its stdout.

    The executable is this interpreter running the CLI module, not a `ding` on PATH: the
    harness starts this process from whatever environment it has, and a virtualenv's
    console script is not always on that PATH.
    """
    command = [sys.executable, "-m", "dkitchen.ding", *args]
    try:
        # The CLI writes UTF-8; a Windows console's GBK default would raise inside the
        # reader thread and hide the real exit status.
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        raise ToolError(f"ding {' '.join(args)} did not finish within {timeout:.0f}s")
    except OSError as error:
        raise ToolError(f"cannot run ding: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(detail or f"ding exited {result.returncode}")
    return result.stdout


def _target(target: str | None, issue: int | None) -> list[str]:
    """The target argument every session command takes, as one positional or `--issue`."""
    if issue is not None:
        return ["--issue", str(issue)]
    if not target:
        raise ToolError("give either target (session id, name, or '#42') or issue")
    return [target]


# -- tools -----------------------------------------------------------------------------

def new_session(args: dict) -> Any:
    command = ["new", "--cwd", str(args.get("cwd") or os.getcwd()),
               "--name", str(args["name"])]
    if args.get("issue") is not None:
        command += ["--issue", str(args["issue"])]
    if args.get("station"):
        command += ["--station", str(args["station"])]
    if args.get("provider"):
        command += ["--provider", str(args["provider"])]
    if args.get("model"):
        command += ["--model", str(args["model"])]
    command += ["--message", str(args.get("message") or "")]
    return {"session": _run(command).strip()}


def send(args: dict) -> Any:
    command = ["send", *_target(args.get("target"), args.get("issue")),
               "--message", str(args.get("message") or "")]
    if args.get("sender"):
        command += ["--from", str(args["sender"])]
    _run(command)
    return {"ok": True}


def read(args: dict) -> Any:
    command = ["read", *_target(args.get("target"), args.get("issue"))]
    if args.get("head"):
        command.append("--head")
    return {"text": _run(command)}


def status(args: dict) -> Any:
    return {"status": json.loads(_run(["status", *_target(args.get("target"), args.get("issue"))]))}


def info(args: dict) -> Any:
    return {"info": json.loads(_run(["info", *_target(args.get("target"), args.get("issue"))]))}


def rename(args: dict) -> Any:
    _run(["rename", *_target(args.get("target"), args.get("issue")), "--name", str(args["name"])])
    return {"ok": True}


def board(args: dict) -> Any:
    command = ["board"]
    if args.get("all"):
        command.append("--all")
    if args.get("status"):
        command.append("--status")
    return {"board": _run(command, timeout=180.0)}


def approve(args: dict) -> Any:
    command = ["approve", *_target(args.get("target"), args.get("issue")),
               "--decision", str(args.get("decision") or "allow")]
    _run(command)
    return {"ok": True}


def relay(args: dict) -> Any:
    command = ["relay", str(args["ticket"]), "--message", str(args.get("message") or "")]
    if args.get("comment"):
        command += ["--comment", str(args["comment"])]
    if args.get("no_comment"):
        command.append("--no-comment")
    if args.get("to"):
        command += ["--to", str(args["to"])]
    _run(command)
    return {"ok": True}


def merge(args: dict) -> Any:
    command = ["merge", str(args["pr"]), str(args["sha"])]
    if args.get("ticket") is not None:
        command += ["--ticket", str(args["ticket"])]
    if args.get("no_ticket"):
        command.append("--no-ticket")
    return {"merge": _run(command)}


def log(args: dict) -> Any:
    command = ["log"]
    for item in args.get("items") or []:
        command += ["--item", str(item)]
    for pending in args.get("pending") or []:
        command += ["--pending", str(pending)]
    if args.get("issue") is not None:
        command += ["--issue", str(args["issue"])]
    if not args.get("items"):
        raise ToolError("log needs at least one item")
    _run(command)
    return {"ok": True}


def launch(args: dict) -> Any:
    command = ["launch", str(args["name"])]
    if args.get("message"):
        command += ["--message", str(args["message"])]
    if args.get("session"):
        command += ["--session", str(args["session"])]
    if args.get("record"):
        command += ["--record", str(args["record"])]
    command += ["--", *[str(part) for part in args.get("command") or []]]
    if not args.get("command"):
        raise ToolError("launch needs the command to run")
    return {"launch": _run(command)}


def logs(args: dict) -> Any:
    command = ["logs", str(args["name"])]
    if args.get("lines") is not None:
        command += ["--lines", str(args["lines"])]
    return {"text": _run(command)}


def stop(args: dict) -> Any:
    _run(["stop", str(args["name"])])
    return {"ok": True}


def _object(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_TARGET = {"type": "string", "description": "Session id, exact name, '#42', OWNER/REPO#42, or issue URL"}
_ISSUE = {"type": "integer", "description": "Ticket number, when the session is addressed by ticket"}

TOOLS: dict[str, dict] = {
    "new_session": {
        "fn": new_session,
        "description": "Create a DeepSeek Harness session for a station and start its first message. "
                       "Returns the session id.",
        "schema": _object({"cwd": {"type": "string", "description": "Working directory (default: current)"},
                           "name": {"type": "string", "description": "Session title"},
                           "station": {"type": "string",
                                       "description": "orchestrator, designer, verifier, or executor"},
                           "issue": _ISSUE, "message": {"type": "string"},
                           "provider": {"type": "string"}, "model": {"type": "string"}},
                          ["name"]),
    },
    "send": {
        "fn": send,
        "description": "Send a message to a session and wait for it to be delivered.",
        "schema": _object({"target": _TARGET, "issue": _ISSUE,
                           "message": {"type": "string"},
                           "sender": {"type": "string",
                                      "description": "Prefix the message with '[from NAME]'"}},
                          ["message"]),
    },
    "read": {
        "fn": read,
        "description": "Print a session's latest reply.",
        "schema": _object({"target": _TARGET, "issue": _ISSUE,
                           "head": {"type": "boolean", "description": "Only the first paragraph"}}),
    },
    "status": {
        "fn": status,
        "description": "A session's status as JSON: idle or working, stop reason, and last error.",
        "schema": _object({"target": _TARGET, "issue": _ISSUE}),
    },
    "info": {
        "fn": info,
        "description": "A session's id, name, working directory, station, and model route as JSON.",
        "schema": _object({"target": _TARGET, "issue": _ISSUE}),
    },
    "rename": {
        "fn": rename,
        "description": "Change the name dkitchen records for a session.",
        "schema": _object({"target": _TARGET, "issue": _ISSUE,
                           "name": {"type": "string"}}, ["name"]),
    },
    "board": {
        "fn": board,
        "description": "List sessions in this repository with their tickets, latest replies, and jobs.",
        "schema": _object({"all": {"type": "boolean", "description": "Include every directory"},
                           "status": {"type": "boolean", "description": "Show ticket status blocks"}}),
    },
    "approve": {
        "fn": approve,
        "description": "Answer a permission prompt the harness is waiting on, when approvals = 'ask'.",
        "schema": _object({"target": _TARGET, "issue": _ISSUE,
                           "decision": {"type": "string", "enum": ["allow", "reject"]}}),
    },
    "relay": {
        "fn": relay,
        "description": "Send a message to a ticket's executor, then leave it on the ticket. Needs gh.",
        "schema": _object({"ticket": {"type": "integer"}, "message": {"type": "string"},
                           "comment": {"type": "string"}, "no_comment": {"type": "boolean"},
                           "to": {"type": "string", "description": "Executor, when the ticket has no owner line"}},
                          ["ticket", "message"]),
    },
    "merge": {
        "fn": merge,
        "description": "Merge a reviewed PR at the reviewed HEAD. Needs gh.",
        "schema": _object({"pr": {"type": "integer"}, "sha": {"type": "string"},
                           "ticket": {"type": "integer"}, "no_ticket": {"type": "boolean"}},
                          ["pr", "sha"]),
    },
    "log": {
        "fn": log,
        "description": "Post a signed, UTC-stamped entry to the orchestrator log ticket. Needs gh.",
        "schema": _object({"items": {"type": "array", "items": {"type": "string"},
                                     "description": "Automatic actions, one line each"},
                           "pending": {"type": "array", "items": {"type": "string"},
                                       "description": "Matters waiting for the user"},
                           "issue": {"type": "integer"}}, ["items"]),
    },
    "launch": {
        "fn": launch,
        "description": "Run a long job in the background and wake the session when it exits. Returns at once.",
        "schema": _object({"name": {"type": "string"}, "command": {"type": "array", "items": {"type": "string"}},
                           "message": {"type": "string"}, "session": {"type": "string"},
                           "record": {"type": "string"}}, ["name", "command"]),
    },
    "logs": {
        "fn": logs,
        "description": "Print a launched job's latest output.",
        "schema": _object({"name": {"type": "string"},
                           "lines": {"type": "integer"}}, ["name"]),
    },
    "stop": {
        "fn": stop,
        "description": "End a launched job and everything it started.",
        "schema": _object({"name": {"type": "string"}}, ["name"]),
    },
}


def tool_list() -> list[dict]:
    return [{"name": name,
             "description": spec["description"],
             "inputSchema": spec["schema"]}
            for name, spec in TOOLS.items()]


def call_tool(name: str, arguments: dict) -> Any:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"unknown tool {name!r}; this server offers {', '.join(TOOLS)}")
    return spec["fn"](arguments or {})


# -- JSON-RPC --------------------------------------------------------------------------

def _reply(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, one reply out (or None for a notification)."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if request_id is None:
        return None  # a notification, such as notifications/initialized
    if method == "initialize":
        return _reply(request_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        })
    if method == "tools/list":
        return _reply(request_id, {"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name")
        try:
            value = call_tool(str(name), params.get("arguments") or {})
        except ToolError as error:
            # A failed tool is a result the model reads, not a transport error: the turn
            # continues and the model can try something else.
            return _reply(request_id, {"content": [{"type": "text", "text": str(error)}],
                                       "isError": True})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            return _reply(request_id, {"content": [{"type": "text", "text": str(error)}],
                                       "isError": True})
        return _reply(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
    if method == "ping":
        return _reply(request_id, {})
    return _error(request_id, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> int:
    """Read JSON-RPC from stdin until EOF, answering on stdout. Returns an exit code."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error")
        else:
            try:
                reply = handle(message)
            except Exception as error:  # a bug here must not kill the server silently
                reply = _error(message.get("id"), -32603, f"internal error: {error}")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
