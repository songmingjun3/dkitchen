# Development

- Everything ships as one Deep​Seek​ Harness plugin, in `src/dkitchen-node/`. There is no
  Python runtime to install and no package to build.
- Run the suite with `node --test "tests/*.test.mjs"` from `src/dkitchen-node/`. There is
  nothing to install first: every module the bundle imports is supplied by the harness as a
  peer dependency.
- The suite runs on Linux and Windows, so keep both supported: a test that needs `tmux`,
  `/proc`, `fcntl`, or a POSIX path is a test that will fail someone's CI.
- Tests must never start the real Deep​Seek​ Harness, send a message to a real session, read a
  real session log, or write to GitHub. Each test that touches state points
  `DKITCHEN_STATE_HOME` (or `DSH_HOME`) at a fresh temporary directory, and nothing in the
  suite shells out to `gh`.
- `node --test` isolates each test file in a child process. Under the Windows sandbox those
  spawns are refused (`spawn EPERM`); run the suite with
  `node --test --experimental-test-isolation=none "tests/*.test.mjs"` there. CI uses the
  default isolation.

## Where things live

| Module | Role |
| --- | --- |
| `src/dkitchen-node/index.js` | Mounting the plugin: the services it injects, config resolution per call, tool registration |
| `src/dkitchen-node/lib/agents.js` | Creating a station session through `ctx.agents`, and delivering a message to it |
| `src/dkitchen-node/lib/tools.js` | The session tools: new, send, read, info, rename, list, board, status |
| `src/dkitchen-node/lib/jobs.js`, `lib/job-tools.js` | Long jobs through `ctx.jobs` and `ctx.subprocess`, plus the `runs.jsonl` record |
| `src/dkitchen-node/lib/tickets.js` | Reading a ticket: its role, status block, and staleness |
| `src/dkitchen-node/lib/ticket-tools.js` | Relay, merge, log, executor status |
| `src/dkitchen-node/lib/watch.js`, `lib/watch-tools.js` | One batch of GitHub activity the orchestrator must handle |
| `src/dkitchen-node/lib/refs.js` | Checking the `file:line` and `sha256` references a draft cites |
| `src/dkitchen-node/lib/registry.js`, `lib/state-tools.js` | dkitchen's own record of the sessions it opened |
| `src/dkitchen-node/lib/config.js`, `lib/config-tools.js` | `.dkitchen.toml`: parsing, refusals, and the surgical writer |
| `src/dkitchen-node/lib/paths.js`, `lib/cwd.js`, `lib/locks.js` | The state root, which repository a call is about, and the one exclusive file lock |
| `src/dkitchen-node/lib/skill.js` | Installing the setup skill once per machine, into `<DSH_HOME>/skills` |
| `src/dkitchen-node/lib/approval.js` | The `[approval]` seam that answers a policy by rule |
| `src/dkitchen-node/lib/github.js` | Every `gh` call, and nothing else |
| `src/dkitchen-node/client.js` | The bundle's section on the harness's Plugins page |
| `src/dkitchen-node/skills/setup-dkitchen/` | The setup skill, and the renderer that needs only Node |
| `src/dkitchen-node/kitchen/` | The station rules the setup skill renders into a repository |
| `scripts/market.mjs` | Generating and validating the plugin market index the Discover tab reads |

The bundle's own README, `src/dkitchen-node/README.md`, is the reference for a user: which
tools exist, what each one needs, and how a repository configures its kitchen.

## The rules a change must keep true

These hold across modules, and a change that breaks one is a bug even when the tests pass:

- **The configuration is resolved per call**, from the session's own working directory — never
  once at mount time. One harness process serves many repositories, and a kitchen belongs to
  a repository.
- **Registration failures are silent.** A schema the registry refuses leaves the process
  running with no tool and no error. `debug: true` in `cordis.patch.yml` is how that becomes
  visible, and it must keep reporting each registration.
- **The output schema names `additionalProperties` explicitly.** A raw `{type: 'object'}` is
  refused as `JsonSchemaError`, which is the silent case above.
- **A refusal is its own answer.** Relay that was not delivered, a merge at an unreviewed
  HEAD, a station the kitchen does not run: each reports why instead of doing something
  adjacent. When a new failure mode appears, give it its own message and a test rather than
  widening a `catch`.

## Changing the harness integration

The plugin mounts through `ctx.agents`, `ctx.sessions`, `ctx.jobs`, `ctx.subprocess`,
`ctx.tools`, `ctx.approval`, `ctx.workspaceRegistry`, and `ctx.sessionTitle`, all named in
`inject` in `index.js`. A harness upgrade that changes one of those surfaces shows up as a
tool that disappears or a call that throws — check `inject` first, then `debug` output.

Two deliberate behaviours are documented in `src/dkitchen-node/README.md` and must survive a
refactor: `[approval]` answers by rule and defers otherwise, and a background job's
settlement arrives as a session notice rather than through polling.
