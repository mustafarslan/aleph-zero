# Aleph Zero

Local code search for coding agents. Aleph Zero (`aleph-zero`) indexes a repository on your
machine and answers the questions an agent asks before it edits code: where something is
handled, where a name is defined, who calls it, and what the project has decided about it.
It works as a Claude Code plugin, as an MCP server for any client, and on the command line.

- **Always current.** Every answer reflects the working tree as it is now, uncommitted edits
  included. Nothing has to be rebuilt after a pull or a branch switch.
- **Local.** Your code never leaves the machine. The network is used only when you ask: to
  download grammars, the optional embedding model, or a repository you link by URL.
- **Fast.** 25 to 160 ms a question on repositories of 4,000 to 90,000 chunks.

## Quick start

```sh
uv tool install git+https://github.com/mustafarslan/aleph-zero.git
uv tool update-shell                 # then open a new terminal
cd path/to/your/project && aleph-zero setup
```

`aleph-zero setup` asks about each part once, and does it on a yes: installing the Claude Code
plugin (it runs Claude Code's own `claude plugin marketplace add` and `claude plugin install`),
letting the plugin use the engine's tools (one rule in `~/.claude/settings.json`), the optional
embedding worker and memory by meaning (built on this machine), documentation from another
repository, and git's untracked cache. Then start a new Claude Code session. Everything else has
working defaults; nothing else needs switching on. It needs macOS on Apple Silicon (Linux is
untested), [uv](https://docs.astral.sh/uv/), git and Claude Code; the optional builds also need
Xcode's command-line tools.

## What it does

**Search.** Hybrid retrieval over code and documentation: full-text search (BM25) with
identifiers split from `camelCase` and `snake_case`, a lookup of definitions by name, a
match on file paths and, optionally, search by meaning with a local embedding model
(EmbeddingGemma). The four are fused into one ranking. Results are file and line ranges with
the definition's signature, sized to a token budget.

**Code structure.** Definitions and file outlines for Python, TypeScript, JavaScript, Go,
Rust, Java, C++ and C, read with tree-sitter. Markdown is split by heading; other text
files, reStructuredText included, are searched in windows of lines.

**Code graph.** Who calls a function and what it calls, resolved through imports, classes,
inheritance and stated types. Each edge is marked certain or candidate. When you import a
compiler's [SCIP](https://github.com/sourcegraph/scip) index, its exact answers are used for
the files it still matches.

**Architecture facts.** What the code states about the system: HTTP routes and where routers
are mounted, models and tables, environment variables, background tasks, queues and gRPC
services, each with its file and line; routes are read for Express, NestJS, FastAPI, Flask, Django
URL patterns, Spring and Go (gin, echo, chi, net/http; gin's and echo's groups made in the same file), and where a handler is
given by name, where it is defined. Where a server is generated from a committed OpenAPI document
(Spring's openapi-generator, connexion), each operation leads to its handler by its `operationId`;
the document itself is never given as an answer.
With an [oxn](docs/adr/ADR-007-relationship-to-oxn.md) `oxn.yaml`, the layer a file belongs to
and what that layer may depend on. With the plugin these are looked up for the agent: when a
prompt, or a pattern the agent greps for, names an HTTP route (`GET /api/users/{id}`) or an
environment variable (`DATABASE_URL`), or a prompt's words point to a route ("deleting a client
fails" to `DELETE /client/{id}`), the plugin adds where the code registers and handles that route
or reads that variable, from source code only (not documentation or tests). Most requests
name neither, and then nothing is added: on 1,398 issues people wrote, in six languages, files
were added to 8.6%, without lowering how often the right files were found.

**Module summaries.** In a project whose main language is Python, a question about a whole
area ("how is a recipe imported from a URL") puts first the files of the two modules (directories)
whose definitions, routes and paths match it best: their summaries are made without a model, from
what the index already keeps for each file's content, so they are never stale and an edit re-reads
only the changed file. Measured: +9.2 points of right files found on 72 questions about whole
areas, +2.5 among ten on 595 Python issues, the first file right unchanged within the noise. Off
for other languages, where it was not shown to help. It reorders most searches; that is its job.
Nothing to switch on.

**Decision records.** Ranks a repository's ADRs for a task, or lists those that apply to a
file, so the agent sees what was already decided.

**Memory across sessions.** The side chat's answers and the notes you keep are stored per
repository and recalled later, by the agent (`recall`, `remember`) or on the command line.
Each memory cites code by content, so a recall says whether that code has changed since. It
is recalled by words and recency, and also by meaning when the optional
[Aeon](docs/adr/ADR-006-memory-layer.md) store is available. `aleph-zero memory forget` erases a
memory's text, except the copy Aeon's store keeps when it is used.

**Linked sources.** Documentation or code from another repository, searched beside your own
and cited as `name:path`.

**Claude Code plugin.** Starts the engine, loads its tools, and steers the agent to search
before it greps, in levels you choose: tool descriptions, ranked locations with the first
prompt, and suggestions after a grep that found nothing. Whether steering saves tokens has
not been shown yet: in measured sessions the agent often still greps first
([ADR-011](docs/adr/ADR-011-steering.md)). `/aleph` opens a side chat about the repository
whose answers cite `path:line`.

## Requirements

- macOS on Apple Silicon, where it is developed and measured. Linux is expected to work but
  has not been tested.
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 itself if needed), git, and
  Claude Code for the plugin.
- For search by meaning and memory by meaning: Xcode's command-line tools (their C++
  compiler). The embedding server is built locally from one pinned commit of llama.cpp (about
  220 MB of source) and the model is a 310 MB download; Aeon's core is built from one pinned
  commit too. cmake, ninja and SIMDe are fetched by Aleph into its cache when they are not
  installed, with uv; nothing else is installed by hand.

## Install with Claude Code

### Once per machine

1. uv, and Xcode's command-line tools (for search and memory by meaning).
   `xcode-select --install` opens Apple's installer; let it finish first. Already installed:
   it says so, and nothing happens.

   ```sh
   brew install uv        # or: curl -LsSf https://astral.sh/uv/install.sh | sh
   xcode-select --install
   ```

2. The engine. `uv tool update-shell` puts uv's tool directory on PATH for new terminals.

   ```sh
   uv tool install git+https://github.com/mustafarslan/aleph-zero.git
   uv tool update-shell
   ```

3. In a new terminal, in a project: `aleph-zero setup` asks about each of the rest and does it on
   a yes (`aleph-zero setup --plugin` does the plugin step alone). By hand instead: the embedding
   server and model (optional, for search by meaning), Aeon
   (optional, for recalling earlier answers by meaning), then the plugin. Each build fetches what
   it needs that is missing (cmake, ninja, SIMDe) and says so.

   ```sh
   aleph-zero setup --worker
   aleph-zero setup --memory
   claude plugin marketplace add mustafarslan/aleph-zero
   claude plugin install aleph-zero@aleph-zero
   ```

4. Let the plugin use the engine's tools. This is the one step that is yours to give: Claude Code
   asks before any tool runs, and the plugin's own look-ups (where a route or an environment
   variable you name is handled, rungs 2 and 3, the side chat) do nothing until it is allowed.
   `aleph-zero setup` asks it once the plugin is installed; to give it directly:

   ```sh
   aleph-zero setup --allow-tools     # adds "mcp__plugin_aleph-zero_aleph__*" to permissions.allow in ~/.claude/settings.json
   ```

   So the look-ups are on after this one yes, not before it, in every permission mode (auto mode
   included: the plugin makes no call Claude Code would ask about). Until then they do nothing, the plugin
   says so once a session, and `/aleph check` says whether the tools are allowed. Nothing else needs
   switching on: the plugin's options all have working defaults.

The command is `aleph-zero`. It is also installed as `aleph-zero`, but TeX Live installs a program of
that name too, so this README and the plugin always use `aleph-zero`.

The plugin installs at rung 1 with the graph tools on (see [Plugin options](#plugin-options)).
To steer as far as it goes:

```sh
claude plugin install aleph-zero@aleph-zero --config rung=3
```

Rungs 2 and 3 and the side chat call the engine from the plugin itself, which Claude Code
allows only when its tools are allowed. Add this to `~/.claude/settings.json`, merged into
what is there:

```json
{ "permissions": { "allow": ["mcp__plugin_aleph-zero_aleph__*"] } }
```

### Once per project

```sh
cd ~/code/your-project
aleph-zero setup --source ~/code/your-docs --embed --untracked-cache
aleph-zero status
```

- `--source` links a documentation (or code) repository: a path is read in place and
  follows that checkout; a URL is cloned once and fetched again by `aleph-zero update`. Give it
  once for each repository to link.
- `--embed` makes vectors for the project and its linked sources, and switches search by
  meaning on for this project. By default it is on only where the main language is
  JavaScript, TypeScript, C, C++ or Rust (see [Search by meaning](#search-by-meaning)), so use
  it for a Python, Go or Java project where you want it. Each project keeps its own vectors:
  docs linked to three projects are embedded three times (about 90 chunks a second).
- `--untracked-cache` turns on git's untracked cache, which makes git's look at a large
  worktree several times faster.
- `aleph-zero status` shows the main language, whether vectors are on and why, and the linked
  sources. In Claude Code, `/aleph check` tests the plugin's connection to the engine.
- In Claude Code, `/aleph configure` opens the same settings in a pane: the plugin's options
  below, search by meaning and git's untracked cache for this project, linked sources (a
  path or URL to link, a digit to unlink), and whether the worker and Aeon are built on this
  machine. A build takes minutes, so for those a key copies the command (`… setup --worker`,
  `… setup --memory`) for you to run in a terminal.

Nothing else is needed afterwards. Edits, pulls and branch switches are picked up at the
next question, in the docs too, and missing vectors are made in the background.

### Updating

```sh
uv tool install --reinstall git+https://github.com/mustafarslan/aleph-zero.git
claude plugin marketplace update aleph-zero
claude plugin update aleph-zero@aleph-zero
```

### Plugin options

Set at install (`--config KEY=VALUE`), later with `/aleph configure` or `/plugin` in Claude
Code, or with `claude plugin configure aleph-zero@aleph-zero`. A change reaches the plugin
at once (Claude Code loads it again with the new value), except `graph`, which the engine
reads when Claude Code starts it: from the next session.

| Option | Values | What it does |
|---|---|---|
| `rung` | `0`–`3` (default `1`) | How far the plugin steers. `0`: the tools only. `1`: the four search tools loaded up front, a short instruction to search before a recursive grep, and a pointer in Grep's and Glob's descriptions. `2`: also ranked locations with the first prompt of a session. `3`: also ranked locations after a search with Claude Code's Grep tool, or a recursive `grep`, `rg`, `git grep` or `find`, that found nothing. In measured sessions no rung has yet been shown to save tokens or tool calls ([ADR-011](docs/adr/ADR-011-steering.md)); rung 1 is the default as the least intrusive. |
| `graph` | `on`/`off` (default `on`) | The knowledge graph and the other graph tools. On: when a prompt or a grep names an HTTP route or an environment variable, the plugin adds where the code handles or reads it; the `facts` tool is shown to the agent in full, and `references` (who calls what) and `decisions` (decision records) are offered on demand. Measured (2026-10-16, sonnet, [ADR-011](docs/adr/ADR-011-steering.md)): on 30 route and environment questions, the agent called `facts` 19 times and read the right file first in 26 of 30, against 24 of 30 with it off (descriptive, not a test), at the same tokens (×0.99, interval through no change); earlier, offered by name only, the graph tools were not called in 56 sessions. On by the owner's decision; `off` leaves the agent the search tools alone. |
| `grep_pointer` | `on`/`off` (default `on`) | The pointer in Grep's and Glob's descriptions (part of rung 1). |
| `lines` | `places`/`signatures` (default `places`) | What rung 2 shows for each file. |

### Other MCP clients

The engine is a standard MCP server on standard input and output, `aleph-zero serve`:

```sh
claude mcp add --scope user aleph-zero -- "$(which aleph-zero)" serve     # Claude Code, without the plugin
```

The agent gets `search` (always loaded), and `symbol`, `outline`, `status`, `recall` and
`remember` on demand, and `references`, `facts` and `decisions` (`ALEPH_GRAPH=0` leaves them out). Each
tool takes the working directory, so one server answers for any worktree.

## What it runs, sends and fetches

- **Runs:** the engine (`aleph-zero serve`) as a local process started by Claude Code, and
  `git` in your repositories to read what changed. With search by meaning, a local
  llama.cpp server for the embedding model, started on demand and stopped after thirty idle
  minutes. `aleph-zero scip run` runs a language's compiler indexer only when you ask.
- **Sends:** nothing. Your code, the index, vectors and memories stay on the machine. The
  side chat (`/aleph`) uses Claude Code's own model connection, as any Claude Code request does.
- **Fetches, only when asked:** language grammars (the first time a language is read), the
  llama.cpp source and the embedding model (`aleph-zero setup --worker`), and repositories you
  link by URL (`aleph-zero setup --source URL`, `aleph-zero update`).
- **Writes:** see [Where data lives](#where-data-lives). It changes no git configuration;
  `--untracked-cache` writes git's cache into the worktree's index only.

## Command line

```sh
aleph-zero search "where are refresh tokens rotated"
aleph-zero symbol refresh_token          # where a name is defined
aleph-zero outline src/auth/store.py     # what a file defines
aleph-zero callers TokenStore.get        # who calls it
aleph-zero facts route --like users      # routes the code declares
aleph-zero facts --mentioned "GET /api/users/7 returns 500"   # where what a text names is handled
aleph-zero decisions "cache invoice totals in redis"
```

The first search indexes the repository; later ones read only what changed. `aleph-zero setup`
on its own lists the optional parts and asks about each once on a terminal; without a
terminal it only prints the list.

### All commands

| Command | What it does |
|---|---|
| `aleph-zero setup` | Asks about each part once on a terminal and does it on a yes: the Claude Code plugin (`--plugin`), allowing its tools (`--allow-tools`), the embedding worker (`--worker`), memory by meaning (`--memory`), linked sources (`--source PATH_OR_URL`), this project's vectors (`--embed`, `--dense`/`--no-dense`), git's untracked cache (`--untracked-cache`). Each flag does its part alone; `--again` asks again about what was declined; without a terminal or with `--json` it only lists |
| `aleph-zero search QUERY` | Ranked locations. Options: `--kind any\|code\|docs`, `--glob`, `--lang`, `--detail locations\|snippets\|full`, `-k`, `--budget-tokens`, `--timeout-ms`, `--dense`/`--no-dense`, `--json` |
| `aleph-zero symbol NAME` | Where a name is defined |
| `aleph-zero outline PATH` | What a file defines, with line ranges |
| `aleph-zero callers NAME` / `aleph-zero callees NAME` | The call graph, with how sure each answer is; the first question reads every file once |
| `aleph-zero scip import\|run\|list\|forget` | A compiler's [SCIP](https://github.com/sourcegraph/scip) index for exact call answers while its files are unchanged (`import FILE [--tree REV\|--worktree]`; `run` needs the language's indexer installed, e.g. `scip-python`) |
| `aleph-zero facts [KIND] [--like TEXT]` | Routes, models, environment variables, tasks … the code states. `--mentioned TEXT`: the routes and environment variables TEXT names, with where the code handles or reads each (the handler's definition included). `--words` with `--mentioned`: when TEXT names no route, the routes whose words it shares (at most three) |
| `aleph-zero decisions [TASK]` / `--for PATH` | Decision records that govern a task or a file; all of them with neither |
| `aleph-zero source add\|list\|remove\|refresh` | Repositories searched beside this one (`name:path` in results) |
| `aleph-zero memory list\|recall\|forget` | What earlier sessions kept |
| `aleph-zero embed setup\|run\|status\|stop` | The embedding worker and this worktree's vectors |
| `aleph-zero index` | Index now (searching does it on demand) |
| `aleph-zero update` | Everything up to date at once after a pull: fetches URL sources, then the index, facts and vectors for what changed. `--print-hook` prints lines for git's `post-merge`/`post-checkout` hooks and a nightly schedule; Aleph installs none itself |
| `aleph-zero status` / `aleph-zero doctor [--fetch-grammars]` | What the index holds; whether everything is in place |
| `aleph-zero gc [--days N]` | Drop content no worktree has used for N days (30 by default) |
| `aleph-zero serve` | The MCP server, on standard input and output |

`--cwd DIR` before the command runs it for another directory.

## Search by meaning

Optional. With the embedding worker installed (`aleph-zero embed setup`) and vectors made
(`aleph-zero embed run`, about 90 chunks a second), results also come from a local embedding
model. It is on by default for a project whose main language passed a benchmark: at least
5 points of file recall@10 on issue titles, with a 95% interval that excludes zero
([ADR-002](docs/adr/ADR-002-lexical-and-fusion.md)).

| Main language | Gain (95% interval) | Default |
|---|---|---|
| JavaScript | +9.8 (+6.0 to +13.6), 448 questions | on |
| TypeScript | +7.3 (+1.8 to +12.9), 110 questions | on |
| C | +12.3 (+3.7 to +21.3), 88 questions | on |
| C++ | +6.7 (+1.4 to +12.3), 108 questions | on |
| Rust | +8.0 (+2.3 to +13.9), 170 questions | on |
| Python | +3.9 (+0.6 to +7.2), 400 questions | off |
| Go | +2.0 (−3.9 to +7.8), 111 questions | off |
| Java | +0.1 (−3.2 to +3.4), 296 questions | off |

On whole issue texts vectors gain 5 to 20 points in every language. Switch them on or off
per repository with `aleph-zero setup --dense` / `--no-dense`, for one search with
`aleph-zero search --dense` / `--no-dense`, or for a process with `ALEPH_DENSE=1|0`. One worker serves every repository; it unloads its
model after ten idle minutes and exits after thirty.

## Environment variables

| Variable | Effect |
|---|---|
| `ALEPH_DENSE=1\|0` | Search by meaning on or off, overriding the project's setting |
| `ALEPH_GRAPH=0` | Do not offer the code-graph, facts and decisions tools (offered by default, and a worktree's facts are then read when a session starts; the plugin's `graph` option sets it) |
| `ALEPH_SCIP_MEMORY_MB` | Memory limit for `aleph-zero scip run` (2,048 MB by default; JavaScript 3,072) |
| `ALEPH_AEON_LIB` | Path to Aeon's core library, for recalling memories by meaning |
| `XDG_CACHE_HOME` | Where the machine-wide cache lives (`~/.cache` by default) |

## Performance

Measured on an M4 Max: median of five warm questions with vectors on, and a cold index with four processes
(the cold-index figures are from another commit of the same repository):

| Repository | Question | Cold index |
|---|---|---|
| fmt (4,000 chunks) | 24 ms | |
| pandas (43,000) | 44 ms | |
| vscode (77,000) | 83 ms | 4.5 s (87,000 chunks) |
| trino (90,000) | 84 ms | |
| django (52,000) | 105 ms | 3.5 s (64,000 chunks) |
| material-ui (59,000) | 159 ms | |

Most of a question on a large repository is git listing what changed. Git's untracked
cache shortens it (vscode 36 → 13 ms); `aleph-zero setup` offers it, or run
`git update-index --untracked-cache` in the repository.

## Where data lives

| What | Where |
|---|---|
| The index, vectors, settings and memory of a repository | `<git-common-dir>/aleph-zero/`, shared by its worktrees; `~/.cache/aleph-zero/index/` when git's directory cannot be written or is on a network filesystem (`aleph-zero status` says which) |
| The embedding worker, model and machine-wide answers | `~/.cache/aleph-zero/` (`$XDG_CACHE_HOME` if set) |
| Clones of sources linked by URL; indexes of directories outside git | `~/.cache/aleph-zero/sources/`, `~/.cache/aleph-zero/index/` |
| The plugin's session meter and side-chat settings | `~/.claude/plugins/store/aleph-zero_*.json` |
| Language grammars | `tree-sitter-language-pack`'s cache (`~/Library/Caches/` on macOS, `~/.cache/` on Linux) |

The index, vectors and facts are caches: deleting them is safe, they are rebuilt as
needed, and a damaged index is set aside and rebuilt. Imported SCIP indexes live in the
index, so import them again after that. Memories, the list of linked sources and setup
answers are not caches: deleting the directory loses them.

## What is searched

What git tracks in the worktree, as it is on disk, plus untracked files that are not
ignored. Files over 300 KB, binary and non-UTF-8 files, lockfiles, Git LFS pointers and
minified files are skipped.

- **Sparse checkouts:** files outside the checkout are not searched.
- **Submodules** are not searched; link one with `aleph-zero source add path/to/submodule`.
- **Shallow and partial clones** work; nothing is fetched to answer a question.
- **Outside git**, a directory is searched from there down, as it is on disk, without
  hidden files, `node_modules`, `venv`, `__pycache__`, symbolic links and nested
  repositories. Aleph refuses your home directory, system directories and directories of
  more than 20,000 files.

## Uninstall

```sh
claude plugin uninstall aleph-zero@aleph-zero
claude plugin marketplace remove aleph-zero
claude mcp remove aleph-zero                          # only if you added the server without the plugin
aleph-zero embed stop                                 # stop the embedding worker, if running
uv tool uninstall aleph-zero                          # removes both `aleph-zero` and `aleph-zero`
```

Then remove what it kept:

```sh
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/aleph-zero"   # worker, model, URL clones, machine settings
rm -rf "/tmp/aleph-zero-$(id -u)"                     # worker sockets, where a cache path was too long for one (every cache's)
rm -f ~/.claude/plugins/store/aleph-zero_*.json       # the plugin's meter and side-chat settings
rm -rf ~/.claude/plugins/cache/aleph-zero             # the plugin's copies, which Claude Code keeps after an uninstall
rm -rf "$(git rev-parse --git-common-dir)/aleph-zero" # in each project: its index, vectors, memories, settings
git update-index --no-untracked-cache                 # optional, in each worktree where setup turned it on
```

Also remove the `permissions.allow` entry from your Claude Code settings, and any lines you
added from `aleph-zero update --print-hook` (git hooks, cron or launchd). The grammar cache
(`tree-sitter-language-pack`) is shared with other tools; delete it only if nothing else
uses it.

## Documentation

- Decisions and measurements: [`docs/adr/`](docs/adr/)
- Open work: [`docs/DEFERRED.md`](docs/DEFERRED.md)

## License

[MIT](LICENSE)
