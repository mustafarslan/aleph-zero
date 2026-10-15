# Changelog

## 0.1.4

The side chat (`/aleph`), from its first use by a person.

- A question no longer reads as part of the last excerpt: once it was taken for a line of a README and answered as
  file content. Excerpts and the question each have tags of their own, which their text cannot open or close.
- Answers come directly, in the first person, about the repository, instead of about "the excerpts". The small
  model (the default) still does so now and then; the larger ones follow it better.
- It answers questions about Aleph itself (linking documentation, search by meaning, its own settings) from a
  short guide, and says which it means when a question could be about Aleph or the repository.
- The pane says how to close it and how to come back, and has keys: c closes, m and e change model and effort
  (Esc first leaves the question field).

## 0.1.3

From the plugin's run in Claude Code sessions (nine headless sessions, haiku).

- Rung 3 also follows the Grep tool, the search an agent uses most: when a Grep search of the repository finds
  nothing, the ranked locations reach the model as context after the result. Before, only a grep run in the shell
  had them.
- `/aleph check` with the engine not running says the usual reason (the engine is not installed) and how to
  install it; `/mcp` shows the server's own error.

## 0.1.2

From an end-to-end run as a new user (install from GitHub, every command, offline, update, uninstall).

- `aleph setup --worker` works from any directory. In a new terminal, which starts in the home directory,
  it refused the home directory and installed nothing: the first command of the install failed.
- Files read while their grammar could not load (offline, behind a proxy) are read again once it loads.
  Before, they kept no definitions until each file changed: `symbol` and `callers` stayed empty. They
  stay searchable by words until their new rows are stored.
  A command on a terminal says when a grammar could not load, and says before it fetches one; only the
  grammars a repository needs are fetched.
- The model download names its source and size, shows percent, speed and time left, replaces a
  connection that stays slow (the CDN's speed differs between connections: 40 KB/s and 2.9 MB/s were
  measured for the same bytes) or ends early, never throws away what came, and resumes a part left
  whole. Two installs at once wait for each other. A yes in the menu says what it installed.
- `aleph setup`: a no to the worker where vectors do not help no longer silences it in repositories
  where they do; with the plugin installed, Claude Code counts as connected and no second server is
  suggested; the plugin is offered first.
- Run as `aleph-zero`, Aleph names `aleph-zero` in what it tells you to run (TeX Live has an `aleph`).
- Ctrl-C, a closed pipe and file errors end in one line instead of a traceback; `--cwd` is also taken
  after the command; empty answers say so; `embed setup --model-file` checks the file before building.
- The plugin's `graph` option is on with `true` or `yes` as well as `on`.
- When the cache path is too deep for a socket, each cache gets its own worker socket under /tmp. A worker
  of 0.1.1 still running there on update is not seen by 0.1.2: a second one starts, and the old one stops
  by itself after thirty idle minutes.
- The engine named in printed `claude mcp add` and git-hook lines is `aleph-zero`, never `aleph`.
- `aleph setup --worker` outside a git repository installs the worker without walking the directory.
- Dependencies locked at the versions a new install resolves (apsw 3.54, tree-sitter-language-pack 1.22.1).

## 0.1.1

- The plugin's `graph` option is free text (`on` or `off`), as Anthropic's plugin directory requires for an option passed
  to the MCP server.
- The plugin folder has an icon, and its README describes each hook, the tools the plugin calls and what it sends where.

## 0.1.0

First public release.

- Hybrid code search (full text with identifier splitting, definitions by name, paths, and
  optional search by meaning with a local embedding model), always matching the working tree.
- Definitions and outlines for Python, TypeScript, JavaScript, Go, Rust, Java, C++ and C.
- Code graph (callers and callees, certain or candidate), with SCIP compiler indexes.
- Architecture facts, decision records, linked sources, and memory across sessions.
- Claude Code plugin, installable from this repository's marketplace, with steering levels
  and a side chat; MCP server for other clients.
- Search by meaning on by default for JavaScript, TypeScript, C, C++ and Rust, by benchmark
  ([ADR-002](docs/adr/ADR-002-lexical-and-fusion.md)).
