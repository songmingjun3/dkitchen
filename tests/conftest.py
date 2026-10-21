"""Shared test fixtures: an isolated state root and a fake DeepSeek Harness.

No test may start the real harness, reach a real session log, or post to GitHub. The fixture
below gives every test its own state directory and its own `dsh` launcher that runs
`tests/fake_dsh.py`, so the ACP client is exercised over real pipes against a scripted server.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

import pytest

TESTS = Path(__file__).resolve().parent
FAKE_SERVER = TESTS / "fake_dsh.py"

#: The pytest scratch root, recorded so fixtures can recognise it without guessing names.
TEST_ROOT_ENV = "DKITCHEN_TEST_ROOT"


def pytest_configure(config):
    """Record where pytest keeps this run's temporary directories."""
    basetemp = config.option.basetemp or str(Path(tempfile.gettempdir()) / "pytest-of-temp")
    os.environ[TEST_ROOT_ENV] = str(Path(basetemp).resolve())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """A private state root, harness home, and launcher for every test."""
    state = tmp_path / "state"
    home = tmp_path / "dsh-home"
    (home / "profiles" / "dkitchen").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(state))
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.delenv("DSH_SESSION_ID", raising=False)
    launcher = _write_launcher(tmp_path)
    monkeypatch.setenv("DKITCHEN_DSH_BIN", str(launcher))
    monkeypatch.chdir(tmp_path)
    yield {"state": state, "home": home, "root": tmp_path, "launcher": launcher}


@pytest.fixture(autouse=True)
def own_repository_root(monkeypatch):
    """Treat a test directory as its own repository root.

    The test suite usually runs inside a checkout, so `git rev-parse --show-toplevel` from a
    temporary directory climbs back into the checkout and every `config.load(tmp)` would pick
    up the developer's own `.dkitchen.toml`. Real invocations keep the climbing behaviour;
    only a pytest scratch directory gets a boundary at itself.
    """
    from dkitchen import config

    real = config.repository_root
    roots = [Path(tempfile.gettempdir()).resolve(),
             Path(os.environ.get(TEST_ROOT_ENV) or "").resolve()]

    def bounded(cwd=None):
        resolved = Path(cwd or Path.cwd()).resolve()
        if any(resolved.is_relative_to(root) for root in roots):
            return resolved
        return real(cwd)

    monkeypatch.setattr(config, "repository_root", bounded)
    yield


def _write_launcher(directory: Path) -> Path:
    """A `dsh` shim that runs the fake ACP server and ignores the profile flags."""
    if sys.platform == "win32":
        target = directory / "dsh.cmd"
        target.write_text("@echo off\r\n"
                          f'"{sys.executable}" "{FAKE_SERVER}" %*\r\n', encoding="utf-8")
    else:
        target = directory / "dsh"
        target.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(FAKE_SERVER)!r} \"$@\"\n",
                          encoding="utf-8")
        target.chmod(0o755)
    return target


@pytest.fixture
def fake_launcher(isolated_home):
    """The fake launcher path, for tests that start it themselves."""
    return isolated_home["launcher"]


@pytest.fixture(autouse=True)
def no_detached_broker(monkeypatch):
    """Never start a real detached broker from a test.

    A detached broker keeps its log file open, so the test's temporary directory cannot be
    removed on Windows, and it would outlive the test. Tests that need a broker run one in a
    thread; anything else sees "not running", which is what an unstarted broker reports.
    """
    monkeypatch.setenv("DKITCHEN_BROKER_AUTOSTART", "0")
    from dkitchen import broker

    def refuse(profiles):
        pytest.fail("a test tried to start a detached broker; use the served/live fixtures")

    monkeypatch.setattr(broker, "_spawn", refuse)


@pytest.fixture(autouse=True)
def no_real_subprocess_escape(monkeypatch):
    """Fail loudly if a test tries to launch the real harness or the real gh."""
    import subprocess

    run = subprocess.run
    popen = subprocess.Popen

    def guard(argv):
        if not isinstance(argv, (list, tuple)) or not argv:
            return
        program = str(argv[0]).lower()
        blocked = ("deepseek harness", "\\resources\\runtime\\cli\\bin\\dsh")
        if any(part in program for part in blocked):
            pytest.fail(f"tests must not start the real harness: {argv!r}")

    def guarded_run(argv, *args, **kwargs):
        guard(argv)
        return run(argv, *args, **kwargs)

    def guarded_popen(argv, *args, **kwargs):
        guard(argv)
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
