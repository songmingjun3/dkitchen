"""A stand-in DeepSeek Harness ACP server for tests.

The real harness is a Node process; tests must never start one, never touch a real session
log, and never send anything to a real model. This module is a small Python program that
speaks the same newline-delimited JSON-RPC over stdio, so `dkitchen.acp` is exercised against
the wire contract rather than a mock of its own methods.

Behaviour it reproduces, because dkitchen depends on each:

- one `session/prompt` in flight per session, rejected otherwise;
- `session/prompt` settles only after the scripted turn, so a caller can observe queueing;
- `session/close` and `session/resume` with the "already active" refusal the real server
  uses when another process owns a session;
- the model selector value as `["provider","model"]`;
- one `session/request_permission` when a prompt asks for it, answered by the client.

Run it directly to speak ACP by hand, or point `DKITCHEN_DSH_BIN` at `fake_dsh.py` so that
`dkitchen.acp.Connection` starts it exactly as it would start the real launcher.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

MODELS = [
    {"provider": "deepseek-official", "model": "deepseek-v4-flash", "name": "deepseek-v4-flash"},
    {"provider": "agentroute-codex", "model": "gpt-6-astra", "name": "gpt-6-astra"},
    {"provider": "docode-claude", "model": "claude-sonnet-5", "name": "claude-sonnet-5"},
]

EFFORTS = ["off", "low", "high", "max"]


def selector(provider: str, model: str) -> str:
    return json.dumps([provider, model], separators=(",", ":"))


class Session:
    def __init__(self, session_id: str, cwd: str) -> None:
        self.id = session_id
        self.cwd = cwd
        self.provider = "deepseek-official"
        self.model = "deepseek-v4-flash"
        self.effort = ""
        self.in_flight: str | None = None
        self.messages: list[str] = []

    def options(self) -> list[dict]:
        return [
            {"id": "model", "name": "Model", "type": "select",
             "currentValue": selector(self.provider, self.model),
             "options": [{"group": "providers", "name": "Providers",
                          "options": [{"value": selector(row["provider"], row["model"]),
                                       "name": row["name"]} for row in MODELS]}]},
            {"id": "reasoning_effort", "name": "Reasoning effort", "type": "select",
             "currentValue": self.effort,
             "options": [{"value": "", "name": "Provider default"}]
                        + [{"value": effort, "name": effort.title()} for effort in EFFORTS]},
        ]


class FakeServer:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.owned_elsewhere: set[str] = set()
        self.next_id = 0
        self.write_lock = threading.Lock()
        self.permission_armed = os.environ.get("FAKE_ACP_PERMISSION") == "1"
        self.turn_delay = float(os.environ.get("FAKE_ACP_TURN_DELAY") or 0.05)
        #: Sessions the scripted run should behave as if another process owned.
        self.busy = {value for value in (os.environ.get("FAKE_ACP_BUSY") or "").split(",") if value}

    # -- transport -------------------------------------------------------------------

    def send(self, message: dict) -> None:
        with self.write_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def reply(self, request_id, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def fail(self, request_id, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32602, "message": message}})

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- request handling ------------------------------------------------------------

    def handle(self, request: dict) -> None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            self.reply(request_id, {
                "protocolVersion": 1,
                "agentInfo": {"name": "deepseek-harness-acp-fake", "version": "fake"},
                "agentCapabilities": {"sessionCapabilities": {"close": {}, "list": {}, "resume": {}}},
                "authMethods": [],
            })
            return
        if method == "session/new":
            cwd = params.get("cwd")
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                self.fail(request_id, f"cwd must be absolute: {cwd}")
                return
            session = Session(str(uuid.uuid4()), cwd)
            self.sessions[session.id] = session
            self.reply(request_id, {"sessionId": session.id, "configOptions": session.options()})
            return
        if method == "session/resume":
            session_id = params.get("sessionId")
            if session_id in self.busy or session_id in self.sessions:
                self.fail(request_id, f"session is already active: {session_id}")
                return
            cwd = params.get("cwd")
            session = Session(str(session_id), str(cwd))
            self.sessions[session.id] = session
            self.reply(request_id, {"configOptions": session.options()})
            return
        if method == "session/set_config_option":
            session = self.sessions.get(params.get("sessionId"))
            if session is None:
                self.fail(request_id, f"unknown session: {params.get('sessionId')}")
                return
            config_id = params.get("configId")
            value = params.get("value")
            if config_id == "model":
                known = {selector(row["provider"], row["model"]) for row in MODELS}
                if value not in known:
                    self.fail(request_id, f"unknown model option: {value}")
                    return
                provider, model = json.loads(value)
                session.provider, session.model = provider, model
            elif config_id == "reasoning_effort":
                if value not in ["", *EFFORTS]:
                    self.fail(request_id, f"unknown reasoning effort: {value}")
                    return
                session.effort = value
            else:
                self.fail(request_id, f"unknown session config option: {config_id}")
                return
            self.reply(request_id, {"configOptions": session.options()})
            return
        if method == "session/close":
            session_id = params.get("sessionId")
            self.sessions.pop(session_id, None)
            self.reply(request_id, {})
            return
        if method == "session/list":
            self.reply(request_id, {"sessions": [{"sessionId": session.id, "cwd": session.cwd}
                                                 for session in self.sessions.values()]})
            return
        if method == "session/cancel":
            session = self.sessions.get(params.get("sessionId"))
            if session is not None:
                session.in_flight = None
            return
        if method == "session/prompt":
            threading.Thread(target=self.prompt, args=(request_id, params), daemon=True).start()
            return
        self.fail(request_id, f"unknown method: {method}")

    def prompt(self, request_id, params: dict) -> None:
        session = self.sessions.get(params.get("sessionId"))
        if session is None:
            self.fail(request_id, f"unknown session: {params.get('sessionId')}")
            return
        if session.in_flight is not None:
            self.fail(request_id, "a prompt is already in flight for this session")
            return
        blocks = params.get("prompt") or []
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
        session.in_flight = text
        session.messages.append(text)
        turn_text = f"echo: {text}"
        try:
            self.notify("session/update", {
                "sessionId": session.id,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": turn_text}},
            })
            if self.permission_armed and "approve-me" in text:
                allowed = self.ask_permission(session.id)
                turn_text += " (allowed)" if allowed else " (denied)"
            time.sleep(self.turn_delay)
            self.reply(request_id, {"stopReason": "end_turn"})
        finally:
            session.in_flight = None

    def ask_permission(self, session_id: str) -> bool:
        """Ask the client once and read its answer synchronously."""
        self.next_id += 1000
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "session/request_permission",
                   "params": {"sessionId": session_id,
                              "toolCall": {"toolCallId": "call-1", "title": "run a command"},
                              "options": [{"optionId": "allow-once", "name": "Allow once",
                                           "kind": "allow_once"},
                                          {"optionId": "reject-once", "name": "Reject",
                                           "kind": "reject_once"}]}})
        line = sys.stdin.readline()
        if not line:
            return False
        message = json.loads(line)
        outcome = (message.get("result") or {}).get("outcome") or {}
        return outcome.get("optionId") == "allow-once"

    def serve(self) -> int:
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                request = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(request, dict) and "method" in request:
                self.handle(request)
        return 0


def main() -> int:
    return FakeServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
