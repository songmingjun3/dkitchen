# dsh-dkitchen

A DeepSeek Harness plugin bundle that puts dkitchen's tools into every session of a
profile.

It is **declarative**: there is no JavaScript here. The bundle inserts one row naming the
harness's own `@deepseek-ai/dsh-mcp-client`, pointed at `ding mcp-serve`. Because that
client ships with the harness, the bundle declares no dependencies and needs no build step.

```yaml
- insert:
    - id: mcp-dkitchen
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dkitchen
        transport: stdio
        command: ding
        args: ['mcp-serve']
```

## What the model gets

Every tool is named `mcp__dkitchen__<name>`:

| Group | Tools |
| --- | --- |
| Sessions | `new_session`, `send`, `read`, `status`, `info`, `rename`, `board`, `approve` |
| Tickets | `relay`, `merge`, `log` (these need the `gh` CLI) |
| Long jobs | `launch`, `logs`, `stop` |

Each one forwards to the matching `ding` command, so a session created from the harness is
the same kind of session as one created from a terminal.

## Install

```bash
ding plugin install            # checks this machine, then installs into `desktop`
ding plugin check              # what it needs, and what is missing
ding plugin install --dry-run  # check only
```

`ding plugin install` runs `dsh plugin --profile <name> add link:<this directory>`, which
installs the package and appends it to the profile's `dsh.profile.bundles`. Restart the
harness afterwards; the **Plugins** page then lists `dkitchen`.

Installing by hand is not supported: the harness's plugin manager owns the profile's
`package.json`, its patch layer, and its lockfile, and a profile edited by hand is one the
manager cannot reconcile later.

## Two deliberate choices

`failOnStartupError: false` — when `ding` is missing or unreachable, the dkitchen tools are
unavailable but the rest of the profile still starts. `ding plugin check` is what catches
that case before installation.

`toolCallTimeoutMs: 180000` — `board` folds every session log in the repository and can
take longer than the 60-second default on a large kitchen.

## The other half

This bundle only mounts the server; the tools live in the Python package. `ding mcp-serve`
speaks MCP on stdin/stdout using the standard library alone, so installing dkitchen adds no
dependency. See [docs/ding.md](../../../docs/ding.md) for the full CLI.
