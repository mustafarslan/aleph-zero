# Aleph Zero for Claude Code

Local code search for coding agents. This plugin connects Claude Code to the Aleph Zero
engine, which indexes your repository on your machine and answers where something is
handled, where a name is defined, who calls it, and what the project has decided about it.
Answers always match the working tree, uncommitted edits included.

## Install

The plugin starts the engine, which is installed separately:

```sh
uv tool install git+https://github.com/mustafarslan/aleph-zero.git
uv tool update-shell
```

Then, optionally: `aleph-zero setup --worker` once (the embedding server and model), and in each project `aleph-zero setup --embed` for search by meaning and
`aleph-zero setup --source PATH_OR_URL` to search a documentation repository beside it. The
[main README](https://github.com/mustafarslan/aleph-zero#readme) has the full setup.

## What it adds

- The engine's tools: `search`, `symbol`, `outline`, `status`, `recall`, `remember`, and with
  the `graph` option `references`, `facts` and `decisions`.
- Steering, by the `rung` option (default `1`): tool descriptions and a short instruction to
  search before a recursive grep; at `2`, ranked locations with the first prompt of a
  session; at `3`, ranked locations after a grep that found nothing. No rung has yet been
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
- **Stores** the side chat's answers in the repository's memory (`.git/aleph-zero/`), and its
  meter and settings in Claude Code's plugin store.
- **Fetches** only when you ask the engine to: language grammars, the embedding model and
  llama.cpp source (`aleph-zero setup --worker`), and repositories you link by URL.

## License

MIT
