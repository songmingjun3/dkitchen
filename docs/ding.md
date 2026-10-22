# 🛎️ ding

`ding` is the bell in the kitchen. It starts DeepSeek Harness sessions, lets one session message another, and wakes a session when the long job it started has finished.

Every session it drives is a real DeepSeek Harness session: the harness creates it, persists it, and shows it in its GUI. `ding` adds the kitchen's vocabulary on top — stations, tickets, status blocks, the launch and wake-up loop — and never invents a second notion of "session".

- [Before you start](#before-you-start)
- [A quick tour](#a-quick-tour)
- [Start and stop the broker: `up`, `down`](#start-and-stop-the-broker-up-down)
- [Create a session: `new`](#create-a-session-new)
- [Choosing a session](#choosing-a-session)
- [Answer a permission prompt: `approve`](#answer-a-permission-prompt-approve)
- [Rename a session: `rename`](#rename-a-session-rename)
- [See what everyone is doing: `board`](#see-what-everyone-is-doing-board)
- [Read a session: `read`, `status`, and `info`](#read-a-session-read-status-and-info)
- [Send a message: `send`](#send-a-message-send)
- [Run a long job: `launch`, `logs`, `stop`](#run-a-long-job-launch-logs-stop)
- [Wrap a command yourself: `run`](#wrap-a-command-yourself-run)
- [Stations and models: `config`](#stations-and-models-config)
- [Use dkitchen inside the harness: `plugin`](#use-dkitchen-inside-the-harness-plugin)
- [Install from the page: the plugin market](#install-from-the-page-the-plugin-market)
- [The harness profile: `harness init`](#the-harness-profile-harness-init)
- [Kitchen commands](#kitchen-commands)
- [Files `ding` keeps](#files-ding-keeps)
- [When a wake-up does not arrive](#when-a-wake-up-does-not-arrive)
- [Troubleshooting](#troubleshooting)

## Before you start

You need:

- **DeepSeek Harness** installed, with the `dsh` launcher reachable. On Windows that is the desktop installation under `%LOCALAPPDATA%\Programs\DeepSeek Harness`, which dkitchen finds by itself; elsewhere put `dsh` on `PATH`, or set `DKITCHEN_DSH_BIN` to its full path. Everything below works on Windows and on POSIX systems.
- **Python 3.10 or later.**
- **The `gh` CLI**, installed and logged in, if you want to address sessions by GitHub issue. Every session command except the ticket lookups works without it.
- `tmux` **on POSIX only**, for `ding launch`. On Windows there is no tmux, and `ding launch` runs the job as a detached process instead; `ding logs` and `ding stop` work either way.

Install with pipx:

```bash
pipx install dkitchen
```

Or into a virtual environment:

```bash
python -m venv ~/.venvs/dkitchen
~/.venvs/dkitchen/bin/python -m pip install dkitchen
```

`ding` talks to the harness over the Agent Client Protocol: it starts one `dsh --profile <name>` process and multiplexes every kitchen session over it. That process is owned by a **broker** that `ding` starts on demand, because a harness turn outlives a single command.

## A quick tour

The shortest path is to make dkitchen part of the harness, so its tools appear in every
session:

```bash
ding plugin install                                 # add the bundle to your desktop profile
```

Then, from any harness session, the model can call `mcp__dkitchen__new_session`,
`mcp__dkitchen__board`, `mcp__dkitchen__launch`, and the rest. Nothing else is required:
no profile of dkitchen's own, and no separate broker to start.

From a terminal, the same capabilities:

```bash
ding harness init                                    # only for `ding launch`/`watch` wake-ups
ding up                                              # start the broker
ding new --cwd /work/repo --name 'Evaluation runner' --station executor --message 'Review the evaluation config.'
ding board                                           # sessions in this repo, their tickets and jobs
ding read --head '#42'                               # first paragraph of the latest reply on ticket #42
ding send '#42' --from orchestrator --message 'GPU 5 is free now; go ahead with stage 2.'
ding launch eval-42 -- python evaluate.py            # run in the background; wake me when it exits
```

## Start and stop the broker: `up`, `down`

```bash
ding up                      # start it and print its pid
ding down                    # stop it, closing its sessions first
```

Every command that needs a harness session starts the broker if it is not running, so `ding up` is optional; it exists to make the first failure visible. One broker serves one state directory, so two checkouts with their own `DKITCHEN_STATE_HOME` never share one.

The broker owns **one** `dsh --profile <name>` process. Sessions are created, resumed, and closed through that process, which is why `ding` returns in milliseconds instead of waiting for a turn to end.

## Create a session: `new`

```bash
session_id=$(ding new --cwd /work/repo --name 'Evaluation runner' --station executor --message 'Review the evaluation config.')
printf '%s\n' 'Review the evaluation config.' | ding new --cwd /work/repo --name 'Evaluation runner' --message -
ding new --cwd /work/worktree-57 --issue 57 --name 'Ablation' --message-file brief.md   # named 【#57】Ablation
ding new --cwd /work/repo --name 'Cheap check' --station verifier --provider docode-claude --model claude-sonnet-5 --message 'Check ticket 42.'
```

`--cwd`, `--name`, and one of `--message` or `--message-file` are required. The working directory must exist. The title and first message must contain non-whitespace text; their text is sent unchanged to the harness.

- **`--station`** picks which station's model route the session runs: `orchestrator`, `designer`, `verifier`, or `executor` (the default). The station must be listed in `.dkitchen.toml`; the route comes from `[models.<station>]` and falls back to the harness default.
- **`--provider` / `--model`** override that route for this one session.
- **`--issue N`** starts the session for ticket `#N` in the repository of `--cwd`. `--name` is still required and should be a short title; `ding` fills it into the `[sessions] name` template from [.dkitchen.toml](#dkitchentoml), by default `【#{issue}】{name}`, so `--issue 57 --name 'Ablation'` names the session `【#57】Ablation`. `ding` writes nothing to GitHub. Instead, the first message opens with one paragraph asking the new session to claim the ticket:

```text
You own ticket 917Dhj/my-repo#57. Before anything else, post a comment on it with the line `DSH owner: 3f2a…`.
```

After the harness accepts the session, `new` prints only the session id to stdout and exits with code 0. It does not wait for the reply. Use that id to continue or inspect the session:

```bash
ding status "$session_id"
ding read "$session_id"
ding send "$session_id" --message 'Also check the dataset paths.'
```

If the harness refuses because the model route is unknown, `ding` logs the refusal in the broker journal and still creates the session on the harness's default route; `ding info` then shows the route the session really runs. Use `ding config models` to see the routes this machine offers.

## Choosing a session

Every command that talks to one session (`read`, `status`, `info`, `rename`, `send`, `run`, `approve`) takes a target in any of these forms:

| Target | Example |
| --- | --- |
| Session id | `3f2a1b4c-…` |
| Exact session name, as shown by `board` | `'Evaluation runner'` |
| Issue in the current repo | `'#42'` or `--issue 42` |
| Issue in any repo | `917Dhj/my-repo#42` |
| Issue or PR link | `https://github.com/917Dhj/my-repo/issues/42` |

Quote `'#42'`, or your shell will treat it as a comment. Links to `/pull/42` work too; query strings and `#issuecomment-...` fragments are ignored.

**How an issue finds its session.** `ding` keeps its own record of the session that owns a ticket, and falls back to the ticket itself. Executors sign their issue comments with a line naming their session:

```text
DSH owner: 3f2a1b4c-…        # "this ticket is mine"
DSH session: 3f2a1b4c-…      # "this comment came from me"
```

Tickets written before the move to DeepSeek Harness say `Codex owner:` and `Codex session:`; both spellings resolve, so no old ticket needs rewriting. A new session always writes the `DSH` spelling.

When a name matches several sessions, or none, `ding` shows the candidates instead of guessing.

## Answer a permission prompt: `approve`

The harness asks before it runs a command the session's sandbox does not already allow. Who answers depends on the `approvals` setting in `.dkitchen.toml`:

| `approvals` | What happens |
| --- | --- |
| `"auto"` (the default) | The broker answers at once with the allow option. Nothing waits, and no one has to watch. |
| `"ask"` | The prompt waits for `ding approve`. Until it arrives, that session's turn is paused. |

```bash
ding config set harness.approvals ask   # make prompts wait for a human
ding approve 3f2a1b4c-…                # allow it
ding approve 3f2a1b4c-… --decision reject
ding approve '#42' --decision reject   # a target works like any other command
```

`--decision` is `allow` (the default) or `reject`. With `reject`, the harness is handed the prompt's reject option, so the command does not run. Without one pending prompt the command fails with `no permission request is waiting` rather than pretending to answer.

A prompt nobody answers is **cancelled after an hour**, not allowed: a forgotten `ding approve` must never become a silent yes. A prompt that offers no allow or reject option is cancelled at once, since there is nothing to choose.

`approvals` bounds the bell, not the sandbox: the session's own sandbox mode still decides what it may touch. `auto` is the right setting for unattended kitchens; `ask` is for the first runs in a new repository.

## Rename a session: `rename`

```bash
ding rename 3f2a1b4c-0000-4000-8000-000000000001 --name 'Evaluation runner'
ding rename 'Evaluation runner' --name 'Evaluation complete'
ding rename --issue 42 --name 'Ticket 42 executor'
```

The new name must contain non-whitespace text. On confirmed success, stdout contains only the session id followed by a newline.

The name is dkitchen's own: the harness's ACP surface has no rename, so `ding` records the name beside the session and `board`/`info` read it from there. The DeepSeek Harness GUI keeps showing the title it derived from the conversation; the two can differ. `rename` refuses a session `ding` did not create, rather than writing a record the harness cannot honour.

## See what everyone is doing: `board`

```bash
ding board
```

```text
NAME              CWD                     STATUS  ISSUE  HEAD                          RUN
eval-runner       /work/repo              active  #42    Stage 1 finished.             running
data-cleanup      /work/repo/.wt/issue57  idle    #57    Merged the loader fix.        exited(0)
```

| Column | What it shows |
| --- | --- |
| `NAME` | The name dkitchen gave the session, or its id if it has none |
| `CWD` | The session's working directory |
| `STATUS` | `active` (a turn is running), `idle`, `notLoaded` (the broker has not opened it), or `unavailable` |
| `ISSUE` | The tickets this session owns or has signed comments on |
| `HEAD` | The first paragraph of its latest reply, read from the harness's session log |
| `RUN` | The state of its most recent wrapped job ([states below](#job-states)) |

By default the board shows sessions in the current Git repo and its worktrees (or the current directory outside Git). Options:

- `--all` shows every session in every directory. It is slower.
- `--status` replaces `HEAD` with the status block from the session's latest issue comment. Otherwise it falls back to the reply.
- `--no-start` reads the board without starting a broker, which is what `ding watch` uses.

Below the session table `board` prints a `JOB` table for every job `ding launch` started. On POSIX it then prints a `UNWRAPPED` table of long jobs nobody was woken for; Windows has no `/proc` to read, so that table is absent there.

### Status blocks

A status block is the first few lines of an executor's comment, one field per line, in a fixed order:

```text
Status: running
Waiting on: none
Key numbers: 412/500 samples
Next: finish stage 1, then post results
```

`board --status` shows it only when every field is present, in order, with a non-empty value. The default fields are `Status`, `Waiting on`, `Key numbers`, and `Next`. To use your own, set them once for your environment, or per command:

```bash
export DING_STATUS_FIELDS='状态,在等,关键数字,下一步'
ding board --status --status-fields 'State,Blocked by,Next'
```

`board` only reads. It never resumes a session or retries a message.

### Jobs nobody will be woken for

On POSIX, `board` ends with a second table of long-running processes on this machine that were started without `ding`:

```text
UNWRAPPED_PID  CPU%   AGE_SECONDS  TMUX_SESSION  TMUX_WINDOW  COMMAND
48211          97.3   5420         exp           0            python train.py --config big.yaml
```

When a job finishes, nobody gets woken for it. Restart it with `ding launch`, or tell the session that owns it to check on it.

What counts:

- **Inside tmux**, each pane contributes its main workload once it is older than 10 minutes, whatever its CPU use. Idle shells are skipped.
- **Outside tmux**, a process must also average at least 10% of one CPU core over its lifetime.
- Tune both with `--min-age SECONDS` and `--min-cpu PERCENT` (values over 100 are fine for multicore jobs).

Editors, language servers, the harness itself, tmux, and SSH tunnels are ignored, as is the tmux session named `ssh_tunnel`.

This is a heuristic. Outside tmux it can miss GPU jobs that use little CPU, and it never looks at GPUs or other machines.

## Read a session: `read`, `status`, and `info`

```bash
ding read '#42'            # the latest reply, in full
ding read --head '#42'     # only its first paragraph
ding status '#42'          # the live state, as JSON
ding info '#42'            # id, name, working directory, station, and route, as JSON
```

`read` folds the harness's own session log, which the harness writes whether or not `ding` is running. That is why it works for a session the GUI has open, and why it survives a broker restart. A log written in a newer format than this `dkitchen` understands is reported as unreadable, with the advice to read it in the GUI, rather than parsed into something wrong.

`status` asks the broker and prints `{"type": "active"|"idle"|"notLoaded"|"unavailable", ...}`. `info` prints one line of JSON, for example after `new` or `rename` to check the name, or to see which route a session actually runs:

```json
{"id": "3f2a1b4c-…", "name": "【#42】Ablation", "station": "executor", "cwd": "/work/worktree-42", "status": {"type": "idle"}, "route": {"provider": "docode-claude", "model": "claude-sonnet-5", "effort": "low"}}
```

`info` only reads: it starts no model turn and writes nothing to GitHub.

## Send a message: `send`

```bash
ding send '#42' --from orchestrator --message 'Stage 2 is approved within the ticket budget.'
ding send '#42' --from orchestrator --message-file note.md
printf '%s\n' 'Short note' | ding send '#42' --message -
```

`--from NAME` adds a first line `[from NAME]`, so the receiver knows who is talking. Message files are read as UTF-8.

What happens depends on what the receiver is doing:

| Receiver | Result | Exit code | Meaning |
| --- | --- | --- | --- |
| Idle, or not opened yet | `started` | 0 | `ding` resumed the session if needed and the message began running. |
| Busy | `queued_behind_active` | 0 | The message waits in that session's queue and runs when the current turn ends. |
| Unclear | `queued` or `unknown` | non-zero | Delivery could not be confirmed. **Do not resend yet.** |
| Error | `failed` | non-zero | Nothing was delivered. |

Neither `started` nor `queued_behind_active` means the work is done. Look for the receiver's reply on the ticket.

**Before resending,** check the send log and the receiver's history. Each `send` is logged before delivery, so a blind resend can make the receiver do the work twice. `ding` itself never retries a delivery.

**A session belongs to one process at a time.** If you have the same session open in the DeepSeek Harness GUI, the harness refuses to hand it to the broker, and `ding` reports that instead of fighting for it. Close it there and send again.

Every send is written to the `sent.jsonl` under [the state directory](#files-ding-keeps) before delivery, with its time, sender, target, and full text. The result is appended when known. Issue targets are logged as `OWNER/REPO#42`.

## Run a long job: `launch`, `logs`, `stop`

From inside a session:

```bash
ding launch train -- python train.py --config big.yaml
```

```text
ding: created train via process; record: C:\Users\you\AppData\Local\dkitchen\launch-records\<id>.jsonl; log: C:\Users\you\AppData\Local\dkitchen\launch-records\<id>.log
```

This:

1. starts the command in the background — in tmux session `ding-train` on POSIX, as a detached process on Windows;
2. wraps it with [`ding run`](#wrap-a-command-yourself-run);
3. when the command exits, wakes the session that launched it with `Task train exited; inspect <record path>`.

Then end your turn. The wake-up will come.

```bash
ding logs train              # the job's latest output (log file, or the tmux pane)
ding stop train              # end it, and everything it started
```

Options:

- `--message TEXT`, `--message-file PATH`, or `--message -` set your own wake-up text. `--from NAME` works as in `send`.
- `--record PATH` writes the job's record somewhere you choose.
- `--session ID` names the session to wake; by default it is the calling session (`$DSH_SESSION_ID`).

Things to know:

- **Names** are 1 to 64 letters, digits, `_`, or `-`, starting with a letter or digit. A name already running is an error; `ding` never replaces a live job.
- **The environment** inside the job carries `DSH_SESSION_ID` and `DKITCHEN_RUN_ID` on top of what `ding` itself sees. On POSIX under tmux only `PATH`, `HOME`, `PYTHONPATH`, and the session id are guaranteed, so pass what the job needs explicitly:

  ```bash
  ding launch train -- env CUDA_VISIBLE_DEVICES=0 OMP_NUM_THREADS=8 python train.py
  ```

- **The output** goes to a log file on Windows and to a tmux pane on POSIX; the pane stays open after the job exits so you can read it, and `tmux attach -t ding-train` / `tmux kill-session -t =ding-train` manage it by hand.
- **A successful launch** means the job started, not that it finished.
- **If `launch` exits nonzero,** nothing was started; read the message and retry.

## Wrap a command yourself: `run`

`run` is what `launch` uses in the background. Use it directly when you want to manage the terminal yourself, or to wake a session other than your own:

```bash
ding run '#42' --message 'Training finished; see exit.jsonl' --record "$PWD/exit.jsonl" -- python train.py
ding run '#42' --message 'done' --record exit.jsonl --log job.log -- python train.py   # also capture output
```

Keep the job in the foreground of `run`. When it exits, `run`:

1. writes a `task_exit` event to the record, with the exit code;
2. delivers the message to the target exactly as `send` does;
3. writes a `notification` event with the delivery result;
4. exits with the job's own exit code, whether or not the notification worked.

It never reruns the job. The wrapped job and everything it starts see `DKITCHEN_RUN_ID`, the id of this run. With `--log`, the job's stdout and stderr are pumped into that file as they appear, which is what makes `ding logs` live.

### Job states

`board` reads these records to fill its `RUN` column:

| `RUN` | Meaning |
| --- | --- |
| `running` | The wrapper is alive and the job has not exited. |
| `exited(CODE)` | The job exited with `CODE`, and the wake-up started, was queued behind active work, or is still being sent. |
| `notify_failed` | The job exited, but the wake-up failed or could not be confirmed. Tell the session yourself. |
| `lost` | The wrapper died before the job exited, for example after a reboot or a kill. |
| `unknown` | The record could not be read. `board` prints a warning. |

## Stations and models: `config`

The kitchen's stations and their model routes live in `.dkitchen.toml`, and change without touching rules or restarting anything:

```bash
ding config show                                  # stations, routes, harness settings, signatures
ding config models                                # the provider/model routes this machine offers
ding config disable designer                      # the next `ding new` uses the smaller kitchen
ding config enable designer
ding config set models.verifier.model claude-sonnet-5
ding config set models.verifier.provider docode-claude
ding config set harness.profile lab
```

`enable`/`disable` write `[kitchen] stations`; `set` writes one dotted key, creating the table it needs and keeping everything else. `[models.<station>]` set per-station routes; a station without one inherits `[harness] default_provider` / `default_model`.

## Use dkitchen inside the harness: `plugin`

The harness loads plugins as **bundles**: a package whose `package.json` points at a
`cordis.patch.yml` that inserts rows into the profile's composition. dkitchen ships one.
Its patch inserts the harness's own `@deepseek-ai/dsh-mcp-client` row pointed at `ding
mcp-serve`:

```yaml
- insert:
    - id: mcp-dkitchen
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dkitchen
        transport: stdio
        command: !!js process.env.DKITCHEN_DING || 'ding'
        args: ['mcp-serve']
```

The `!!js` expression is why `DKITCHEN_DING` exists. The harness starts this subprocess from
an environment it scrubs, so a `ding` installed by pipx or into a virtual environment is not
always on that PATH; setting `DKITCHEN_DING` to its full path is how you point at one. The
bare name stays the default, which is what an ordinary install on PATH resolves to.

Because the client ships with the harness, the bundle declares no dependencies. Once
installed, the harness starts `ding mcp-serve` as a subprocess and registers its tools on the
model's tool list:

```bash
ding plugin check                    # what the bundle needs, and what is missing
ding plugin status                   # the same facts as JSON, for the bundle's page
ding plugin install                  # check, then install into the `desktop` profile
ding plugin install --profile lab    # a different profile
ding plugin install --dry-run        # run the checks, change nothing
```

`plugin install` does three things, in this order, so that one command is enough on a machine
that has never run the harness:

1. writes the `dkitchen` profile if it is missing, through `ding harness init`;
2. adds the route bundles that profile needs (`dsh-wtq-pool`, `dsh-llm-agentrouter`,
   `dsh-openai-codex-auth`, `dsh-our-free-model`), because each carries its provider routes
   inside its own patch and a fresh machine has no configured profile to copy them from;
3. installs the bundle into the profile you will open conversations in.

Every tool is named `mcp__dkitchen__<name>`: `new_session`, `send`, `read`, `status`,
`info`, `rename`, `board`, `approve`, `relay`, `merge`, `log`, `launch`, `logs`, and
`stop`. They forward to the same CLI commands documented here, so a session created from
the harness and one created from a terminal are the same kind of session.

Step 3 runs `dsh plugin --profile <name> add link:<bundle dir>`, which installs the package
and appends it to the profile's `dsh.profile.bundles` — the same operation the harness's own
plugin manager performs. It refuses to install when a prerequisite is missing, because a
bundle that cannot reach `ding` produces a plugin whose every tool call fails. `gh` is a
warning, not a refusal: only `relay`, `merge`, and `log` need it. On a machine whose desktop
profile has never been booted, the harness's own refusal ("Open DeepSeek Harness Desktop
once to initialize its profile") is reported with what to do about it.

The bundle sets `failOnStartupError: false` on purpose. A missing `ding` then leaves the
dkitchen tools unavailable instead of preventing the whole profile from starting.

Restart the harness after installing, or reload the profile where HMR is enabled. The
`desktop` profile belongs to the Electron application, which holds a lock on it, so its
composition cannot be dumped with `dsh --profile desktop --dump-config` after installing.
The install itself lands — the dependency, the bundle entry, the `node_modules` link, and
the lockfile entry — and the application's own plugin manager keeps them. Confirm the
`dkitchen` row on the harness's **Plugins** page, where the bundle also shows a section with
the two commands the plugin cannot run for you.

### Install from the page: the plugin market

The harness Plugins page has a **Discover** tab that searches plugin market indexes. dkitchen
publishes one, so the bundle can be found by name instead of pasted as a path:

1. **Plugins** → **More** → switch on **Custom plugin markets** (the harness ships this
   optional bundle off, and there is no official market: Discover lists only the indexes you
   add).
2. **Discover** → **Add index source**, and paste
   `https://raw.githubusercontent.com/917Dhj/dkitchen/main/docs/market/index.json`.
3. Search `dkitchen`, open the entry, and press **Install**.

The index is format 1, the format `@deepseek-ai/dsh-plugin-market-index` validates. Each
version names a pinned commit of this repository rather than a registry package, so an
install clones that commit and takes the bundle from `src/dkitchen/harness/dsh-dkitchen`.
Generate or check it with:

```bash
ding market index                      # rewrite docs/market/index.json for HEAD
ding market index --check              # validate the committed file
ding market index --commit <sha> --published <unix>   # what CI passes for a tag
```

Because the index describes a published commit, `ding plugin install` remains the way to
install the working tree you are editing: the market entry cannot point at uncommitted files.


## The harness profile: `harness init`

**You only need this for `ding launch` and `ding watch`**, which wake a session by sending
it a message; that needs a harness profile dkitchen owns. Installing the [plugin](#use-dkitchen-inside-the-harness-plugin)
does not: the harness starts `ding mcp-serve` itself and drives it over MCP.

```bash
ding harness init                 # write $DSH_HOME/profiles/dkitchen
ding harness init --force         # rewrite it
```

The kitchen profile is not the bare ACP template. It starts from the profile you already run (its plugin bundles and its provider routes), swaps the web application for the ACP one, and reuses that profile's `node_modules` instead of downloading a second copy of every plugin. That is what gives the stations the models you already have sign-ins for.

`dsh --profile dkitchen --help` is run at the end: if it fails, the profile is not usable and `ding` says so.

## Kitchen commands

These commands carry out the kitchen's routine steps, as the house rules in [src/dkitchen/kitchen/](../src/dkitchen/kitchen/) describe them. They read the repo's vocabulary from [`.dkitchen.toml`](#dkitchentoml).

| Command | Who runs it | What it does |
| --- | --- | --- |
| `ding relay 42 --message TEXT [--comment TEXT]` | Orchestrator | Sends the message to ticket #42's executor and, only once delivery is confirmed, leaves it (or `--comment`) on the ticket, signed. `--to SESSION` picks the executor when the ticket has no owner line; `--no-comment` only sends. |
| `ding merge 57 a1b2c3d [--ticket 42]` | Orchestrator | Merges PR #57 only if its HEAD is still the reviewed `a1b2c3d`, it is not a draft, and the ticket's executor is waiting on the orchestrator to merge. Then it confirms the merge and prints the merge commit. |
| `ding log --item TEXT [--pending TEXT]` | Orchestrator | Posts a signed, UTC-stamped entry to the orchestrator log ticket: one line per `--item`, then the matters waiting for the user. |
| `ding watch` | Orchestrator, in the background | Waits for GitHub activity the orchestrator must handle, prints it as one batch, and exits. See [While the user is away](../src/dkitchen/kitchen/modules/unattended.md). |
| `ding check-refs draft.md` | Verifier | Checks every `` `file:line` `` in a draft comment (a name written right next to it must appear on that line) and every `sha256`, against `origin/main` or `--root DIR`. Exits 1 when something is wrong. |
| `ding skill install` | Setup | Copies dkitchen's setup skill into `<repo>/.agents/skills/setup-dkitchen/`. |
| `ding plugin install` | Setup | Checks this machine, writes the kitchen profile, adds the route bundles, then installs the harness plugin bundle into a profile. See [Use dkitchen inside the harness](#use-dkitchen-inside-the-harness-plugin). |
| `ding plugin status` | Setup, indirectly | The same facts as `check`, as JSON. The bundle's page in the harness reads it. |
| `ding market index` | Release | Writes or validates the format-1 plugin market index the harness's Discover tab reads. See [Install from the page](#install-from-the-page-the-plugin-market). |
| `ding mcp-serve` | Setup, indirectly | Serves dkitchen's tools over MCP on stdin/stdout. The plugin's `cordis.patch.yml` names it; you do not run it by hand. |

**Before merging, `ding merge` reads the executor's latest status block.** The ticket is `--ticket N`. Without it, `ding` takes the one ticket mentioned in the PR body (as `#N` or a link) that has executor comments, and refuses when there are none or several. It prints that ticket's latest executor status block, and merges only when its second field (*Waiting on*) names the orchestrator. An executor that has since said it is still changing the PR, or that waits on nobody, blocks the merge, even if the review already passed. Use `--no-ticket` for a PR that belongs to no ticket. There is no override: when the executor cannot answer, the user merges by hand.

`ding watch` keeps its place in the state directory, so rerunning the same command after handling a batch loses nothing. Only one watch runs per state file. It always skips the orchestrator log ticket; add `--skip-issue N` for others. Run `ding watch --help` for the timing options.

### .dkitchen.toml

Put this file at the repo root to change the kitchen's words, its stations, and their routes. Every key is optional; these are the defaults:

```toml
repo = "owner/name"                  # default: the repository gh sees here

[kitchen]
stations = ["orchestrator", "designer", "verifier", "executor"]   # the stations this kitchen runs

[harness]
profile = "dkitchen"                 # the DeepSeek Harness profile ding boots
default_provider = "deepseek-official"
default_model = "deepseek-v4-flash"
approvals = "auto"                   # auto answers permission prompts; ask waits for `ding approve`
permission = "workspace-write"       # DSH_PERMISSION_MODE for the booted profile

[models.executor]                    # one table per station; every key optional
provider = "docode-claude"
model = "claude-sonnet-5"
effort = "high"                      # reasoning_effort, when the model declares one

[stations]
orchestrator = "Orchestrator"
designer = "Designer"
verifier = "Verifier"
executor = "Executor"
user = "User"
separator = ": "                     # written after a station name to sign a comment

[status]
fields = ["Status", "Waiting on", "Key numbers", "Next"]   # first: the state; second: who it waits on
in_progress = "in progress"
waiting_on_design = "waiting on design"
blocked = "blocked"
done = "done"
none = "none"                        # a "Waiting on" value that names nobody

[labels]
prefix = "status:"
awaiting_user = "status:awaiting-user"

[log]
issue = 1                            # the pinned orchestrator log ticket
pending = "Waiting for the user"

[sessions]
owner_line = "DSH owner"             # the line that claims a ticket
session_line = "DSH session"         # the line that signs a comment
name = "【#{issue}】{name}"           # session name for `ding new --issue N --name NAME`
```

`[sessions] name` may use `{issue}`, `{name}` (the `--name` text), and `{repo}` (`OWNER/REPO`), with an optional format spec such as `{issue:04d}`. Any other field is an error when the file is read. The `--name` text is filled in as is, so braces in it are kept. `ding new` without `--issue` and `ding rename` use `--name` unchanged.

`owner_line` and `session_line` change what new comments say; the old `Codex` spellings are always read too, so a repository can migrate one ticket at a time.

`ding board --status` uses `[status] fields`, after `--status-fields` and `DING_STATUS_FIELDS`. A misspelled key is an error, so a typo cannot silently fall back to a default.

## Files `ding` keeps

Everything lives under one state directory, readable only by you: `$DKITCHEN_STATE_HOME` if set, else `$XDG_STATE_HOME/ding`, else `%LOCALAPPDATA%\dkitchen` on Windows, else `~/.local/state/ding`.

| Path | Contents |
| --- | --- |
| `sent.jsonl` | Every `send`: what was sent, to whom, and the result |
| `sessions.json` | dkitchen's record of the sessions it created: name, station, ticket, route |
| `broker.jsonl` | The broker's journal: connections, created sessions, route refusals, turns, recoveries |
| `broker.log` | The harness process's own stderr |
| `runs.jsonl` | One `run_start` line per wrapped job |
| `launch-jobs.jsonl` | One line per `ding launch`: pid, record, log, backend |
| `launch-records/<id>.jsonl` | Default record for a launch: `task_exit` and `notification` events |
| `launch-records/<id>.log` | The job's own output; `<id>.wrapper.log` holds the wrapper's diagnostics |
| `watch/OWNER-REPO.json` | Where `ding watch` left off |
| `run-locks/<run id>.lock` | Held while a wrapper is alive; this is how `board` tells `running` from `lost` |

A `runs.jsonl` line looks like this:

```json
{"event":"run_start","run_id":"…","session_id":"…","record":"/abs/exit.jsonl","pid":1234,"started_ns":1791799200000000000,"cwd":"/abs/launch/dir"}
```

The harness's own session logs live where DeepSeek Harness puts them, `$DSH_HOME/sessions/`, and `ding` only reads them.

## When a wake-up does not arrive

A job wakes its session exactly once, after it exits. You will not get that wake-up if:

- the wrapper itself was killed, or the machine rebooted (`board` shows `lost`);
- the job is stuck and never exits (`board` still shows `running`);
- delivery failed (`board` shows `notify_failed`; the record says why).

Don't schedule follow-up messages in advance to cover these cases. Check `ding board`, read the record, and decide from there.

## Troubleshooting

| Symptom | What it means and what to do |
| --- | --- |
| `cannot find the DeepSeek Harness launcher` | Install DeepSeek Harness, put `dsh` on PATH, or set `DKITCHEN_DSH_BIN`. |
| `no broker is running` | `ding up`, or let the next session command start it. If it will not start, read `broker.log` in the state directory. |
| `session … is owned by another DeepSeek Harness process` | The GUI has that session open. Close it there, or keep working in the GUI. |
| `ding read` says the log uses a newer format | The log was written by a newer DeepSeek Harness than this dkitchen knows. Read it in the GUI. |
| `config models` lists only `deepseek-official` | The kitchen profile did not inherit your providers. Rerun `ding harness init --force`, then `ding up`. |
| A session runs a different route than `[models]` says | The route was refused (see `broker.jsonl`, event `route_refused` or `route_unavailable`). `ding config models` lists what is actually offered. |
| `ding board` shows `notLoaded` for everything | Nothing is wrong: no turn is running and the broker has not opened those sessions. `send` resumes them on demand. |
