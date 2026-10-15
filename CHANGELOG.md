# Changelog

## 0.1.9

- The graph tools are on by default in the plugin: `references` (who calls a function, and what it calls), `facts`
  (routes, data models, environment names) and `decisions` (the decision records that govern a change) are offered
  to the agent, and the standing instruction says when to use each. Whether they help an agent has not been measured;
  `graph=off` (in `/aleph configure`, or `/plugin`) offers the search tools alone. A setup without the plugin still
  opts in with `ALEPH_GRAPH=1`.
- Benchmark C has its hand-made part: 68 questions on six of the owner's public repositories, made so that nobody who
  built Aleph wrote, answered or edited one (`bench/eval/owner_questions.py`, `--set owner`), read by kind.

## 0.1.8

- `aleph-zero status` in a directory Aleph refuses (a home directory, more than 20,000 files) still refuses, and
  now says what is built on this machine beside the refusal: `/aleph configure` keeps its Machine rows there.
- `status` no longer looks for or fetches tree-sitter grammars on a repository not indexed yet: it reads nothing,
  so the download bought nothing.
- `status` says where git's untracked cache comes from. Kept by git's configuration (`core.untrackedCache`),
  `/aleph configure` shows it with no key, since only a change of that configuration turns it off.
- `aleph-zero setup` run again on a terminal with every step answered says the list once and names
  `setup --again`, which asks again about what was declined.
- Two kept tests: `setup`'s menu on a real pseudo-terminal, and the wheel installed with `uv tool install` and used.

## 0.1.7

- Changing a plugin option in `/aleph configure` no longer clears the side chat's conversation or this session's
  `/aleph stats`: Claude Code loads the plugin again with the new value, and what it had counted and said is now kept
  for the session and read back. A question still being answered at that moment is kept as stopped.

## 0.1.6

- `/aleph configure` opens Aleph's settings in a pane, each changed with one key. The plugin's options (steering,
  the pointer in Grep and Glob, rung 2's lines, the graph tools) through Claude Code's settings, reaching the plugin
  at once, the graph tools from the next session. For this repository: search by meaning (as measured, on or off),
  git's untracked cache, linked sources (a path or URL typed in to link one, a digit and a yes to unlink one). For
  this machine: whether the embedding worker and Aeon are built and current; a build takes minutes, so its key copies
  the command for a terminal. Without the engine, or with an older one, the plugin's options still work and the pane
  says what to install.
- `status` says whether the worker and Aeon are built, and whether from the commits this Aleph pins; and for search by
  meaning, the repository's own choice and its language's verdict side by side.
- `setup --dense`, `--no-dense` and `--auto-dense` on their own switch search by meaning in one line, without indexing.
- The plugin finds the engine through one script, `scripts/engine.sh`, for its server and for the pane alike.
- Nothing to install by hand for the two builds but Xcode's command-line tools: cmake and ninja, when missing, are
  fetched once from PyPI into Aleph's cache with uv, and SIMDe (for Aeon) at one pinned release. Seen in a sandbox
  with no Homebrew on PATH: Aeon built in 41 s, the worker in 25 s. A missing compiler is said with the one command
  that installs it (`xcode-select --install`).
- A build remembers the commit it was made from. When a newer Aleph pins a newer Aeon or llama.cpp, the setup list
  and doctor say so, and `aleph update` builds it again (a git hook's `update --no-fetch` never does). Builds made
  before this kept no record and count as current: they were made from the same pins.
- The worker build no longer uses ccache: CMake found Homebrew's even off PATH, and the build, run with a short
  PATH (Claude Code started from the Dock), then failed.

## 0.1.5

Memory by meaning works for real (Aeon), and the side chat reads like a conversation (the owner's asks, with the
advisor's and agy's review).

- **Aeon, switched on by a command.** `aleph setup --memory` builds Aeon's core for this engine's Python into the cache
  directory (a few seconds to a few minutes; needs git, cmake, ninja, a C++ compiler and SIMDe: `brew install simde`),
  and memory finds it there with no setting. The setup list offers it once the embedding worker is installed.
  Before, Aeon was used only where `ALEPH_AEON_LIB` pointed at a build, which nothing set.
- `forget` now erases inside Aeon too (the event is tombstoned and its text overwritten on disk); `forget --all`
  removes Aeon's files. Memories kept without a vector (the worker was down, or Aeon came later) get one at the next
  `aleph update` or `aleph embed run`.
- The agent is told that earlier sessions' answers are kept and that `recall` finds them. The side chat does not
  recall memories: its saved answers are unverified, and feeding them back would compound mistakes (advisor and agy).
- Aeon's branch `aleph-memory-fixes` (off `v4`) carries the fixes this needs, each with a test that failed first: ids
  kept across a crash, crash-recovered events searchable, a durable tombstone that erases text, compaction that
  keeps histories whole and keeps what was written during it, a blob arena safe for concurrent writers (it
  segfaulted under eight threads before), and a base install of `aeon_py` without PyTorch.

- Docked beside the transcript, the pane takes the window's height: a one-line header (model, effort, tokens), the
  messages with the newest at the bottom, a rule, the question field, and one line of keys under it. Before, the
  engine's window started at the top and the question field scrolled out of view after a few answers.
- Questions show as `> …`, answers marked `⏺` as Claude Code marks its own, each followed by one dim line of what it
  cites and its tokens; `✻ Searching the index…` and `✻ Thinking…` while it works.
- Settings are keys (m model, e effort) instead of two pickers; c closes; i inserts; s stops. The empty pane says
  what to ask, how to move between it and the conversation, and that answers are kept.
- Above the prompt (a narrow terminal) the pane fits its contents and shows the last two turns.

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
