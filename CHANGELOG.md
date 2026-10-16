# Changelog

## 0.1.20

- **The embedding worker is built from a newer llama.cpp, with no patch of Aleph's own** (`b76b133`, your decision on
  the measurement). The fix Aleph patched in (no logits buffer for an embeddings-only context, 1 MB a token) is upstream
  (PR #30255); the worker's memory is the same (564 MB against 563 after the same sample). Measured before moving: on
  2,000 chunks of vue/core and ripgrep every vector's cosine against the old build is 1.00000, and 50 questions find the
  same ten nearest chunks: **vectors made before stay valid; nothing is embedded again.** An installed worker is now
  older than the one this Aleph pins: `aleph-zero update` (or `aleph-zero setup --worker`) builds the new one, about
  30 s once the source is fetched; until then the old one keeps working, and setup and doctor say so.
- `aleph-zero doctor` now says when the worker is built from an older llama.cpp than this Aleph pins, also for a worker
  built before builds were recorded (it asks the server's own `--version`).

## 0.1.19

- **Routes from a committed OpenAPI document.** Where a server is generated from its spec (Spring with
  openapi-generator, Python with connexion), the code states no route; each operation now leads to its handler by its
  `operationId` (the controller module first, where the document names it). The document itself is never handed over.
  Judged on Apache Airflow 2.10.5 (not tuned on): operations with their handler 0 → 88 of 89, none of the 94 handlers
  wrong; spring-petclinic-rest 0 → 35 of 37; the route hand-over +8.7 points (from +7.1) and the right file first
  65.7% → 75.9%. Facts are read again once (version 11).
- Paths that agree only through parameters no longer tie: the one whose words agree more wins.
- Measured, not shipped: the call graph's neighbours ranked by meaning (−0.8; the call graph's ranking is closed after
  three tries); documentation ranked by whole documents (+9.4, under the +10 asked for).

## 0.1.18

- **Install in three lines.** `uv tool install …`, `uv tool update-shell`, then `aleph-zero setup` in a project: it now
  installs the Claude Code plugin on a yes (it runs Claude Code's own `claude plugin marketplace add mustafarslan/aleph-zero`
  and `claude plugin install aleph-zero@aleph-zero`, and says what failed in Claude Code's words), then asks to allow
  the plugin's tools, then the optional parts. `aleph-zero setup --plugin` does the first step alone. The menu now asks
  a step that an earlier answer made possible (allowing the tools, once the plugin is in) in the same run.
- **One name.** The README and the plugin's own texts say `aleph-zero` everywhere (TeX Live also installs an `aleph`);
  the `aleph` command is still installed.
- **Where to start.** `aleph-zero --help` says where to start and lists the everyday commands first; nothing was
  renamed or removed. `/aleph configure` puts the two switches that exist for measurements last, said so.
- **Module summaries after an edit:** made again only for the modules whose files changed: the first search after an
  edit on the largest project measured is as fast as with summaries off (was about 70 ms more).
- DEFERRED keeps only what waits; the lines that the graph tools' defaults made untrue are marked so.

## 0.1.17

- **Module summaries in search, on for Python projects** (DEFERRED 0-graph part 17; your decision on the readings).
  A module is a directory of source code; its summary is the words of what its files define and state (names, the
  first line of signatures, routes, models, environment names) and of their paths, made without a model from what the
  index keeps per file content. Search puts the files of the two modules that match a question best first (three
  each, a module only when it shares two of the question's words), then its own results. Measured through the engine:
  +9.2 points of files found (+3.6 to +15.1) on 72 questions about whole areas from six projects' documentation;
  on 595 Python issues +2.5 among ten (+0.6 to +4.2) and +3.2 among eight, the first file right +1.3 (−2.4 to +4.9);
  no added time to a warm search, the summaries made in 0.02 to 0.4 s once in the background, about 70 ms more for the
  first search after an edit on the largest project. Off for other languages: not shown to help (TypeScript leaned
  lower in two readings). It reorders most searches. The rule written for it asked a loss over one point on the first
  file to be ruled out, which cannot be done on fewer than thousands of questions: it was not met, and this is your
  call on the numbers, recorded so.

## 0.1.16

- **Routes read in more frameworks** (DEFERRED 0-graph part 12), each judged on a project that was not tuned on:
  NestJS (`@Controller('albums')` with `@Get(':id')`: ghostfolio 0 → 120 of 120 routes), Django URL patterns (`path()`,
  `re_path()`, `include()` as a mount: healthchecks 0 → 156 of 179), and gin and echo router groups made in the same file
  (gin-vue-admin 0 → 123 of 127). Among the projects measured before: gotify 23 → 39 of 42, immich 1 → 251 of 279.
- **Handlers wrapped in calls** (`pm(hasID(a.GetSubscriber))`) are read by their innermost name, and a handler made by
  a call by the name of what makes it: listmonk's routes with a handler found 27 → 126 of 132.
- **A prompt that names no route is matched by its words** ("deleting a client fails" → `DELETE /client/{id}`): at most
  three routes, only those sharing two words or more with it, one of them a word of the route's path. On 99 questions
  asked by an API operation's summary: +16.4 points of right files found, the right file first in 17 points more; on
  1,398 issues people wrote, files added to 8.6% and how often the right files are found not lowered. `facts` takes
  `words`; the plugin asks it for every prompt that is a question.
- **The look-ups are on after one yes, said plainly:** the plugin's look-ups use the engine's tools, which Claude Code
  asks about first. `aleph setup` now asks to allow them once the plugin is installed ("Allow Claude Code to use Aleph's
  tools without asking?"; or `aleph setup --allow-tools`): it adds one rule to `permissions.allow` and nothing else.
  Until then the plugin says so once a session and `/aleph check` says how. The plugin itself never writes a permission.
- **No permission pop-ups from the plugin.** Measured in an interactive session in Claude Code's default mode: the
  plugin's own calls of the engine's tools opened a "Do you want to proceed?" dialog for a person who had not allowed
  them, at a session's start (the vectors line, since 0.1.11). The plugin now asks Claude Code's decision first
  (`tool.check`, which opens nothing) and makes no call that would ask; the vectors line reads the engine's status
  without the tools. In auto mode too: the mode is not known to the plugin there, so the look-ups
  wait for the one yes in every mode (0.1.15's look-ups could be let through by auto mode's classifier).
- **The graph tools are on by default in the engine's own server too** (a bare `claude mcp add`); `ALEPH_GRAPH=0` turns
  them off, as the plugin's `graph=off` does.
- **For the plugin directory's review:** each plugin option is written by its fixed key; the settings pane no longer
  starts a shell to find the engine (`status` reports it); the plugin README says what the mod runs, calls and sends.
- `/aleph stats` counts look-ups that came back too late and refused calls.
- The facts are read again once, in the background when a session starts (0.2 to 6 s on the projects measured).
- Measured and not shipped: summaries of a module made without a model gain +8.7 points on 48 questions about a whole
  area (next: in the engine, then a model-written summary as a priced ask); the call graph's neighbours ranked by their
  own text gain +1.8, under the 5 points asked for.

## 0.1.15

- **The knowledge graph is handed to the agent, on by default** (the `graph` option, the owner's ask). When a prompt, a
  Grep or a recursive grep names an HTTP route (`GET /api/users/{id}`, a local URL, a path of two segments or more) or
  an environment variable (`DATABASE_URL`), the plugin looks it up and adds where the code registers and handles that
  route or reads that variable, from source code only (not documentation, example environment files, OpenAPI files or
  tests). Each name is looked up once a session; a text that names nothing costs no call.
- Go (gin, echo, chi, net/http) and Express routes record their handler when it is given by name
  (`clientHandler.DeleteClient`), and an answer gives where that handler is defined. `facts` takes `mentioned` (an issue, an error, a log line); on the command line `aleph facts --mentioned TEXT`.
  The facts are read again once (version 5).
- The `facts` tool is shown to the agent in full; `references` and `decisions` stay on demand. The server reads a
  worktree's facts in the background when a session starts while the graph tools are offered (0.2 to 6 s, once).
- Measured (DEFERRED 0-graph part 4, ADR-011): on 137 route and environment questions, what is handed over then search
  finds +4.8 points more answers than search (+1.6 to +8.7) and puts the right file first in 65% of questions against
  35%; on 1,504 ordinary issues it adds a note to 2.7%. In 60 sonnet sessions on 30 of those questions the agent called
  `facts` 19 times (0 before, when it saw only the tool's name) and read the right file first in 26 of 30 against 24
  with the graph off, at the same tokens (×0.991, 0.911 to 1.082): not shown to save tokens.
- Corrected: 0.1.14's run offered the graph tools by name only (Claude Code kept them behind its tool search).

## 0.1.14

- The call graph and the knowledge graph measured, by rules written first (DEFERRED entry 0-graph, ADR-011):
  - **The call graph** reaches answers search misses (+10.8 points of recall@10 one step from search's first five
    results, over 1,398 questions in six languages), but adding its neighbours to search's results gains nothing
    without a way to rank them (−0.5, interval through zero).
  - **Facts** (the knowledge graph) add +4.0 points (+0.8 to +7.9) to search on where a route is handled or an
    environment name read, over 137 questions in seven open-source repositories, and put the right file first far
    more often; under the 10 points asked for. The gain is where `facts` reads the framework (gin, echo).
  - **In real sessions** (120, sonnet, two passes) the agent never called the graph tools though they were offered:
    not shown to help, at +3% tokens with an interval through no change. They stay on by the owner's decision;
    `graph=off` is as defensible. The option's description says so.
- The A/B harness has an arm with the graph tools off (`L1o`); the benchmarks that made these readings are kept
  (`bench/eval/graph_expand.py`, `facts_bench.py`, `facts_questions.py` and its 137 questions).

## 0.1.13

- Functions defined inside functions are found by `symbol` and shown by `outline` (the owner's decision). `symbol` lists
  them after the top-level definitions of the same name, qualified under their function (`outer.inner`); `outline`
  shows them nested under it, and leaves them out first when a file is too long for its budget. What counts: a
  function, a name bound to a function (`const f = () => …`, `f = lambda …`), a class and its methods, defined in a
  function's body; not plain locals, options in object literals, default arguments or unnamed callbacks.
- Search ranks as before (it does not use them by name), the code graph answers as before, and nothing is chunked or
  embedded again. The index's schema goes from 10 to 11 in place; an index made before has its symbols read again
  once, during a long sync (the server's start, `index`, `update`, `source add`), never during a question: 1.7 s on a
  repository of 18,489 files. An older Aleph cannot open an index this one has upgraded.

## 0.1.12

- `/aleph configure`: Esc in the source field leaves the field and keeps the pane (what was typed stays); a second
  Esc closes it. Before, Esc there closed the whole pane.
- The vectors line paces itself: its next look at the engine waits at least 30 times what the last one took, so on a
  very large worktree it never takes much of the server's time.
- Documentation answers measured on 60 new questions (36 on six repositories' own docs, 24 on Vue's docs linked to
  vue/core), made so that nobody who built Aleph wrote or answered one: Aleph finds 81 to 83% of the answering
  documents in ten results, against 96 to 98% for whole-file BM25. Three ways of arranging code and documentation were
  measured by a rule written first; none passed its second draw, so the arrangement is unchanged (DEFERRED, entry 2).

## 0.1.11

- How far vectors are made is shown while they are being made, wherever you are: one short line under the prompt
  (`vectors 3,100/8,239 · ~2 min`, or `waiting` when the worker does not answer, or `indexing N files`), a word in
  the side chat's header, and in `/aleph configure` the row's "58% made" and, in its help, the project's code, its
  documentation and each linked source counted apart. Nothing is shown once they are made, nor where vectors are off.
  The estimate is from measured progress only. Designed with the advisor and agy.
- `status` gives `vectors.parts` (code, documentation, each linked source); from the running server also
  `vectors.making` (making, waiting and why, or idle; and the measured rate).
- Linking a source (`source add`, or the settings pane) indexes it at once, so its vectors are made without waiting
  for the next search; a `status` that finds vectors missing wakes the server's background, as a search does.
- Fixed: a linked source counted towards the repository's main language, so linking a large repository in another
  language could switch search by meaning's default for the project (seen: vue/core read as Go after linking a Go
  repository). Linked sources no longer count.

## 0.1.10

- `/aleph configure` explains the row the focus is on (↑↓, Tab or a click), in a fixed place under the rows: what it
  does in a sentence or two, its values with the current one marked, when a change applies, and a word where nothing
  was measured. Nothing is focused: what the three parts are. Values sit in a column of their own, an off value
  recedes and an out-of-date build is marked; chrome is in the light tone. Made with the advisor and agy.
- The side chat in the same style: its keys say the model and effort chosen, an answer is marked in the accent colour
  (a failed one in the warning colour), and an answer that cites nothing says so in the warning colour.

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
