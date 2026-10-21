"""Drive DeepSeek Harness sessions over the Agent Client Protocol.

`dsh --profile <name>` with the ACP application bundle speaks ACP v1 as newline-delimited
JSON-RPC over stdio: one request or notification object per line on each side. dkitchen
spawns exactly one such process and multiplexes every kitchen session over it, because ACP
selects the working directory and the model per session.

The methods this module uses, and the reason each one is needed:

| Method | Why dkitchen needs it |
| --- | --- |
| `initialize` | handshake; `protocolVersion` is 1 in every DSH release dkitchen targets |
| `session/new` | create an executor/station session in a specific directory |
| `session/resume` | wake a session that is not loaded in this process |
| `session/prompt` | deliver one message; settles when the whole turn (and its subagents) ends |
| `session/cancel` | stop a turn a human asked to stop |
| `session/close` | release a session from this process so the GUI can open it |
| `session/set_config_option` | select the provider/model and reasoning effort per station |

Three contract details shape the code below:

- A session accepts one `session/prompt` at a time; a second one is rejected with
  "a prompt is already in flight". Queueing is therefore the caller's job, not the protocol's.
- `session/prompt` settles only when the root turn *and its subagents* finish, so a prompt
  can stay in flight for a long time. Delivery, not completion, is what `ding send` reports.
- A session is owned by one process at a time. When the GUI holds a session, `session/resume`
  fails; dkitchen reports that instead of fighting for it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Callable

from dkitchen import paths

#: ACP protocol version dkitchen speaks.
PROTOCOL_VERSION = 1

#: ACP session configuration ids advertised by DeepSeek Harness.
MODEL_OPTION = "model"
EFFORT_OPTION = "reasoning_effort"

#: How long a handshake or a metadata request may take.
CALL_TIMEOUT = 120


class AcpError(ValueError):
    """The harness refused a request, or answered with an error."""


class AcpUnavailable(AcpError):
    """The ACP process is gone or cannot be started."""


class SessionBusy(AcpError):
    """Another DeepSeek Harness process owns the session."""


@dataclass
class Turn:
    """One accepted prompt: the message that went in and how it ended.

    The reply text is not carried here: it is durable in the session log, and `ding read`
    folds that log, so the same command reads replies from sessions this process never
    drove (including ones the GUI is running).
    """

    message: str
    started: bool = False
    stop_reason: str | None = None
    error: str | None = None


class Connection:
    """One `dsh --profile <name>` process and its newline-delimited JSON-RPC channel."""

    def __init__(self, executable: str, profile: str, *, env: dict | None = None,
                 cwd: Path | None = None, log_path: Path | None = None) -> None:
        self.executable = executable
        self.profile = profile
        self.log_path = log_path
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._updates: Callable[[str, dict], None] | None = None
        self._permissions: Callable[[str, dict], dict] | None = None
        self._reader: threading.Thread | None = None
        self._closed = False
        self._write_lock = threading.Lock()
        environment = os.environ.copy()
        environment.update(env or {})
        environment.setdefault("DSH_HOME", str(paths.dsh_home()))
        command = [executable, "--profile", profile]
        if paths.WINDOWS and executable.lower().endswith((".cmd", ".bat")):
            # A .cmd shim is not an executable image; cmd /c is the portable way to run it.
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *command]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None, env=environment, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as error:
            raise AcpUnavailable(f"cannot start DeepSeek Harness: {error}") from error
        self._stderr_tail: list[str] = []
        threading.Thread(target=self._drain_stderr, name="dkitchen-acp-stderr", daemon=True).start()
        self._reader = threading.Thread(target=self._read_loop, name="dkitchen-acp-reader", daemon=True)
        self._reader.start()

    # -- transport --------------------------------------------------------------------

    def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        for line in stream:
            text = line.rstrip("\r\n")
            if not text:
                continue
            self._stderr_tail.append(text)
            if len(self._stderr_tail) > 200:
                del self._stderr_tail[0]
            if self.log_path is not None:
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.log_path.open("a", encoding="utf-8") as handle:
                        handle.write(text + "\n")
                except OSError:
                    pass

    def _read_loop(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        for line in stream:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                self._handle_request(message)
            elif "id" in message and "method" not in message:
                self._handle_response(message)
            elif "method" in message:
                self._handle_notification(message)
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot.put(("error", AcpUnavailable("the DeepSeek Harness process exited")))

    def _handle_response(self, message: dict) -> None:
        with self._lock:
            slot = self._pending.pop(message.get("id"), None)
        if slot is None:
            return
        if "error" in message:
            error = message["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            slot.put(("error", AcpError(str(detail))))
        else:
            slot.put(("result", message.get("result") or {}))

    def _handle_notification(self, message: dict) -> None:
        if message.get("method") != "session/update":
            return
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        update = params.get("update")
        if isinstance(session_id, str) and isinstance(update, dict) and self._updates is not None:
            try:
                self._updates(session_id, update)
            except Exception:  # a listener must never kill the transport
                pass

    def _handle_request(self, message: dict) -> None:
        """Answer the one client-side request DSH sends: a permission prompt."""
        method = message.get("method")
        params = message.get("params") or {}
        if method == "session/request_permission" and isinstance(params, dict):
            session_id = params.get("sessionId")
            decision = {"outcome": "cancelled"}
            if self._permissions is not None and isinstance(session_id, str):
                try:
                    decision = self._permissions(session_id, params)
                except Exception:
                    decision = {"outcome": "cancelled"}
            self._send({"jsonrpc": "2.0", "id": message.get("id"), "result": {"outcome": decision}})
            return
        self._send({"jsonrpc": "2.0", "id": message.get("id"),
                    "error": {"code": -32601, "message": f"unsupported client method: {method}"}})

    def _send(self, message: dict) -> None:
        if self._closed or self.process.stdin is None:
            raise AcpUnavailable("the DeepSeek Harness process is not running")
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (OSError, ValueError) as error:
                raise AcpUnavailable(f"cannot write to DeepSeek Harness: {error}") from error

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, method: str, params: dict, *, timeout: float = CALL_TIMEOUT) -> dict:
        if self._closed:
            raise AcpUnavailable(self._exit_detail())
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            slot: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = slot
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            kind, value = slot.get(timeout=timeout)
        except queue.Empty as error:
            with self._lock:
                self._pending.pop(request_id, None)
            raise AcpError(f"{method} timed out after {timeout:.0f}s") from error
        if kind == "error":
            raise value
        return value

    def _exit_detail(self) -> str:
        code = self.process.poll()
        tail = self._stderr_tail[-3:]
        detail = f": {' | '.join(tail)}" if tail else ""
        return f"the DeepSeek Harness process exited (code {code}){detail}"

    # -- lifecycle --------------------------------------------------------------------

    def on_update(self, listener: Callable[[str, dict], None]) -> None:
        self._updates = listener

    def on_permission(self, listener: Callable[[str, dict], dict]) -> None:
        self._permissions = listener

    def initialize(self) -> dict:
        result = self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False},
            "clientInfo": {"name": "dkitchen", "version": "1"},
        })
        return result if isinstance(result, dict) else {}

    # -- sessions ---------------------------------------------------------------------

    def new_session(self, cwd: Path) -> tuple[str, list[dict]]:
        result = self.call("session/new", {"cwd": str(cwd), "mcpServers": []}, timeout=CALL_TIMEOUT)
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("session/new returned no session id")
        options = result.get("configOptions")
        return session_id, options if isinstance(options, list) else []

    def resume(self, session_id: str, cwd: Path) -> list[dict]:
        try:
            result = self.call("session/resume", {"sessionId": session_id, "cwd": str(cwd)},
                               timeout=CALL_TIMEOUT)
        except AcpError as error:
            detail = str(error)
            if "already active" in detail or "occupied" in detail or "already open" in detail:
                raise SessionBusy(f"session {session_id} is owned by another DeepSeek Harness "
                                  f"process: {detail}") from error
            raise
        options = result.get("configOptions") if isinstance(result, dict) else None
        return options if isinstance(options, list) else []

    def close(self, session_id: str) -> None:
        try:
            self.call("session/close", {"sessionId": session_id}, timeout=CALL_TIMEOUT)
        except AcpError:
            pass

    def set_option(self, session_id: str, config_id: str, value: str) -> list[dict]:
        result = self.call("session/set_config_option",
                           {"sessionId": session_id, "configId": config_id, "value": value},
                           timeout=CALL_TIMEOUT)
        options = result.get("configOptions") if isinstance(result, dict) else None
        return options if isinstance(options, list) else []

    def cancel(self, session_id: str) -> None:
        self.notify("session/cancel", {"sessionId": session_id})

    def prompt(self, session_id: str, text: str, *, timeout: float | None = None) -> Turn:
        turn = Turn(message=text)
        params = {"sessionId": session_id,
                  "prompt": [{"type": "text", "text": text}]}
        try:
            result = self.call("session/prompt", params,
                               timeout=timeout if timeout is not None else 24 * 3600)
        except AcpError as error:
            if "already in flight" in str(error):
                raise
            turn.error = str(error)
            return turn
        turn.stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        turn.started = True
        return turn

    # -- teardown ---------------------------------------------------------------------

    def shutdown(self, *, grace: float = 10) -> None:
        if self.process.poll() is None:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        self._closed = True

    @property
    def alive(self) -> bool:
        return not self._closed and self.process.poll() is None


def model_choices(options: list[dict]) -> list[dict]:
    """Flatten the `model` selector into `{value, name, provider, model}` rows.

    DeepSeek Harness encodes a selector value as the JSON array `["provider","model"]`, so
    each row carries the route it stands for as well as the opaque value to send back.
    """
    for option in options:
        if isinstance(option, dict) and option.get("id") == MODEL_OPTION:
            rows = _flatten(option.get("options"))
            for row in rows:
                provider, model = route_of(row["value"])
                row["provider"], row["model"] = provider, model
            return rows
    return []


def route_of(value: str) -> tuple[str | None, str | None]:
    """Split a `model` selector value into its provider and model."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None, value
    if isinstance(parsed, list) and len(parsed) == 2 and all(isinstance(item, str) for item in parsed):
        return parsed[0], parsed[1]
    return None, value


def encode_route(provider: str, model: str) -> str:
    """The selector value naming one exact route.

    DeepSeek Harness writes this value with JavaScript's `JSON.stringify`, which emits no
    space after the comma; the harness compares the string it receives against those keys,
    so the separators must match exactly.
    """
    return json.dumps([provider, model], ensure_ascii=False, separators=(",", ":"))


def effort_choices(options: list[dict]) -> list[dict]:
    for option in options:
        if isinstance(option, dict) and option.get("id") == EFFORT_OPTION:
            return _flatten(option.get("options"))
    return []


def current_value(options: list[dict], config_id: str) -> str | None:
    for option in options:
        if isinstance(option, dict) and option.get("id") == config_id:
            value = option.get("currentValue")
            return value if isinstance(value, str) else None
    return None


def _flatten(entries: object) -> list[dict]:
    """Accept both the ungrouped and grouped ACP option shapes."""
    rows: list[dict] = []
    if not isinstance(entries, list):
        return rows
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("options"), list):
            rows.extend(_flatten(entry["options"]))
            continue
        value = entry.get("value")
        if isinstance(value, str):
            rows.append({"value": value, "name": entry.get("name") or value,
                         "description": entry.get("description") or ""})
    return rows


def session_id_from(value: str) -> str:
    """The harness session id for a value the user typed.

    DeepSeek Harness ids are bare UUIDs in the ACP surface and in the persisted log
    directory, so no prefix is added; a leading `session-` (the SDK profile's spelling of
    the same identity) is kept as typed only when it is the whole identifier.
    """
    return value.strip()
