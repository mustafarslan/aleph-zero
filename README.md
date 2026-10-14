# Aleph Zero

Local code search for coding agents. Aleph Zero (`aleph`) indexes a repository on your
machine and answers "where is this handled?" with ranked file and line locations, for
Claude Code (as an MCP server or a plugin) and on the command line.

- **Always current.** Every answer reflects the working tree as it is now, uncommitted
  edits included. There is no stale index to rebuild.
- **Local.** Your code never leaves the machine. The network is used only when you ask:
  to fetch grammars, the optional embedding model, or a repository you link by URL.
- **Fast.** A question takes 25 to 160 ms on repositories of 4,000 to 90,000 chunks; a
  cold index of Django takes about 3.5 seconds.

## Features

- **Ranked search** by words, identifiers (split from `camelCase` and `snake_case`) and
  paths, fused into one ranking, with optional search by meaning from a local embedding model.
- **Definitions and outlines**: where a name is defined; what a file defines, with line ranges.
- **Call graph**: who calls a function and what it calls, each answer marked certain or
  candidate; exact answers from a compiler's [SCIP](https://github.com/sourcegraph/scip)
  index when you import one.
- **Facts the code states**: routes, models and tables, environment variables, tasks,
  queues and gRPC services, each with its file and line.
- **Decision records**: the ADRs that govern a task or a file.
- **Linked sources**: search documentation or code from another repository beside your own.
- **Memory across sessions**: answers and notes kept per repository, recalled later with
  a check of whether the code they cite has changed.
- **Claude Code integration**: an MCP server, and a plugin that steers the agent to search
  before it greps.
- **Languages**: Python, TypeScript, JavaScript, Go, Rust, Java, C++ and C, plus Markdown
  and reStructuredText. Other text files are searched as plain text.

## Requirements

- Python 3.13, git, and [uv](https://docs.astral.sh/uv/). Developed and measured on macOS (Apple
  Silicon); Linux is expected to work but has not been measured.
- Optional, for search by meaning: cmake, ninja and a C++ compiler (the embedding server is
  built locally), and 310 MB for the model. On Apple Silicon it runs on the GPU.

## Install

```sh
git clone https://github.com/mustafarslan/aleph-zero.git
cd aleph-zero
uv tool install .            # puts `aleph` on your PATH
aleph doctor                 # checks that everything is in place
```

For development, `uv sync` instead, then run commands as `uv run aleph …`. Keep the
checkout: the Claude Code plugin is loaded from its `plugin/` directory and is not part of
the installed package.

Language grammars are downloaded the first time a file of that language is read
(`aleph doctor --fetch-grammars` gets them all at once).

## Quick start

```sh
cd your/project
aleph search "where are refresh tokens rotated"
aleph symbol refresh_token          # where a name is defined
aleph outline src/auth/store.py     # what a file defines
aleph status                        # what the index holds, and what is switched on
aleph setup                         # the optional parts, asked once (see below)
```

The first search indexes the repository. Later searches read only what changed.

## Use with Claude Code

**As an MCP server**, for every project:

```sh
claude mcp add --scope user aleph-zero -- "$(which aleph)" serve
```

The agent gets `search` (always loaded), and `symbol`, `outline`, `status`, `recall` and
`remember` on demand. Each tool takes the working directory, so one server answers for
any worktree.

**As a plugin**, which adds steering towards search:

```sh
claude --plugin-dir /path/to/aleph-zero/plugin
```

The plugin's `rung` option (`/config`) sets how far it steers:

| Rung | What it adds |
|---|---|
| 0 | The tools only |
| 1 (default) | Tools loaded up front, a short instruction to search before a recursive grep, and a pointer in Grep's and Glob's descriptions (`grep_pointer`: on/off) |
| 2 | Ranked locations attached to the first prompt of a session, when the engine is confident (`lines`: `places`, or `signatures` to add each file's first signature) |
| 3 | Ranked locations appended to a recursive `grep`, `rg`, `git grep` or `find` that found nothing |

`/aleph` opens a side chat about the repository whose answers cite `path:line`;
`/aleph check` tests the connection to the engine and `/aleph stats` shows the session's
tool use. Rungs 2 and 3 and the side chat call the engine themselves, which Claude Code
allows only when its tools are: add `"mcp__plugin_aleph-zero_aleph__*"` to
`permissions.allow`.

## Setup

Search by words and names needs no setup. `aleph setup` lists the optional parts, says
what each costs, and asks about each once on a terminal:

| Part | What it does | Flag (for scripts) |
|---|---|---|
| Claude Code | Prints the `claude mcp add` command; never edits Claude Code's settings | — |
| Embedding worker | Builds the local server and downloads the model (310 MB, network) | `--worker` |
| Linked sources | Searches another repository beside this one | `--source PATH_OR_URL` |
| Search by meaning | Uses it in this repository (`--dense`, vectors made in the background) or makes the vectors now (`--embed`) | `--embed`, `--dense`, `--no-dense` |
| Git's untracked cache | Speeds up git's look at the worktree; suggested from 3,000 files | `--untracked-cache` |

Answers are kept; `aleph setup --again` asks about declined parts again. Without a
terminal it only prints the list (`--json` for data). `aleph doctor` names what is still open.

## Commands

| Command | What it does |
|---|---|
| `aleph search QUERY` | Ranked locations. Options: `--kind code\|docs`, `--glob`, `--lang`, `--detail locations\|snippets\|full`, `-k`, `--budget-tokens`, `--timeout-ms`, `--dense`/`--no-dense`, `--json` |
| `aleph symbol NAME` | Where a name is defined |
| `aleph outline PATH` | What a file defines, with line ranges |
| `aleph callers NAME` / `aleph callees NAME` | The call graph, with how sure each answer is; the first question reads every file once |
| `aleph scip import\|run\|list\|forget` | A compiler's [SCIP](https://github.com/sourcegraph/scip) index for exact call answers while its files are unchanged (`import FILE [--tree REV\|--worktree]`; `run` needs the language's indexer installed, e.g. `scip-python`) |
| `aleph facts [KIND] [--like TEXT]` | Routes, models, environment variables, tasks … the code states |
| `aleph decisions TASK` / `--for PATH` | Decision records that govern a task or a file |
| `aleph source add\|list\|remove\|refresh` | Repositories searched beside this one (`name:path` in results) |
| `aleph memory list\|recall\|forget` | What earlier sessions kept |
| `aleph embed setup\|run\|status\|stop` | The embedding worker and this worktree's vectors |
| `aleph index` | Index now (searching does it on demand) |
| `aleph update` | Everything up to date at once after a pull: fetches URL sources, then the index, facts and vectors for what changed. `--print-hook` prints lines for git's `post-merge`/`post-checkout` hooks and a nightly schedule; Aleph installs none itself |
| `aleph status` / `aleph doctor` | What the index holds; whether everything is in place |
| `aleph gc [--days N]` | Drop content no worktree has used for N days (30 by default) |
| `aleph serve` | The MCP server, on standard input and output |

`--cwd DIR` before the command runs it for another directory.

## Search by meaning

Optional. With the embedding worker installed (`aleph embed setup`) and vectors made
(`aleph embed run`, about 90 chunks a second), results also come from a local embedding
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
per repository with `aleph setup --dense` / `--no-dense`, or per call with `--dense`,
`--no-dense` or `ALEPH_DENSE=1|0`. One worker serves every repository; it unloads its
model after ten idle minutes and exits after thirty.

## Environment variables

| Variable | Effect |
|---|---|
| `ALEPH_DENSE=1\|0` | Search by meaning on or off, overriding the language default |
| `ALEPH_GRAPH=1` | Offer the agent the call-graph, facts and decisions tools as well |
| `ALEPH_SCIP_MEMORY_MB` | Memory limit for `aleph scip run` (2,048 MB by default; JavaScript 3,072) |
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
cache shortens it (vscode 36 → 13 ms); `aleph setup` offers it, or run
`git update-index --untracked-cache` in the repository.

## Where data lives

| What | Where |
|---|---|
| The index, vectors, settings and memory of a repository | `<git-common-dir>/aleph-zero/`, shared by its worktrees; `~/.cache/aleph-zero/index/` when git's directory cannot be written or is on a network filesystem (`aleph status` says which) |
| The embedding worker, model and machine-wide answers | `~/.cache/aleph-zero/` (`$XDG_CACHE_HOME` if set) |
| Clones of sources linked by URL; indexes of directories outside git | `~/.cache/aleph-zero/sources/`, `~/.cache/aleph-zero/index/` |
| The plugin's session meter | `~/.claude/plugins/store/aleph-zero_*.json` |
| Language grammars | `tree-sitter-language-pack`'s cache (`~/Library/Caches/` on macOS, `~/.cache/` on Linux) |

The index, vectors and facts are caches: deleting them is safe, they are rebuilt as
needed, and a damaged index is set aside and rebuilt. Memories, the list of linked sources,
setup answers and imported SCIP indexes are not: deleting the directory loses them.

## What is searched

What git tracks in the worktree, as it is on disk, plus untracked files that are not
ignored. Files over 300 KB, binary and non-UTF-8 files, lockfiles, Git LFS pointers and
minified files are skipped.

- **Sparse checkouts:** files outside the checkout are not searched.
- **Submodules** are not searched; link one with `aleph source add path/to/submodule`.
- **Shallow and partial clones** work; nothing is fetched to answer a question.
- **Outside git**, a directory is searched from there down, as it is on disk, without
  hidden files, `node_modules`, `venv`, `__pycache__`, symbolic links and nested
  repositories. Aleph refuses your home directory, system directories and directories of
  more than 20,000 files.

## Uninstall

```sh
aleph embed stop                                   # stop the embedding worker, if running
claude mcp remove aleph-zero                       # if you added the MCP server
uv tool uninstall aleph-zero
```

Then remove what it kept:

```sh
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/aleph-zero" # worker, model, linked clones, settings
rm -rf "/tmp/aleph-zero-$(id -u)"                   # the worker's socket, where the cache path was too long
rm -f ~/.claude/plugins/store/aleph-zero_*.json     # the plugin's meter, if you used the plugin
rm -rf "$(git rev-parse --git-common-dir)/aleph-zero"   # in each repository you used it in
git update-index --no-untracked-cache               # optional: in each worktree where setup switched it on
```

If you used the plugin, also remove the `--plugin-dir` flag, and the `permissions.allow`
and `pluginConfigs` entries from your Claude Code settings. Remove any lines you added from
`aleph update --print-hook` (git hooks, cron or launchd), then delete the checkout. The
grammar cache is shared with other tools that use `tree-sitter-language-pack`; delete it
only if nothing else needs it.

## Documentation

- Design: [`docs/superpowers/specs/`](docs/superpowers/specs/)
- Decisions and measurements: [`docs/adr/`](docs/adr/)
- Open work: [`docs/DEFERRED.md`](docs/DEFERRED.md)

## License

[MIT](LICENSE)
