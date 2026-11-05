# Aleph Zero for Claude Code

Local code search for coding agents. This plugin connects Claude Code to the Aleph Zero
engine, which indexes your repository on your machine and answers where something is
handled, where a name is defined, who calls it, and what the project has decided about it.
Answers always match the working tree, uncommitted edits included.

## Install

The plugin starts the engine, which is installed separately:

```sh
uv tool install git+https://github.com/mustafarslan/aleph-zero.git
uv tool update-shell        # then open a new terminal
cd path/to/your/project && aleph-zero setup
```

`aleph-zero setup` asks about each part once and does it on a yes: this plugin (it runs Claude
Code's own `claude plugin marketplace add` and `claude plugin install`), allowing its tools (below),
the optional embedding worker and memory by meaning, and documentation from another repository.
The [main README](https://github.com/mustafarslan/aleph-zero#readme) has the full setup.

## Permissions

Claude Code asks before any tool runs. With the `allow_reads` option on (the default), the plugin
answers that question itself for the engine's **read-only** tools (`search`, `symbol`, `outline`,
`status`, `facts`, `references`, `decisions`, `recall`): where Claude Code would ask you, they run
without a dialog, for the agent and for the plugin's own look-ups. It writes no setting to do so,
and a deny rule you wrote still stands. `remember`, which writes to the repository's memory, is
still asked. Before 0.1.21 nothing was allowed until you said so, and in one long session the
look-ups never ran.

With `allow_reads` off, every engine tool needs your yes, as for any MCP server: answer yes when
`aleph-zero setup` asks ("Allow Claude Code to use Aleph's tools without asking?", or
`aleph-zero setup --allow-tools`), or add this line to `permissions.allow` in
`~/.claude/settings.json` (or with `/permissions`):

```json
"mcp__plugin_aleph-zero_aleph__*"
```

Until the tools are allowed, the plugin's own look-ups do nothing (the session still works without
them). The plugin says so once a session, and `/aleph check` says whether they are allowed.

## What it adds

- The engine's tools: `search`, `symbol`, `outline`, `status`, `recall`, `remember`, and with
  the `graph` option (on by default) `facts` (shown in full), `references` and `decisions`.
- With `graph` on, at any rung from 1: when a prompt, a Grep or a recursive grep names an HTTP
  route (`GET /api/users/{id}`) or an environment variable (`DATABASE_URL`), the plugin asks the
  engine's `facts` where the code registers and handles that route (its handler's definition
  included) or reads that variable, and gives it to the model as context. Each name once a
  session; a text that names nothing costs nothing.
- Steering, by the `rung` option (default `1`): tool descriptions and a short instruction to
  search before a recursive grep; at `2`, ranked locations with the first prompt of a
  session; at `3`, ranked locations after a grep (the Grep tool's or a shell's) that found nothing
  (with `grep_assist` set to `first`, also beside the first locating grep of each turn and each subagent).
  No rung has yet been shown to save tokens in measured sessions.
- Subagents, with the `subagents` option (on by default): a subagent is given only its task, so
  the instruction above never reached it. The plugin adds the same instruction after each
  subagent's brief, and at rung 2 and above the locations the engine ranks highest for the task.
  Not measured yet.
- `/aleph`: a side chat about the repository whose answers cite `path:line`;
  `/aleph check`, and `/aleph stats` (below).

The rung is the plugin's option in your user settings (`/aleph configure`, `/plugin`). A CLAUDE.md
cannot set it, and a project's `.claude/settings.json` did not either while the user settings gave
another rung (measured with Claude Code 2.1.318). When either names a rung other than the one the
plugin runs at, the plugin says so once.

These look-ups, rungs 2 and 3 and the side chat need the engine's tools allowed (above).

## Is it used? `/aleph stats`

`/aleph stats` counts, for this conversation, the main loop and its subagents apart:

- how often code was located with Aleph (`search`, `symbol`, `facts`, `references`) against a
  locating grep (a recursive grep, rg, git grep, `find -name` or a Grep tool search of the
  repository; a grep of one file or of a command's output is not one);
- in how many turns and subagents Aleph was the first to locate something;
- how many reads went to a file Aleph named, or one the plugin added, and how often a grep came
  right after Aleph (its answer was not enough);
- what the plugin added, by kind; calls by feature (retrieval, architecture, memory, side chat);
- how the engine answered the plugin's own look-ups (time, timeouts, answers from an index still
  catching up with edits).

`/aleph stats last` shows the conversation before, `/aleph stats all` every conversation of this
session, and `/aleph stats report` writes the full report as Markdown and JSON to
`.aleph/reports/` in the repository (a `.gitignore` there keeps it out of git).

`/aleph advise` turns those figures into suggestions about Aleph's own setup, each with the figure that triggered it,
whether what it promises was measured, what applying it does and what it costs: rung 2 when subagents locate code
mostly by grep (at least five subagents and 20 locating calls, under a quarter with Aleph); rung 3 when at least 8
locating greps, a fifth of them, found nothing; search by meaning when its benchmark says it helps for the repository's
language and a grep followed Aleph's answer in a quarter of at least 15 cases (also when you turned it off before, and
then it says so); git's untracked cache when the engine is slow on a large worktree; a rebuild of what is out of date;
memory by meaning when memory is used. `/aleph advise apply N` applies one (a plugin option in your user settings, an
engine command for this repository, or a build's command copied for a terminal); `/aleph advise dismiss N` stops
suggesting it. Nothing is changed otherwise. When a suggestion is new, you get a notice, and the agent is told once that
`/aleph advise` has something for you, never a command or a setting. Never suggested: linked sources, the switches kept
for measurements, `allow_reads`, or turning anything down.

These are counts of use, not of savings. What a session would have cost without Aleph can only
be measured by running the same tasks with and without it; the report quotes those
measurements (ADR-011) beside the counts and claims nothing more.

## What it runs, sends and fetches

- **Runs** `scripts/serve.sh`, which starts the locally installed `aleph-zero serve` (an MCP
  server on standard input and output). The engine runs `git` in your repository, and a
  local llama.cpp embedding server when search by meaning is on.
- **Sends** nothing anywhere. Code, index, vectors and memories stay on your machine. The side
  chat uses Claude Code's own model connection.
- **Stores** the side chat's answers and the notes you keep from it in the repository's
  memory (`<git-common-dir>/aleph-zero/`, or `~/.cache/aleph-zero/` where that cannot be
  written), and its meter and settings in Claude Code's plugin store.
- **Fetches** only when you ask the engine to: language grammars, the embedding model and
  llama.cpp source (`aleph-zero setup --worker`), and repositories you link by URL.

## How the plugin works

The plugin has one MCP server (`aleph`, the local engine) and one mod, `hooks/register.js`.
These are all the mod's hooks, and everything each one does:

| Hook | What it does |
|---|---|
| `session.start` | Registers the `/aleph` command and notes the session's working directory. In an interactive session it reads `.claude/settings.json`, `.claude/settings.local.json`, `CLAUDE.md` and `.claude/CLAUDE.md`, only to say once when they name a rung other than the one the plugin runs at, and it also starts watching how far vectors are made: it asks the engine's `status` (every 10 s while vectors are being made, every minute otherwise) and shows one short line under the prompt while there is work (`vectors 3,100/8,239 · ~2 min`), cleared when it is done. Nothing is watched in a headless run. |
| `command.run` (`/aleph`) | Opens the side chat pane or the settings pane (`/aleph configure`), or answers `/aleph stats` (`last`, `all`, `report`), `/aleph advise` (`apply N`, `dismiss N`), `/aleph check` and `/aleph check model`. `advise apply` writes only one of the plugin's own options by its fixed key (`rung` to 2 or 3, `subagents` on), runs the engine's own `setup --dense` or `setup --untracked-cache` for the session's repository, or copies a build's command for a terminal. `/aleph stats report` writes the report to `.aleph/reports/` in the session's directory, and `.aleph/.gitignore`. |
| `ui.render` (the panes) | Draws the side chat and the settings pane. Pressing Insert puts the chosen answer into your prompt as a note, through `prompt.fill`; nothing reaches the conversation otherwise. |
| `ui.focus` (the settings pane) | Notes which row the focus ring is on, to show that row's help under the rows; passes every move on unchanged. |
| `tool.describe` (the engine's tools) | At rung 1 and above, replaces the descriptions of `search`, `symbol`, `outline` and `status` with tuned ones and loads them up front instead of on demand; with `graph` on, does the same for `facts`. |
| `tool.describe` (Grep, Glob) | At rung 1 and above, with `grep_pointer` on, appends one sentence to their descriptions pointing to `search`. It changes nothing else about them. |
| `prompt.compose` | At rung 1 and above, adds one fixed section to the system prompt: search before a recursive grep, and when grep is still right; with the `graph` option on, one more line saying when to use `references`, `decisions` and `facts`. |
| `prompt.submit` | When a suggestion of `/aleph advise` is new this session: one sentence as context telling the agent that `/aleph advise` has something for the person, with no command or setting in it, and a notice for the person. With `graph` on, at rung 1 and above: when the prompt names a route or an environment variable not looked up yet this session, asks the engine's `facts` with the prompt's first 4,000 characters and attaches where the code states them as context. At rung 2 and above, for the first prompt of a session that is a question, also asks the engine's `search` with its first 2,000 characters and, when the top result is confident, attaches up to five file locations. The prompt's own text is not changed. |
| `tool.call` (Bash) | After the command has run. With `graph` on: when it was a recursive `grep`, `rg` or `git grep` whose pattern names a route or an environment variable not looked up yet, asks the engine's `facts` and gives the answer to the model as context after the result. At rung 3: after a recursive `grep`, `rg`, `git grep` or `find` that found nothing, asks the engine's `search` with its pattern and appends up to five ranked locations to that command's output. The command and its input are never changed, and nothing is done for any other command. |
| `tool.call` (Grep) | After the search has run. With `graph` on, the same `facts` look-up for a pattern that names a route or an environment variable; at rung 3, after a search of the repository that found nothing, up to five ranked locations from `search`, as context after the result. The search, its input and its result are not changed. |
| `tool.call` (all tools) | Counts tool calls for `/aleph stats`, by the loop they ran in (the main conversation or a subagent's type), after the call has run; passes every call and its result on unchanged. |
| `agent.spawn` | With `subagents` on, at rung 1 and above: appends the instruction to locate code with `search` after the subagent's task (the brief itself is not changed), and at rung 2 and above asks the engine's `search` with the task's first 2,000 characters and, when the top result is confident, appends up to five file locations. Forks, resumed runs and workflow agents are left alone. Notes the subagent's type for the meter. |
| `tool.check` (the engine's read-only tools) | With `allow_reads` on: where Claude Code's own decision would ask, answers allow. A deny stands; `remember` and every other tool are left to Claude Code. |
| `turn.step`, `turn.complete` | Add up the session's token counts for `/aleph stats`, end each turn's and subagent's count, and keep the last conversation's figures in the plugin store. |
| `session.end` | After `/clear`, resets the one-prompt allowance of rung 2 and the names already looked up, so the new conversation gets them again, and starts the conversation's meter again (the session's total is kept for `/aleph stats all`). |

**Tools the mod calls itself:** only this plugin's own engine, `aleph` on this machine: `facts`
(the routes and environment variables a prompt or a grep names, with `graph` on), `search` and
`symbol` (prompt context at rung 2, suggestions at rung 3, excerpts for the side chat), `remember`
(to keep a side-chat answer in the repository's memory) and `status` (the vectors line under the
prompt, and `/aleph configure`); `search` for a subagent's task at rung 2; `/aleph check` sends one test `search`.
Its look-ups ask with `stale_ok`: the engine answers from what it has indexed within the time, while it
catches up with the session's edits, instead of reporting the answer late. It calls no other tool and
never stands in for a tool: every tool call goes on to Claude Code and its real result is
returned, with the additions above.

**What the mod sends, and where:**

- To the local engine (`aleph-zero serve`, on this machine): the text of a prompt or a grep
  pattern for `facts` and `search`, and side-chat answers to keep. Nothing the engine receives
  leaves the machine.
- To Claude, through Claude Code's own model connection (as any Claude Code request): your
  side-chat question with excerpts of the matching code, and for `/aleph check model` one short
  test request. Nothing is sent when you do not use the side chat.
- To Claude Code's plugin store, on this machine: the session meter and the side chat's model
  and effort setting.

### For the directory's review

Each finding of the directory's check of 0.1.15, answered, and what 0.1.21 added (line numbers are `hooks/register.js`'s):

- **Settings it writes** (`config.set`, lines 514–520): only the plugin's own seven options, each
  written by its fixed key: `aleph-zero.rung`, `aleph-zero.grep_pointer`, `aleph-zero.lines`,
  `aleph-zero.graph`, `aleph-zero.subagents`, `aleph-zero.grep_assist`, `aleph-zero.allow_reads`.
  Only when the person presses that option's key in `/aleph configure`, as
  `/plugin` would, or types `/aleph advise apply N` for a suggestion that sets `rung` (to 2 or 3) or `subagents` (on). It never writes a permission, the permission mode or any other setting, and
  sets no environment variable; the `graph` option reaches the engine as `ALEPH_GRAPH` through
  `.mcp.json`, as Claude Code passes any option.
- **Permission decisions it makes** (`tool.check`, line 1228): with `allow_reads` on (the default),
  it answers allow for the engine's eight read-only tools where Claude Code's own decision is to
  ask. It never turns a deny into an allow, never answers for any other tool (`remember`, which
  writes, included), and writes nothing to the settings. With `allow_reads` off it does nothing.
- **Tools it calls itself** (`mcp.call`, line 117): this plugin's own local engine only, the tools
  and times listed above, with arguments it builds itself (a prompt's, a subagent task's or a pattern's text, a
  path); nothing it receives is run as a command. Before each call it asks Claude Code's
  permission decision (`tool.check`, which runs nothing and opens no dialog) and makes no call that would ask.
- **What it sends and where** (line 117): to the local engine only, as above. The one request that
  leaves the machine is the side chat's question to Claude, made through Claude Code's own
  `model.complete`, only when the person asks it.
- **Programs it starts** (`process.run`, lines 239 and 298; `process.spawn`, line 338; `process.run` of git, line 1004):
  the engine's `status`, run as `sh <plugin>/scripts/engine.sh --cwd <the session's directory>
  status` (the settings pane and the vectors line); from `/aleph configure`, on a key the person
  presses, the engine's `setup --dense`, `--no-dense` or `--auto-dense`, `source add` with the path
  or URL the person typed, `source remove` after a yes; from `/aleph advise apply N`, typed by the person, the
  engine's `setup --dense` or `setup --untracked-cache`; and `git update-index --untracked-cache` or
  `--no-untracked-cache` in the session's directory. `scripts/engine.sh` only finds the separately
  installed `aleph-zero` (below) and runs it with those arguments. The only network use is a source
  the person links by URL, which the engine clones. Nothing fetched is run.
- **Credentials:** the mod reads none, from the environment or from files. The side chat uses
  Claude Code's own model connection (`model.complete`), the host's own vendor. `.mcp.json` passes
  the plugin's `graph` option (`on` or `off`) to the engine as `ALEPH_GRAPH`: an on/off switch,
  not a credential.
- **Changes a tool's input** (`tool.call`, line 1180): no. That hook runs the call unchanged
  (`await next(e)`), counts it for `/aleph stats`, and returns the call's own result.
- **Answers in place of a tool** (`tool.call`, lines 1305 and 1337): no. Each first runs the tool
  (`await next(e)`) and returns its own result, with the additions in the table above.
- **Changes a subagent's task** (`agent.spawn`, line 1196): with `subagents` on, it appends one
  paragraph after the task, and at rung 2 up to five file locations from the engine; the task as
  written comes first and is not changed. Forks, resumed runs and workflow agents are left alone,
  and it never refuses a spawn or changes its model, type or directory.
- **Files it reads and writes** (`fs.read`, lines 496 and 628; `fs.write`, lines 504–506): it reads
  its own `plugin.json` for its version, and the session's `.claude/settings.json`, `.claude/settings.local.json`,
  `CLAUDE.md` and `.claude/CLAUDE.md` only to compare a rung named there with the one it runs at. It writes only on `/aleph stats report`, typed by the
  person: the report under `.aleph/reports/` in the session's directory and `.aleph/.gitignore`.
- **Hooks an event that is a call** (`command.run`, line 676): it answers only the plugin's own
  `/aleph` command.
- **Hooks what instructions and descriptions pass through** (`tool.describe`, lines 1236 and 1246;
  `prompt.compose`, line 1252): the changes are the ones in the table above.
- **`types` in `plugin.json`:** the mod's type contract, read by `claude plugin validate` for the
  values it keeps in `$.state`; Claude Code ignores it when loading, as the check says.
- **Scripts** (`scripts/serve.sh`, `scripts/engine.sh`): they start the separately installed
  engine. `engine.sh` looks, in order, for `aleph-zero` in a checkout's `.venv` (when the plugin is
  loaded from a clone of this repository), on `PATH`, and in uv's tool directory
  (`$UV_TOOL_BIN_DIR`, `$XDG_BIN_HOME` or `~/.local/bin`), and runs it with the arguments it was
  given; `serve.sh` runs `engine.sh serve`. Neither runs a program called `aleph`, because TeX Live
  installs one. They install nothing and download nothing.

## License

MIT
