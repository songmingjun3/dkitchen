"""The long-lived process that owns one DeepSeek Harness ACP connection.

Why a broker exists at all: `session/prompt` settles only when a whole turn ends, and one
session accepts one prompt at a time. A CLI invocation that sent a message and exited would
either block for the length of the turn or lose the settlement. So one process stays up,
holds the ACP connection, and owns this state:

- which sessions are loaded, their working directory, and their route;
- a per-session queue of messages, so `ding send` can answer in milliseconds and the
  message still runs to completion;
- the outcome of every prompt (stop reason, error), written to an append-only journal.

Every other `ding` command talks to it over a local socket: `AF_UNIX` on POSIX, a named pipe
on Windows. The endpoint name and the owning PID live in the state directory.

Failure policy, which is the interesting part:

- A prompt that was accepted but never started is retried once. A prompt that started is
  never re-sent, because re-sending would make the executor do the work twice.
- If the ACP process dies, the broker marks every loaded session unavailable, records the
  reason, and exits; the next `ding` command starts a fresh broker and resumes sessions on
  demand. Nothing is replayed automatically.
- A session owned by another harness process (the GUI) is reported, never stolen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import multiprocessing.connection as connection_lib
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import traceback
from typing import Any

from dkitchen import acp, locks, paths, registry

#: Authentication key for the local socket. It never leaves this machine.
AUTHKEY = b"dkitchen-broker"

#: How long a client waits for a broker reply.
CLIENT_TIMEOUT = 180.0


def endpoint() -> str:
    """The IPC name the broker listens on.

    It is derived from the state root, so two state roots (a test's and the user's, or two
    checkouts with their own `DKITCHEN_STATE_HOME`) never collide on one pipe or socket.
    """
    token = hashlib.sha256(str(paths.state_root().resolve()).encode("utf-8")).hexdigest()[:12]
    if paths.WINDOWS:
        return rf"\\.\pipe\dkitchen-{paths.user_key()}-{token}"
    return str(paths.state("broker.sock"))


def pid_file() -> Path:
    return paths.state("broker.pid")


#: How long an `approvals = "ask"` prompt waits for `ding approve` before it is cancelled.
PERMISSION_TIMEOUT_SECONDS = 3600.0

#: How long to wait for a slow profile plugin to publish its model routes.
#:
#: Some bundles register provider routes at runtime instead of declaring them in the
#: profile patch — `dsh-wtq-pool` does this through `inject: [llm, settings]`. A cold
#: broker answers `session/new` before those plugins have finished, so the session's
#: options hold only the routes the composition declared statically. Selecting a station's
#: route then fails, and the session silently runs on whatever the profile defaults to,
#: which for a profile without that provider's credentials fails on the first turn with an
#: unrelated message. Measured on a cold start: the first listing showed no `xdpool` routes
#: at all, the second showed 55.
ROUTE_SETTLE_SECONDS = 20.0

#: Seconds between attempts to see a late route while waiting out `ROUTE_SETTLE_SECONDS`.
ROUTE_SETTLE_INTERVAL = 1.5


def journal() -> Path:
    return paths.state("broker.jsonl")


def log_file() -> Path:
    return paths.state("broker.log")


def _append(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


# -- client side -------------------------------------------------------------------------


class BrokerDown(RuntimeError):
    """No broker is listening."""


def connect(*, timeout: float = 5.0):
    """An authenticated connection to the running broker."""
    address = endpoint()
    if not paths.WINDOWS and not Path(address).exists():
        raise BrokerDown("no broker is running")
    try:
        return connection_lib.Client(address, family=_family(), authkey=AUTHKEY)
    except (OSError, EOFError) as error:
        raise BrokerDown(f"no broker is running ({error})") from error


def _family() -> str:
    return "AF_PIPE" if paths.WINDOWS else "AF_UNIX"


def request(action: str, timeout: float = CLIENT_TIMEOUT, **payload: Any) -> dict:
    """Send one request and return its reply; raises `BrokerDown` when nobody is listening."""
    client = connect()
    try:
        client.send({"action": action, **payload})
        if not client.poll(timeout):
            raise BrokerDown(f"the broker did not answer {action!r} within {timeout:.0f}s")
        reply = client.recv()
    except (OSError, EOFError) as error:
        raise BrokerDown(f"lost the broker connection: {error}") from error
    finally:
        try:
            client.close()
        except OSError:
            pass
    if not isinstance(reply, dict):
        raise BrokerDown("the broker sent a malformed reply")
    return reply


def running() -> bool:
    """Whether a broker answers right now."""
    try:
        pid = request("ping", timeout=5)
    except (BrokerDown, ValueError):
        return False
    return bool(pid.get("ok"))


def ensure(*, profiles: dict | None = None, wait: float = 60.0) -> bool:
    """Start a broker in the background unless one answers; return whether it is up.

    Set `DKITCHEN_BROKER_AUTOSTART=0` to forbid starting one: a supervised deployment that
    runs the broker itself, or a test, then gets a clear "not running" instead of a second
    broker appearing behind its back.
    """
    if os.environ.get("DKITCHEN_BROKER_AUTOSTART") == "0":
        return running()
    if running():
        return True
    lock = paths.state("broker.start.lock")
    try:
        with locks.exclusive(lock, blocking=False):
            if running():
                return True
            _spawn(profiles or {})
    except locks.LockBusy:
        # Another `ding` is starting it; wait for that one instead of racing it.
        pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if running():
            return True
        time.sleep(0.1)
    return False


def _spawn(profiles: dict) -> None:
    import subprocess

    paths.state_root().mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "dkitchen.broker", "serve"]
    environment = os.environ.copy()
    if profiles:
        environment["DKITCHEN_BROKER_PROFILES"] = json.dumps(profiles)
    kwargs: dict[str, Any] = {
        "cwd": str(Path.home()),
        "env": environment,
        "stdin": subprocess.DEVNULL,
    }
    if paths.WINDOWS:
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    with log_file().open("a", encoding="utf-8") as handle:
        subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, **kwargs)


# -- server side -------------------------------------------------------------------------


@dataclass
class Loaded:
    """One session this broker has open in its ACP process."""

    id: str
    cwd: str
    route: dict = field(default_factory=dict)
    queue: "queue.Queue[tuple[str, str]]" = field(default_factory=queue.Queue)
    worker: threading.Thread | None = None
    in_flight: str | None = None
    stop_reason: str | None = None
    last_error: str | None = None
    started_ns: int = 0

    @property
    def busy(self) -> bool:
        return self.in_flight is not None


class Broker:
    """Owns the ACP connection, the session map, and the reply journal."""

    def __init__(self, profiles: dict | None = None) -> None:
        self.profiles = profiles or {}
        self.sessions: dict[str, Loaded] = {}
        self.state_lock = threading.Lock()
        self.connection: acp.Connection | None = None
        self.stopping = threading.Event()
        self._listener = None
        self._pending_permissions: dict[str, dict] = {}
        self._permission_lock = threading.Lock()

    # -- ACP plumbing ---------------------------------------------------------------

    def _profile(self, station: str | None) -> str:
        if station and station in self.profiles:
            return str(self.profiles[station])
        return str(self.profiles.get("default") or paths.DEFAULT_PROFILE)

    def _connection(self, station: str | None = None) -> acp.Connection:
        """The ACP connection, started on first use.

        A replacement connection holds none of the sessions the old one had, so the loaded
        map is cleared with it: the next message resumes its session on demand instead of
        prompting a session this process no longer owns.
        """
        with self.state_lock:
            if self.connection is not None and self.connection.alive:
                return self.connection
            dropped = sorted(self.sessions)
            self.sessions.clear()
            if dropped:
                _append(journal(), {"event": "connection_lost", "sessions": dropped,
                                    "time_ns": time.time_ns()})
            profile = self._profile(station)
            connection = acp.Connection(paths.dsh_executable(), profile, log_path=log_file())
            connection.on_update(self._on_update)
            connection.on_permission(self._on_permission)
            connection.initialize()
            self.connection = connection
            _append(journal(), {"event": "connected", "profile": profile,
                                "pid": connection.process.pid, "time_ns": time.time_ns()})
            return connection

    def _on_update(self, session_id: str, update: dict) -> None:
        """Keep the last stop reason visible; the durable record lives in the session log."""
        kind = update.get("sessionUpdate")
        if kind in {"agent_message_chunk", "agent_thought_chunk", "tool_call", "turn_end"}:
            _append(journal(), {"event": "update", "session": session_id, "kind": kind,
                                "time_ns": time.time_ns()})

    def _on_permission(self, session_id: str, params: dict) -> dict:
        """Answer a permission prompt by policy.

        The harness's own sandbox mode already bounds what a session may touch; this bell
        only decides whether an approval waits for `ding approve` or is answered at once.
        A prompt that offers no option to choose is cancelled at once, and one left
        unanswered for an hour is cancelled too, so a forgotten `ding approve` never turns
        into a silent yes.
        """
        policy = str(self.profiles.get("approvals") or "auto")
        options = params.get("options") if isinstance(params.get("options"), list) else []

        def option_of(kind: str) -> str | None:
            return next((option.get("optionId") for option in options
                         if isinstance(option, dict) and option.get("kind") == kind), None)

        allow, reject = option_of("allow_once"), option_of("reject_once")
        _append(journal(), {"event": "permission", "session": session_id,
                            "policy": policy, "tool": (params.get("toolCall") or {}).get("title"),
                            "time_ns": time.time_ns()})
        if allow is None and reject is None:
            return {"outcome": "cancelled"}
        verdict = "allow"
        if policy == "ask":
            verdict = None
            with self._permission_lock:
                self._pending_permissions[session_id] = {"options": options,
                                                         "tool": params.get("toolCall")}
            deadline = time.monotonic() + PERMISSION_TIMEOUT_SECONDS
            while time.monotonic() < deadline and not self.stopping.is_set():
                with self._permission_lock:
                    verdict = self._pending_permissions.get(session_id, {}).get("decision")
                if verdict is not None:
                    break
                time.sleep(0.5)
            with self._permission_lock:
                self._pending_permissions.pop(session_id, None)
            _append(journal(), {"event": "permission_decision", "session": session_id,
                                "decision": verdict, "time_ns": time.time_ns()})
            if verdict is None:
                return {"outcome": "cancelled"}
        chosen = reject if verdict == "reject" else allow
        if chosen is None:
            return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": chosen}

    # -- session lifecycle ----------------------------------------------------------

    def create(self, *, cwd: str, name: str, station: str, issue: int | None, repo: str | None,
               route: dict, message: str | None) -> dict:
        connection = self._connection(station)
        session_id, options, route_selected = self._open_routed(connection, cwd, route)
        record = registry.Record(id=session_id, name=name, station=station, cwd=cwd,
                                 created_ns=time.time_ns(), issue=issue, repo=repo,
                                 provider=route.get("provider"), model=route.get("model"),
                                 effort=route.get("effort"))
        registry.put(record)
        self._adopt(session_id, cwd, route)
        delivered = None
        if message:
            delivered = self._enqueue(session_id, message)
        _append(journal(), {"event": "created", "session": session_id, "name": name,
                            "station": station, "cwd": cwd, "time_ns": time.time_ns()})
        return {"session": session_id, "name": name, "route": route,
                "options": options, "delivery": delivered,
                "route_selected": route_selected}

    def _open_routed(self, connection: acp.Connection, cwd: str,
                     route: dict) -> tuple[str, list[dict], bool]:
        """Open a session whose options already offer the station's route.

        A profile plugin that registers provider routes at runtime publishes them only after
        the harness has started, and `session/new` fixes the option list at that moment — a
        later `session/set_config_option` cannot widen it. A cold broker therefore opens the
        first session too early to see those routes, and the session would run on the
        profile's default route instead of the station's. Dropping the session and opening
        another one after a short wait is the only way to pick them up.
        """
        provider, model = route.get("provider"), route.get("model")
        deadline = time.monotonic() + ROUTE_SETTLE_SECONDS
        attempt = 0
        while True:
            attempt += 1
            session_id, options = connection.new_session(Path(cwd))
            if not (provider and model):
                return session_id, options, True
            available = {(row.get("provider"), row.get("model"))
                         for row in acp.model_choices(options)}
            if (provider, model) in available:
                options = self._select_route(connection, session_id, options, route)
                return session_id, options, True
            if time.monotonic() >= deadline:
                # Out of patience: keep the session, record why, and let the caller's route
                # stand so the failure names the real problem rather than a default route.
                _append(journal(), {"event": "route_unavailable", "session": session_id,
                                    "provider": provider, "model": model, "attempts": attempt,
                                    "available": sorted(f"{p}/{m}" for p, m in available if p),
                                    "time_ns": time.time_ns()})
                return session_id, options, False
            connection.close(session_id)
            _append(journal(), {"event": "route_waiting", "session": session_id,
                                "provider": provider, "model": model, "attempt": attempt,
                                "time_ns": time.time_ns()})
            time.sleep(ROUTE_SETTLE_INTERVAL)

    def _select_route(self, connection: acp.Connection, session_id: str,
                      options: list[dict], route: dict) -> list[dict]:
        """Select the station's model and reasoning effort on a session that offers them."""
        provider, model = route.get("provider"), route.get("model")
        if provider and model:
            try:
                options = connection.set_option(session_id, acp.MODEL_OPTION,
                                                acp.encode_route(provider, model))
            except acp.AcpError as error:
                _append(journal(), {"event": "route_refused", "session": session_id,
                                    "provider": provider, "model": model, "error": str(error),
                                    "time_ns": time.time_ns()})
        effort = route.get("effort")
        if effort:
            values = {row.get("value") for row in acp.effort_choices(options)}
            if effort in values:
                try:
                    options = connection.set_option(session_id, acp.EFFORT_OPTION, effort)
                except acp.AcpError as error:
                    _append(journal(), {"event": "effort_refused", "session": session_id,
                                        "effort": effort, "error": str(error),
                                        "time_ns": time.time_ns()})
        return options

    def _apply_route(self, connection: acp.Connection, session_id: str,
                     options: list[dict], route: dict) -> list[dict]:
        """Select the station's model and reasoning effort, tolerating an unlisted route."""
        provider, model = route.get("provider"), route.get("model")
        if provider and model:
            available = {(row.get("provider"), row.get("model")) for row in acp.model_choices(options)}
            if (provider, model) not in available:
                _append(journal(), {"event": "route_unavailable", "session": session_id,
                                    "provider": provider, "model": model,
                                    "available": sorted(f"{p}/{m}" for p, m in available if p),
                                    "time_ns": time.time_ns()})
                return options
        return self._select_route(connection, session_id, options, route)

    def _adopt(self, session_id: str, cwd: str, route: dict) -> Loaded:
        """Register a session this connection already holds, and start its worker."""
        loaded = Loaded(id=session_id, cwd=cwd, route=route, started_ns=time.time_ns())
        with self.state_lock:
            existing = self.sessions.get(session_id)
            if existing is not None:
                return existing
            self.sessions[session_id] = loaded
        worker = threading.Thread(target=self._work, args=(loaded,),
                                  name=f"dkitchen-session-{session_id[-8:]}", daemon=True)
        loaded.worker = worker
        worker.start()
        return loaded

    def _resume(self, session_id: str, cwd: str, route: dict) -> Loaded:
        connection = self._connection()
        try:
            options = connection.resume(session_id, Path(cwd))
        except acp.SessionBusy:
            raise
        if route:
            self._apply_route(connection, session_id, options, route)
        return self._adopt(session_id, cwd, route)

    def _recover(self, loaded: Loaded) -> Loaded | None:
        """Reopen a session whose owning connection is gone, keeping its queue and worker.

        A replacement connection starts with an empty session map, so recovery also puts the
        session back under this broker; otherwise `status` would report it as not loaded and
        the next `send` would resume a second copy.
        """
        try:
            connection = self._connection()
            options = connection.resume(loaded.id, Path(loaded.cwd))
        except (acp.AcpError, OSError) as error:
            _append(journal(), {"event": "recover_failed", "session": loaded.id,
                                "error": str(error), "time_ns": time.time_ns()})
            return None
        if loaded.route:
            self._apply_route(connection, loaded.id, options, loaded.route)
        with self.state_lock:
            self.sessions[loaded.id] = loaded
        _append(journal(), {"event": "recovered", "session": loaded.id,
                            "time_ns": time.time_ns()})
        return loaded

    def _enqueue(self, session_id: str, message: str) -> dict:
        loaded = self.sessions.get(session_id)
        if loaded is None:
            raise acp.AcpError(f"session {session_id} is not loaded in this broker")
        busy = loaded.busy or not loaded.queue.empty()
        loaded.queue.put(("message", message))
        return {"outcome": "queued_behind_active" if busy else "started", "busy": busy}

    def _work(self, loaded: Loaded) -> None:
        """Deliver queued messages one at a time, recording every outcome."""
        while not self.stopping.is_set():
            try:
                kind, message = loaded.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "stop":
                return
            loaded.in_flight = message
            try:
                connection = self._connection()
                turn = connection.prompt(loaded.id, message)
                if turn.error and "unknown session" in turn.error:
                    # The connection that owned this session is gone. Reopen it and retry
                    # once; a message that never started is safe to send again.
                    if self._recover(loaded) is not None:
                        loaded.queue.put(("message", message))
                        continue
                loaded.stop_reason = turn.stop_reason
                _append(journal(), {"event": "turn", "session": loaded.id,
                                    "stop_reason": turn.stop_reason, "error": turn.error,
                                    "chars": len(message), "time_ns": time.time_ns()})
            except acp.AcpError as error:
                loaded.last_error = str(error)
                _append(journal(), {"event": "turn_failed", "session": loaded.id,
                                    "error": str(error), "time_ns": time.time_ns()})
                if "already in flight" in str(error):
                    # Our own bookkeeping and the harness disagree; wait for the real turn.
                    time.sleep(1)
                    loaded.queue.put(("message", message))
                    continue
                if "unknown session" in str(error) and self._recover(loaded) is not None:
                    loaded.queue.put(("message", message))
                    continue
            except Exception as error:  # pragma: no cover - defensive
                loaded.last_error = str(error)
                _append(journal(), {"event": "turn_crashed", "session": loaded.id,
                                    "error": traceback.format_exc(limit=3),
                                    "time_ns": time.time_ns()})
            finally:
                loaded.in_flight = None
                try:
                    loaded.queue.task_done()
                except ValueError:
                    pass

    # -- request handlers -----------------------------------------------------------

    def handle(self, request: dict) -> dict:
        action = request.get("action")
        try:
            if action == "ping":
                return {"ok": True, "pid": os.getpid(), "sessions": sorted(self.sessions)}
            if action == "create":
                return {"ok": True, **self.create(
                    cwd=request["cwd"], name=request["name"], station=request["station"],
                    issue=request.get("issue"), repo=request.get("repo"),
                    route=request.get("route") or {}, message=request.get("message"))}
            if action == "send":
                session_id = request["session"]
                loaded = self.sessions.get(session_id)
                if loaded is None:
                    loaded = self._resume(session_id, request["cwd"], request.get("route") or {})
                return {"ok": True, **self._enqueue(session_id, request["message"])}
            if action == "status":
                loaded = self.sessions.get(request["session"])
                if loaded is None:
                    return {"ok": True, "state": "notLoaded"}
                return {"ok": True, "state": "active" if loaded.busy else "idle",
                        "in_flight": loaded.in_flight is not None,
                        "queued": loaded.queue.qsize(), "stop_reason": loaded.stop_reason,
                        "last_error": loaded.last_error}
            if action == "cancel":
                loaded = self.sessions.get(request["session"])
                connection = self._connection()
                connection.cancel(request["session"])
                return {"ok": True, "queued": loaded.queue.qsize() if loaded else 0}
            if action == "approve":
                with self._permission_lock:
                    pending = self._pending_permissions.get(request["session"])
                    if pending is None:
                        return {"ok": False, "error": "no permission request is waiting"}
                    pending["decision"] = request.get("decision") or "allow"
                return {"ok": True}
            if action == "close":
                session_id = request["session"]
                connection = self._connection()
                connection.close(session_id)
                with self.state_lock:
                    loaded = self.sessions.pop(session_id, None)
                if loaded is not None:
                    loaded.queue.put(("stop", ""))
                registry.mark_closed(session_id)
                return {"ok": True}
            if action == "models":
                # ACP advertises the catalog per session, so one throwaway session answers
                # for the whole runtime; it is closed again immediately.
                connection = self._connection()
                cwd = Path(request.get("cwd") or os.getcwd())
                created, options = connection.new_session(cwd)
                try:
                    return {"ok": True, "models": acp.model_choices(options),
                            "efforts": acp.effort_choices(options)}
                finally:
                    connection.close(created)
            if action == "shutdown":
                threading.Thread(target=self.stop, name="dkitchen-broker-stop", daemon=True).start()
                return {"ok": True}
            return {"ok": False, "error": f"unknown action: {action}"}
        except acp.SessionBusy as error:
            return {"ok": False, "error": str(error), "busy": True}
        except KeyError as error:
            return {"ok": False, "error": f"missing field: {error}"}
        except (acp.AcpError, OSError, ValueError) as error:
            return {"ok": False, "error": str(error)}

    # -- server loop ----------------------------------------------------------------

    def serve(self) -> int:
        paths.state_root().mkdir(parents=True, exist_ok=True)
        address = endpoint()
        if not paths.WINDOWS and Path(address).exists():
            Path(address).unlink()
        listener = connection_lib.Listener(address, family=_family(), authkey=AUTHKEY)
        self._listener = listener
        pid_file().write_text(str(os.getpid()), encoding="utf-8")
        _append(journal(), {"event": "start", "pid": os.getpid(), "endpoint": address,
                            "time_ns": time.time_ns()})
        for name in ("SIGINT", "SIGTERM"):
            handler = getattr(signal, name, None)
            if handler is not None:
                try:
                    signal.signal(handler, lambda *_: self.stop())
                except (ValueError, OSError):
                    pass
        # Accepting blocks in the platform's own call, which a closing listener does not
        # reliably interrupt on Windows; the thread pushes connections into a queue so this
        # loop can notice `stopping` and shut down within a bounded time.
        accepted: "queue.Queue" = queue.Queue()

        def accept_loop() -> None:
            while not self.stopping.is_set():
                try:
                    accepted.put(listener.accept())
                except (OSError, EOFError):
                    return

        threading.Thread(target=accept_loop, name="dkitchen-broker-accept", daemon=True).start()
        try:
            while not self.stopping.is_set():
                try:
                    client = accepted.get(timeout=0.2)
                except queue.Empty:
                    continue
                threading.Thread(target=self._serve_client, args=(client,),
                                 name="dkitchen-broker-client", daemon=True).start()
        finally:
            self._teardown()
        return 0

    def _serve_client(self, client) -> None:
        try:
            request = client.recv()
            if not isinstance(request, dict):
                client.send({"ok": False, "error": "malformed request"})
                return
            client.send(self.handle(request))
        except (OSError, EOFError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def stop(self) -> None:
        self.stopping.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _teardown(self) -> None:
        self.stopping.set()
        with self.state_lock:
            loaded = list(self.sessions.values())
            self.sessions.clear()
        for item in loaded:
            item.queue.put(("stop", ""))
        if self.connection is not None:
            for item in loaded:
                self.connection.close(item.id)
            self.connection.shutdown()
        for path in (pid_file(),):
            try:
                path.unlink()
            except OSError:
                pass
        if not paths.WINDOWS:
            try:
                Path(endpoint()).unlink()
            except OSError:
                pass
        _append(journal(), {"event": "stop", "pid": os.getpid(), "time_ns": time.time_ns()})


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "serve":
        print("usage: python -m dkitchen.broker serve", file=sys.stderr)
        return 2
    raw = os.environ.get("DKITCHEN_BROKER_PROFILES")
    profiles: dict = {}
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                profiles = decoded
        except json.JSONDecodeError:
            profiles = {}
    return Broker(profiles).serve()


if __name__ == "__main__":
    raise SystemExit(main())
