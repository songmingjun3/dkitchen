# Development

- Install with `python -m pip install -e '.[dev]'`.
- Run `python -m pytest` after code changes. The suite runs on Linux and Windows, so keep
  both supported: no `fcntl`, `os.getuid`, `/proc`, or `tmux` outside a guarded branch.
- Tests must never start the real DeepSeek Harness, send a message to a real session, read a
  real session log, or write to GitHub. `tests/conftest.py` enforces the first three: it
  points `DKITCHEN_DSH_BIN` at `tests/fake_dsh.py`, gives each test its own state root and
  `DSH_HOME`, forbids starting a detached broker, and fails a test that launches the
  installed harness.

## Where things live

| Module | Role |
| --- | --- |
| `src/dkitchen/acp.py` | The Agent Client Protocol client: one `dsh` process, many sessions |
| `src/dkitchen/broker.py` | The long-lived process that owns that connection, with one queue per session |
| `src/dkitchen/logs.py` | Read-only folding of harness session logs, for `read` and `board` |
| `src/dkitchen/registry.py` | dkitchen's own record of the sessions it created (names, stations, routes) |
| `src/dkitchen/launcher.py` | Long jobs: tmux on POSIX, a detached process on Windows |
| `src/dkitchen/locks.py` | The one cross-platform exclusive file lock |
| `src/dkitchen/paths.py` | State root, harness home, and locating the `dsh` launcher |
| `src/dkitchen/ding.py` | The CLI |
| `src/dkitchen/skills/setup-dkitchen/` | The setup skill, shipped in the package and installed by `ding skill install` |
| `src/dkitchen/kitchen/` | The station rules the setup skill renders into a repository, shipped as package data |
| `src/dkitchen/mcp.py` | The MCP server on stdio, which turns the CLI into tools a harness session can call |
| `src/dkitchen/plugin.py` | Installing the `dsh-dkitchen` bundle through the harness's own launcher |
| `src/dkitchen/harness/dsh-dkitchen/` | The plugin bundle: a manifest, a `cordis.patch.yml`, locale files, and an icon |

## Changing the harness integration

The ACP surface is documented in `acp.py`'s module docstring, including which methods
dkitchen relies on and why. When a DeepSeek Harness upgrade breaks something, the failure is
meant to be one of:

- `AcpUnavailable` — the process is gone; the broker reconnects and resumes sessions;
- `SessionBusy` — another process owns the session (typically the GUI);
- `Unreadable` from `logs.py` — a session log in a newer format, reported rather than guessed.

Keep those three distinct. A new failure mode should get its own class and a test, not a
broader `except`.
