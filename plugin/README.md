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
```

Then, optionally: `aleph-zero setup --worker` once (the embedding server and model), and in each project `aleph-zero setup --embed` for search by meaning and
`aleph-zero setup --source PATH_OR_URL` to search a documentation repository beside it. The
[main README](https://github.com/mustafarslan/aleph-zero#readme) has the full setup.

## What it adds

- The engine's tools: `search`, `symbol`, `outline`, `status`, `recall`, `remember`, and with
  the `graph` option `references`, `facts` and `decisions`.
- Steering, by the `rung` option (default `1`): tool descriptions and a short instruction to
  search before a recursive grep; at `2`, ranked locations with the first prompt of a
  session; at `3`, ranked locations after a grep (the Grep tool's or a shell's) that found nothing. No rung has yet been
  shown to save tokens in measured sessions.
- `/aleph`: a side chat about the repository whose answers cite `path:line`;
  `/aleph check` and `/aleph stats`.

Rungs 2 and 3 and the side chat need the engine's tools allowed: add
`"mcp__plugin_aleph-zero_aleph__*"` to `permissions.allow` in your Claude Code settings.

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
| `session.start` | Registers the `/aleph` command and notes the session's working directory. |
| `command.run` (`/aleph`) | Opens the side chat pane, or answers `/aleph stats`, `/aleph check` and `/aleph check model`. |
| `ui.render` (the pane) | Draws the side chat. Pressing Insert puts the chosen answer into your prompt as a note, through `prompt.fill`; nothing reaches the conversation otherwise. |
| `tool.describe` (the engine's tools) | At rung 1 and above, replaces the descriptions of `search`, `symbol`, `outline` and `status` with tuned ones and loads them up front instead of on demand. |
| `tool.describe` (Grep, Glob) | At rung 1 and above, with `grep_pointer` on, appends one sentence to their descriptions pointing to `search`. It changes nothing else about them. |
| `prompt.compose` | At rung 1 and above, adds one fixed section to the system prompt: search before a recursive grep, and when grep is still right. |
| `prompt.submit` | At rung 2 and above, for the first prompt of a session that is a question, asks the engine's `search` with the prompt's first 2,000 characters and, when the top result is confident, attaches up to five file locations to that prompt as context. The prompt's own text is not changed. |
| `tool.call` (Bash) | At rung 3, after a recursive `grep`, `rg`, `git grep` or `find` has run and found nothing, asks the engine's `search` with its pattern and appends up to five ranked locations to that command's output. The command and its input are not changed, and nothing is done for any other command. |
| `tool.call` (Grep) | At rung 3, after a Grep tool search of the repository has found nothing, asks the engine's `search` with its pattern and gives up to five ranked locations to the model as context after the result. The search, its input and its result are not changed. |
| `tool.call` (all tools) | Counts tool calls for `/aleph stats`; passes every call on unchanged. |
| `turn.step`, `turn.complete` | Add up the session's token counts for `/aleph stats` and keep the last session's figures in the plugin store. |
| `session.end` | Resets the one-prompt allowance of rung 2 after `/clear`. |

**Tools the mod calls itself:** the engine's own `search` and `symbol` (prompt context at rung 2,
suggestions at rung 3, excerpts for the side chat), `remember` (to keep a side-chat answer in
the repository's memory); `/aleph check` sends one test `search`. It calls no other tool, runs no
shell command, and never stands in for a tool: every tool call goes on to Claude Code and its
real result is returned, with the rung 3 addition above.

**What the mod sends, and where:**

- To the local engine (`aleph-zero serve`, on this machine): search queries made from your
  prompt or a grep pattern, and side-chat answers to keep.
- To Claude, through Claude Code's own model connection (as any Claude Code request): your
  side-chat question with excerpts of the matching code, and for `/aleph check model` one short
  test request. Nothing is sent when you do not use the side chat.
- To Claude Code's plugin store, on this machine: the session meter and the side chat's model
  and effort setting.

### For the directory's review

Each finding of the directory's validation, answered:

- **MOD_CALLS_TOOLS, MOD_CAN_FETCH_AND_RUN** (`mcp.call`, line 43): the mod's only outbound call is
  `$.mcp.call` to this plugin's own server, `aleph`, which runs on the user's machine. It calls
  `search`, `symbol` and `remember` there, at the times listed above, with arguments it builds
  itself; nothing it receives is run as a command. It runs no shell command. Its only network
  traffic is the side chat's request to Claude, made through Claude Code's own `model.complete`.
- **MOD_SESSION_DATA_LEAVES** (`prompt.submit`): at rung 2 the first 2,000 characters of the
  session's first question go to the local engine's `search`, and nowhere else.
- **MOD_LOCAL_DATA_LEAVES** (`session.model`): the session's model name is read only when the side
  chat is set to "same as the session", to ask Claude Code for that model in `model.complete`. It
  is not sent to the engine.
- **MOD_REWRITES_TOOL_INPUT** (`tool.call`, line 391): this hook only counts calls for
  `/aleph stats`; it reads the tool's name and, for Bash, the command, and passes the event on
  unchanged (`return next(e)`).
- **MOD_ANSWERS_FOR_TOOL** (`tool.call`, line 461): the Bash command always runs first; the hook
  returns the command's own result, with ranked locations appended to its output only at rung 3
  and only when a recursive search found nothing.
- **MOD_HOOKS_OPERATION** (`command.run`): the hooks answer only the plugin's own `/aleph` command.
- **MOD_HOOKS_POLICY_EVENT** (`tool.describe`): the changes are the ones in the table above: tuned
  descriptions of the engine's four search tools, and one sentence added to Grep's and Glob's.
- **COMMAND_SCRIPT_NOT_FOLLOWED** (`scripts/serve.sh`): it starts the separately installed engine.
  It looks, in order, for `aleph-zero` in a checkout's `.venv` (when the plugin is loaded from a
  clone of this repository), on `PATH`, and in uv's tool directory (`$UV_TOOL_BIN_DIR`,
  `$XDG_BIN_HOME` or `~/.local/bin`), and runs `aleph-zero serve`. It never runs a program called
  `aleph`, because TeX Live installs one. It installs nothing and downloads nothing.

## License

MIT
