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

## Turn it on: one step

Its options all have working defaults, but its automatic look-ups are on only after one yes from
you: Claude Code asks before any tool runs, and the plugin's own look-ups use the engine's tools,
so until those tools are allowed they do nothing (the session still works, without them), in every
permission mode: the plugin asks Claude Code's decision first and makes no call that would ask. Either answer yes when `aleph-zero setup` asks ("Allow
Claude Code to use Aleph's tools without asking?", or `aleph-zero setup --allow-tools`), or add
this line to `permissions.allow` in `~/.claude/settings.json` yourself (or with `/permissions`):

```json
"mcp__plugin_aleph-zero_aleph__*"
```

The plugin never writes a permission itself. Until it is allowed, the plugin says so once a
session, and `/aleph check` says whether it is.

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
  session; at `3`, ranked locations after a grep (the Grep tool's or a shell's) that found nothing. No rung has yet been
  shown to save tokens in measured sessions.
- `/aleph`: a side chat about the repository whose answers cite `path:line`;
  `/aleph check` and `/aleph stats`.

These look-ups, rungs 2 and 3 and the side chat need the engine's tools allowed (above).

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
| `session.start` | Registers the `/aleph` command and notes the session's working directory. In an interactive session it also starts watching how far vectors are made: it asks the engine's `status` (every 10 s while vectors are being made, every minute otherwise) and shows one short line under the prompt while there is work (`vectors 3,100/8,239 · ~2 min`), cleared when it is done. Nothing is watched in a headless run. |
| `command.run` (`/aleph`) | Opens the side chat pane or the settings pane (`/aleph configure`), or answers `/aleph stats`, `/aleph check` and `/aleph check model`. |
| `ui.render` (the panes) | Draws the side chat and the settings pane. Pressing Insert puts the chosen answer into your prompt as a note, through `prompt.fill`; nothing reaches the conversation otherwise. |
| `ui.focus` (the settings pane) | Notes which row the focus ring is on, to show that row's help under the rows; passes every move on unchanged. |
| `tool.describe` (the engine's tools) | At rung 1 and above, replaces the descriptions of `search`, `symbol`, `outline` and `status` with tuned ones and loads them up front instead of on demand; with `graph` on, does the same for `facts`. |
| `tool.describe` (Grep, Glob) | At rung 1 and above, with `grep_pointer` on, appends one sentence to their descriptions pointing to `search`. It changes nothing else about them. |
| `prompt.compose` | At rung 1 and above, adds one fixed section to the system prompt: search before a recursive grep, and when grep is still right; with the `graph` option on, one more line saying when to use `references`, `decisions` and `facts`. |
| `prompt.submit` | With `graph` on, at rung 1 and above: when the prompt names a route or an environment variable not looked up yet this session, asks the engine's `facts` with the prompt's first 4,000 characters and attaches where the code states them as context. At rung 2 and above, for the first prompt of a session that is a question, also asks the engine's `search` with its first 2,000 characters and, when the top result is confident, attaches up to five file locations. The prompt's own text is not changed. |
| `tool.call` (Bash) | After the command has run. With `graph` on: when it was a recursive `grep`, `rg` or `git grep` whose pattern names a route or an environment variable not looked up yet, asks the engine's `facts` and gives the answer to the model as context after the result. At rung 3: after a recursive `grep`, `rg`, `git grep` or `find` that found nothing, asks the engine's `search` with its pattern and appends up to five ranked locations to that command's output. The command and its input are never changed, and nothing is done for any other command. |
| `tool.call` (Grep) | After the search has run. With `graph` on, the same `facts` look-up for a pattern that names a route or an environment variable; at rung 3, after a search of the repository that found nothing, up to five ranked locations from `search`, as context after the result. The search, its input and its result are not changed. |
| `tool.call` (all tools) | Counts tool calls for `/aleph stats`; passes every call on unchanged. |
| `turn.step`, `turn.complete` | Add up the session's token counts for `/aleph stats` and keep the last session's figures in the plugin store. |
| `session.end` | After `/clear`, resets the one-prompt allowance of rung 2 and the names already looked up, so the new conversation gets them again. |

**Tools the mod calls itself:** only this plugin's own engine, `aleph` on this machine: `facts`
(the routes and environment variables a prompt or a grep names, with `graph` on), `search` and
`symbol` (prompt context at rung 2, suggestions at rung 3, excerpts for the side chat), `remember`
(to keep a side-chat answer in the repository's memory) and `status` (the vectors line under the
prompt, and `/aleph configure`); `/aleph check` sends one test `search`. It calls no other tool and
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

Each finding of the directory's check of 0.1.15, answered (line numbers are `hooks/register.js`'s):

- **Settings it writes** (`config.set`, lines 740–743): only the plugin's own four options, each
  written by its fixed key: `aleph-zero.rung`, `aleph-zero.grep_pointer`, `aleph-zero.lines`,
  `aleph-zero.graph`. Only when the person presses that option's key in `/aleph configure`, as
  `/plugin` would. It never writes a permission, the permission mode or any other setting, and
  sets no environment variable; the `graph` option reaches the engine as `ALEPH_GRAPH` through
  `.mcp.json`, as Claude Code passes any option.
- **Tools it calls itself** (`mcp.call`, line 100): this plugin's own local engine only, the tools
  and times listed above, with arguments it builds itself (a prompt's or a pattern's text, a
  path); nothing it receives is run as a command. Before each call it asks Claude Code's
  permission decision (`tool.check`, which runs nothing and opens no dialog) and makes no call that would ask.
- **What it sends and where** (line 100): to the local engine only, as above. The one request that
  leaves the machine is the side chat's question to Claude, made through Claude Code's own
  `model.complete`, only when the person asks it.
- **Programs it starts** (`process.run`, lines 173 and 230; `process.spawn`, line 269; `process.run` of git, line 779):
  the engine's `status`, run as `sh <plugin>/scripts/engine.sh --cwd <the session's directory>
  status` (the settings pane and the vectors line); from `/aleph configure`, on a key the person
  presses, the engine's `setup --dense`, `--no-dense` or `--auto-dense`, `source add` with the path
  or URL the person typed, `source remove` after a yes; and `git update-index --untracked-cache` or
  `--no-untracked-cache` in the session's directory. `scripts/engine.sh` only finds the separately
  installed `aleph-zero` (below) and runs it with those arguments. The only network use is a source
  the person links by URL, which the engine clones. Nothing fetched is run.
- **Credentials:** the mod reads none, from the environment or from files. The side chat uses
  Claude Code's own model connection (`model.complete`), the host's own vendor. `.mcp.json` passes
  the plugin's `graph` option (`on` or `off`) to the engine as `ALEPH_GRAPH`: an on/off switch,
  not a credential.
- **Changes a tool's input** (`tool.call`, line 938): no. That hook only counts calls for
  `/aleph stats` and passes every event on unchanged (`return next(e)`).
- **Answers in place of a tool** (`tool.call`, lines 1011 and 1034): no. Each first runs the tool
  (`await next(e)`) and returns its own result, with the additions in the table above.
- **Hooks an event that is a call** (`command.run`, line 462): it answers only the plugin's own
  `/aleph` command.
- **Hooks what instructions and descriptions pass through** (`tool.describe`, lines 955 and 965;
  `prompt.compose`, line 971): the changes are the ones in the table above.
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
