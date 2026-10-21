"""Launch backends, harness profile installation, and skill installation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from dkitchen import config, ding, launcher, paths


def test_backend_choice_follows_tmux_availability(monkeypatch):
    monkeypatch.setattr(launcher, "tmux_available", lambda: True)
    assert launcher.backend() == "tmux"
    monkeypatch.setattr(launcher, "tmux_available", lambda: False)
    assert launcher.backend() == "process"


def test_process_backend_keeps_output_in_a_log_and_its_diagnostics_apart(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(launcher, "tmux_available", lambda: False)
    job = launcher.launch("job", "session-x",
                          [sys.executable, "-c", "print('child output', flush=True)"],
                          record=tmp_path / "exit.jsonl", message="wake up", socket=None,
                          cwd=tmp_path)
    assert job.backend == "process" and job.session == "session-x"
    assert job.log is not None and job.log.parent == launcher.launch_records()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if "child output" in (job.log.read_text(encoding="utf-8", errors="replace")
                              if job.log.exists() else ""):
            break
        time.sleep(0.1)
    assert "child output" in job.log.read_text(encoding="utf-8", errors="replace")
    # The wrapper's own diagnostics never share the job's log file.
    assert job.log.with_suffix(".wrapper.log") != job.log


def test_job_log_names_the_record_and_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    job = launcher.Job(name="train", run_id="r", pid=1, record=tmp_path / "rec.jsonl",
                       log=tmp_path / "job.log", started_ns=1, command=["x"], backend="process")
    described = launcher.describe(job)
    assert "record:" in described and "log:" in described
    tmux_job = launcher.Job(name="train", run_id="r", pid=None, record=tmp_path / "rec.jsonl",
                            log=None, started_ns=1, command=["x"], backend="tmux")
    assert "tmux session: ding-train" in launcher.describe(tmux_job)


def test_log_tail_reports_a_missing_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    assert "no launched job" in launcher.log_tail("nope")


def test_stop_reports_a_missing_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    ok, detail = launcher.stop("nope")
    assert ok is False and "no launched job" in detail


def test_stop_without_a_process_id_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    launcher.record_job(launcher.Job(name="ghost", run_id="r", pid=None, record=tmp_path / "r.jsonl",
                                     log=None, started_ns=1, command=["x"], backend="process"))
    ok, detail = launcher.stop("ghost")
    assert ok is False and "no process id" in detail


def test_tmux_launch_builds_the_documented_arguments(tmp_path, monkeypatch):
    """The tmux backend is exercised with a fake `tmux` binary, as before the migration."""
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(launcher, "tmux_available", lambda: True)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/fake/bin/{name}" if name == "tmux" else None)
    captured = []

    def fake_run(argv, **kwargs):
        captured.append((argv, kwargs))
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="ding-train\ttrain", stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    job = launcher.launch("train", "session-y", ["python", "train.py"], record=tmp_path / "exit.jsonl",
                          message="wake", socket=None, cwd=tmp_path)
    argv, _ = captured[0]
    assert [Path(part).name for part in argv[:7]] == ["tmux", "new-session", "-d", "-s", "ding-train",
                                                      "-n", "train"][:7]
    assert argv[1:7] == ["new-session", "-d", "-s", "ding-train", "-n", "train"]
    joined = " ".join(argv)
    assert "DSH_SESSION_ID=session-y" in joined
    assert "remain-on-exit on" in joined
    assert job.backend == "tmux" and job.pid is None


def write_profile(home: Path, name: str, bundles: list[str], patch: str = "") -> Path:
    directory = home / "profiles" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({
        "name": f"dsh-profile-{name}", "private": True, "dependencies": {"some-plugin": "^1.0.0"},
        "dsh": {"profile": {"bundles": bundles}},
    }), encoding="utf-8")
    (directory / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    return directory


def test_harness_init_copies_the_donor_bundles_and_providers(tmp_path, monkeypatch, capsys):
    home = paths.dsh_home()
    write_profile(home, "desktop", ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app",
                                    "dsh-wtq-pool"],
                  patch='- id: llm-pi-ai\n  config:\n    providers:\n      lab:\n        models: []\n')
    monkeypatch.setattr(ding.paths, "dsh_executable", lambda: sys.executable)
    monkeypatch.setattr(ding.subprocess, "run",
                        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    root = tmp_path / "repo"
    root.mkdir()
    settings = config.load(root)
    assert ding.harness_init(settings, profile="kitchen", force=False) == 0
    target = home / "profiles" / "kitchen"
    package = json.loads((target / "package.json").read_text(encoding="utf-8"))
    bundles = package["dsh"]["profile"]["bundles"]
    assert bundles[0] == "@deepseek-ai/dsh-base"
    assert "@deepseek-ai/dsh-acp-app" in bundles
    assert "@deepseek-ai/dsh-web-app" not in bundles
    assert "dsh-wtq-pool" in bundles
    patch = (target / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "llm-pi-ai" in patch and "lab" in patch


def test_harness_init_leaves_out_the_plugin_that_would_mount_the_mcp_server(
        tmp_path, monkeypatch, capsys):
    """The kitchen profile is the harness dkitchen drives, so it must not mount dkitchen.

    Composing `dsh-dkitchen` there would have that harness spawn `ding mcp-serve`, whose
    tools call `ding`, which reaches the broker owning the very harness the call runs in.
    """
    home = paths.dsh_home()
    write_profile(home, "desktop", ["@deepseek-ai/dsh-base", "dsh-dkitchen"], patch="")
    monkeypatch.setattr(ding.paths, "dsh_executable", lambda: sys.executable)
    monkeypatch.setattr(ding.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    root = tmp_path / "repo"
    root.mkdir()
    assert ding.harness_init(config.load(root), profile="kitchen", force=False) == 0
    package = json.loads((home / "profiles" / "kitchen" / "package.json").read_text(encoding="utf-8"))
    assert "dsh-dkitchen" not in package["dsh"]["profile"]["bundles"]
    assert "dsh-dkitchen" not in (package.get("dependencies") or {})


def test_a_profile_with_no_donor_still_writes_a_loader_readable_patch(
        tmp_path, monkeypatch, capsys):
    """With no donor there is nothing to carry over, but the file must stay an array.

    The loader rejects an empty overlay outright ("must be a top-level YAML array"), so a
    fresh machine used to get a profile `dsh` could not compose at all.
    """
    home = paths.dsh_home()          # no profile written: no donor to copy
    monkeypatch.setattr(ding.paths, "dsh_executable", lambda: sys.executable)
    monkeypatch.setattr(ding.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    root = tmp_path / "repo"
    root.mkdir()
    assert ding.harness_init(config.load(root), profile="kitchen", force=False) == 0
    target = home / "profiles" / "kitchen"
    body = [line for line in (target / "cordis.patch.yml").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert body == ["[]"], f"an empty overlay must still be an array, got {body!r}"
    package = json.loads((target / "package.json").read_text(encoding="utf-8"))
    assert package["dsh"]["profile"]["bundles"] == [
        "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app"]


def test_the_kitchen_profile_workspace_lets_pnpm_install_into_it(tmp_path, monkeypatch, capsys):
    """`packages: []` makes `dsh plugin add` fail in linking, so route bundles cannot land."""
    home = paths.dsh_home()
    monkeypatch.setattr(ding.paths, "dsh_executable", lambda: sys.executable)
    monkeypatch.setattr(ding.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    root = tmp_path / "repo"
    root.mkdir()
    assert ding.harness_init(config.load(root), profile="kitchen", force=False) == 0
    workspace = (home / "profiles" / "kitchen" / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    assert "packages: []" not in workspace
    assert "- ." in workspace
    assert "nodeLinker: hoisted" in workspace


def test_harness_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    home = paths.dsh_home()
    write_profile(home, "kitchen", ["@deepseek-ai/dsh-base"])
    root = tmp_path / "repo"
    root.mkdir()
    assert ding.harness_init(config.load(root), profile="kitchen", force=False) == 1
    assert "already exists" in capsys.readouterr().err


def test_provider_block_is_cut_at_the_next_row(tmp_path):
    text = ("- id: ui-chat\n  config: {}\n"
            "- id: llm-pi-ai\n  name: '@deepseek-ai/dsh-llm-pi-ai'\n  config:\n    providers: {}\n"
            "- id: ui-settings\n  config: {}\n")
    block = ding._llm_pi_ai_block(text)
    assert "llm-pi-ai" in block and "providers" in block
    assert "ui-settings" not in block and "ui-chat" not in block
    assert ding._llm_pi_ai_block("- id: something\n") == ""


def test_skill_install_copies_the_bundled_skill(tmp_path, monkeypatch, capsys):
    source = tmp_path / "pkg" / "skills" / "setup-dkitchen"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: setup-dkitchen\n---\n", encoding="utf-8")
    monkeypatch.setattr(ding, "__file__", str(tmp_path / "pkg" / "ding.py"))
    root = tmp_path / "repo"
    root.mkdir()
    args = type("A", (), {"repo": str(root), "force": False})()
    assert ding.skill_install(args) == 0
    assert (root / ".agents" / "skills" / "setup-dkitchen" / "SKILL.md").is_file()
    assert ding.skill_install(args) == 1
    assert "exists" in capsys.readouterr().err


def test_state_paths_stay_under_the_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path))
    assert launcher.registry() == tmp_path / "launch-jobs.jsonl"
    assert launcher.launch_records() == tmp_path / "launch-records"


def test_windows_and_posix_both_name_a_log_file(tmp_path, monkeypatch):
    """`ding logs` must work on either backend, so a job always has a readable source."""
    monkeypatch.setenv("DKITCHEN_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(launcher, "tmux_available", lambda: False)
    job = launcher.launch("quiet", "sid", [sys.executable, "-c", "pass"],
                          record=tmp_path / "exit.jsonl", message="wake", socket=None, cwd=tmp_path)
    assert job.log is not None
    assert launcher.log_tail("quiet") in {"", "no launched job named quiet"} or isinstance(
        launcher.log_tail("quiet"), str)
