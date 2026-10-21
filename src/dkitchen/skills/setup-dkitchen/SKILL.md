---
name: setup-dkitchen
description: Install the dkitchen multi-agent workflow into this repo, adapted to its work, its resources, and the user's preferences. Run once per repo, or again to change the setup.
disable-model-invocation: true
---

# Set up dkitchen

Install the kitchen into the current repo: house rules under `docs/agents/`, the pointer block in `AGENTS.md`, the repo's `.dkitchen.toml`, and repo scripts under `scripts/`. Every station is a DeepSeek Harness session, opened by `ding` and visible in the harness GUI. The user is the head chef: every rule you install exists to keep their attention on direction and decisions, so tailor the rules to what this repo really does rather than copying them.

This is a prompt-driven setup. Explore, present what you found, settle each section with the user, show the draft, then write. Nothing is written or posted before the user approves the draft in step 4.

## Sources

- **Templates**: the `kitchen/` folder inside the installed `dkitchen` package, `../../kitchen/` from this skill's directory. Read [kitchen/README.md](../../kitchen/README.md) first: it maps each template to its installed path and defines the template markers, the station list, and the default vocabulary. The templates ship inside the package, so `render.py` finds them with no checkout; if it cannot, pass `--kitchen DIR`.
- **Reference repo scripts**: [scripts/](scripts/) in this skill. Each starts with a comment listing what to adapt.
- **Renderer**: [render.py](render.py) in this skill turns the templates into rule files for a chosen set of modules.
- **`ding`**: `ding --help`, and the guide at https://github.com/917Dhj/dkitchen/blob/main/docs/ding.md, including every `.dkitchen.toml` key.
- **The harness**: `ding config models` lists the provider/model routes this machine can run, and `ding info` shows the route one session uses.

## 1. Explore

Read the repo's starting state; assume nothing.

- **GitHub**: `git remote -v` and `gh repo view`. The kitchen runs on GitHub issues, so a repo without a GitHub remote stops here: tell the user, and offer to continue once one exists. `gh` must be installed and logged in (`gh auth status`).
- **Harness**: `ding --version` (0.5.0 or later), `dsh --version`, `$DSH_HOME` (default `~/.dsh`), the profiles under `$DSH_HOME/profiles`, and whether the kitchen profile exists (`ding harness init` writes it). Confirm `ding up` starts the broker and `ding config models` lists at least one route.
- **Existing setup**: `AGENTS.md`, `CLAUDE.md`, `.claude/CLAUDE.md`, `docs/agents/`, `.dkitchen.toml`, `.dsh/skills/`, `.agents/skills/`. A `<!-- dkitchen:start -->` block or a `.dkitchen.toml` means dkitchen is already installed: this run updates it in place. Files from other setups, such as Matt Pocock's `docs/agents/issue-tracker.md`, are merged, never overwritten.
- **The work**: the README, the main code, how tests run (CI files, `pyproject.toml`, `package.json`, `Makefile`), and the signals for each module in [Modules](#modules): model calls and paid APIs, prompts, training or evaluation scripts, output folders other work builds on, long jobs.
- **Resources**: `nvidia-smi` or the equivalent, other hosts mentioned in docs or SSH config, shared services such as model servers and the tunnels to them. On Windows note whether `tmux` exists: without it, `ding launch` runs jobs as detached processes and `ding logs` / `ding stop` replace the tmux pane.
- **Language**: the language the user writes to you in, and the language of the repo's docs.

Done when you can name, with evidence, each prerequisite's state, every existing file you would touch, the repo's test command, and a recommendation for each module.

## 2. Present the findings

Summarize what is present and what is missing in a short list. Missing prerequisites come first, each with the command that fixes it. When the harness is missing or `ding config models` lists no route, say plainly that no station could run, and ask whether to stop or to install the rules anyway.

## 3. Settle each section

Take the sections in order: one section, one answer, then the next. Lead each with your recommendation so the user can accept it in a word, and skip any section that exploration already settled.

**A. Language.** Recommend the language the user writes to you in. For any language other than English, the installed rules are written in that language, and the vocabulary in [kitchen/README.md](../../kitchen/README.md#vocabulary) (station names, signature separator, status fields and values, the word for nobody, the log's pending label) is translated once and used identically in the rules and in `.dkitchen.toml`. Show the translated vocabulary as a table and get it confirmed: scripts match these words exactly.

**B. Stations.** This is the first choice of the kitchen, and the one the user can change later without editing rules. Ask which stations to open, as a multi-select from `orchestrator`, `designer`, `verifier`, `executor`:

- the full kitchen (all four) suits research repos and anything the user wants to run while away;
- the minimal kitchen is `orchestrator` + `executor`: the orchestrator also writes tickets and interprets results, and runs the verifier's checks itself;
- `designer` without `verifier`, or the reverse, is allowed: say what each absence moves into the orchestrator.

Whatever is chosen is written as `[kitchen] stations`, and the rules are rendered to match: with no designer, rewrite the designer's duties into the orchestrator's and drop the `Type:` reply format; with no verifier, fold the checkpoints into the orchestrator's list.

**C. Models per station.** For each chosen station, propose a route from `ding config models`, with the reasoning: the orchestrator and designer want the strongest model the user has, the verifier a cheaper one, and executors the model that is best at long tool-driven runs. Show the table (station · provider/model · reasoning effort) and write the confirmed rows as `[models.<station>]`. Set only the routes that differ from `[harness] default_provider`/`default_model`; a station without its own table inherits the default.

**D. What stays with the user.** Show the default list from the roles template (research direction, how datasets are used, what the paper claims, spending beyond a ticket's budget) and ask what to add or remove. For a repo that is not a research project, rewrite the first three in the repo's own terms.

**E. Modules.** Recommend each module from the signals you found, as a multi-select:

| Module | Recommend when |
| --- | --- |
| `run-budgets` | The repo runs models on GPUs or calls paid APIs |
| `model-text` | The repo studies or depends on what models are told: prompts, tool definitions, request templates |
| `artifacts` | Outputs such as prepared datasets or result packs are built on by later work and must stay frozen |
| `unattended` | The user wants the kitchen to keep moving while they are away |

**F. Resources.** Fill the `<!-- fill: … -->` gaps of the chosen templates: hosts and GPUs and how to tell one is free, shared services with their owners and tunnels, the pre-approved run limit for `run-budgets` (default 500 samples or calls), the artifact root for `artifacts`, where prompts live for `model-text`. Propose values from exploration and let the user correct them.

**G. Repo scripts.** Propose how `scripts/review_pr.py` runs this repo's tests and prepares a trial-merge worktree, and which module scripts to install: `check_requests.py` and `check_prompt_in_issue.py` for `model-text`. For `run-budgets`, a ledger script only if the repo already saves per-request receipts; otherwise drop the ledger paragraph.

Done when every section has an answer from the user, or was settled by exploration and you said so.

## 4. Draft and confirm

Show the user:

- the list of files to create or change, with the full text of each new `docs/agents/` file and a diff for each existing file;
- the `.dkitchen.toml`, holding `[kitchen] stations`, the `[models.<station>]` tables that differ from the default, only keys that differ from the defaults elsewhere, plus `[log] issue` for `unattended`;
- the adapted repo scripts;
- the GitHub changes: the four `status:*` labels from [kitchen/core/issue-tracker.md](../../kitchen/core/issue-tracker.md), and for `unattended` a pinned "Orchestrator log" ticket;
- the harness steps: `ding harness init`, `ding skill install`, and one `ding new --station <name>` per station, with the session id each prints.

Let the user edit anything before you write. Done when the user approves the draft, including each GitHub change by name.

## 5. Write

Apply exactly the approved draft.

**Rules.** Render the templates with this skill's [render.py](render.py), which keeps the chosen modules' marked text, drops the rest, removes every module marker, and prints the pointer block:

```bash
python <this skill's base directory>/render.py --modules run-budgets,unattended --out docs/agents
```

Then edit the rendered files:

- Fill every `<!-- fill: … -->` with the answers from section F. Where a gap does not apply, such as a repo with no tunnels or shared services, remove the sentence or section around it, and reword the sentences that still mention it.
- For a language other than English, translate every file, using the confirmed vocabulary word for word.
- For a kitchen without the designer or the verifier, rewrite that station's duties into the orchestrator's as settled in section B, and drop the sections that only concern them.

**Pointer block.** Wrap the `AGENTS.md` block from [kitchen/core/agents-block.md](../../kitchen/core/agents-block.md) in `<!-- dkitchen:start -->` and `<!-- dkitchen:end -->`, replacing an existing block in place; create `AGENTS.md` if needed, because DeepSeek Harness loads it for every session. If the repo already keeps a `CLAUDE.md`, replace its content with `@AGENTS.md` inside the same markers so the two files cannot drift; do not create one otherwise.

**Settings and scripts.** Write `.dkitchen.toml` with `[kitchen] stations`, the `[models.<station>]` tables, and the rest of the approved keys; write the repo scripts; then make the approved GitHub changes with `gh label create` and, for `unattended`, `gh issue create` and `gh issue pin`, writing the new ticket's number into `[log] issue`.

**Harness.** Run `ding harness init` if the kitchen profile does not exist yet, then `ding skill install` so this skill lives in `<repo>/.agents/skills/setup-dkitchen/` for later runs, then open each station's session:

```bash
ding new --cwd . --name '<station>' --station orchestrator --message-file .dkitchen-brief-orchestrator.md
```

The first message tells the station to read `AGENTS.md` and `docs/agents/roles.md`, and, for the orchestrator, to run `ding board` before reporting. Write those brief files, create the sessions, then delete the briefs and record each session id in the table at the top of `docs/agents/roles.md`.

Done when every approved file exists with its approved content, every approved GitHub change is made, and each station's session id is recorded in `docs/agents/roles.md`.

## 6. Verify

Run each check and fix what fails:

- `grep -rn '<!-- module:\|<!-- /module:\|<!-- fill:' docs/agents AGENTS.md CLAUDE.md .claude/CLAUDE.md` finds nothing.
- Every relative link in the installed files resolves to an existing file.
- `ding config show` prints the chosen stations and routes, and every `[models.<station>]` names a route `ding config models` offers.
- `ding up` starts the broker; `ding board` lists one session per station; `ding info <session>` shows the route you intended.
- `ding watch --help`, `ding relay --help`, and `ding merge --help` print help.
- Each installed script runs with `--help`, and `scripts/review_pr.py` imports cleanly.
- `gh label list --search status:` shows the four labels.

Done when every check passes, or the user has been told exactly which one fails and why.

## 7. Hand over

Tell the user, briefly:

1. that every station is a DeepSeek Harness session they can open in the GUI to watch, while `ding` keeps driving it;
2. to put those session names into the table at the top of `docs/agents/roles.md` if step 5 did not;
3. how to start: describe the first piece of work to the designer, who writes it as a ticket;
4. for `unattended`, that the orchestrator starts `ding watch` in the background at the end of each round;
5. that the rules are theirs to edit, and that stations and models change without touching them: `ding config enable designer`, `ding config set models.verifier.model <id>`, or rerun this skill.

## Modules

Signals to look for in step 1:

- `run-budgets`: GPU training or evaluation scripts, API keys or clients for model providers (OpenAI, Anthropic, vLLM endpoints), cost or token accounting.
- `model-text`: prompt files or folders, request templates, tool or function schemas, code that renders chat messages.
- `artifacts`: an `outputs/` or `results/` folder referenced by later scripts, dataset preparation stages, digests or manifests of outputs.
- `unattended`: long jobs, several parallel workstreams, or the user saying they want progress while away.
