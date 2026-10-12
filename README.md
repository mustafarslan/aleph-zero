# aleph-zero

Local code search for coding agents. Results always reflect the working tree,
including uncommitted edits.

## Quick start

    uv sync
    uv run aleph doctor                       # is everything in place?
    uv run aleph index                        # optional: the first search indexes too
    uv run aleph search "where are refresh tokens rotated"
    uv run aleph search TokenStore.get --detail full
    uv run aleph symbol refresh_token         # where a name is defined
    uv run aleph outline src/auth/store.py    # what a file defines, with line ranges
    uv run aleph status

Useful options for `search`: `--kind code|docs`, `--glob "src/api/*"`, `--lang python`,
`--detail locations|snippets|full`, `--json`, `--timeout-ms 500`.

## Use it from Claude Code

    claude mcp add aleph-zero -- /absolute/path/to/aleph-zero/.venv/bin/aleph serve

The server offers four tools. `search` is always loaded; `symbol`, `outline` and
`status` load on demand. Each takes `cwd`, so one server answers for any worktree.

### As a plugin, with steering

    claude --plugin-dir /absolute/path/to/aleph-zero/plugin

The plugin starts the same server and adds a mod that steers Claude Code towards it.
How far it goes is one option, the rung (`/config`, or `pluginConfigs` in settings):

| Rung | What it adds |
|---|---|
| 0 | Nothing: the tools as above |
| 1 (default) | All four tools loaded up front with tuned descriptions, a short standing instruction to search before recursive grep, and a pointer to it in the descriptions of Claude Code's own Grep and Glob |
| 2 | Ranked locations ride along with the first prompt of a session, when the engine is confident |
| 3 | A recursive `grep`, `rg`, `git grep` or `find -name` that found nothing gets ranked locations appended |

The pointer in Grep and Glob has its own switch (`grep_pointer`: on or off).

Rungs 2 and 3 have the mod itself ask the engine, which Claude Code allows only if the
engine's tools are allowed: add `"mcp__plugin_aleph-zero_aleph__*"` to `permissions.allow`.
`/aleph check` says whether the mod can reach the engine; `/aleph stats` shows the session's
tokens, tool calls and how often search was used before grep (`/aleph stats last` for the
session before). Which rungs pay is being measured (ADR-011); until then the default is 1.

## Documentation that lives in another repository

    uv run aleph source add ../team-docs --name docs        # read in place, follows that clone
    uv run aleph source add https://example.com/handbook.git   # cloned once; uses the network
    uv run aleph source refresh                                # fetch URL sources again
    uv run aleph source list

Its files are searched as `docs:path/in/that/repo.md`.

## Search by meaning

    uv run aleph embed setup      # once: builds the worker and downloads the model (310 MB; uses the network)
    uv run aleph embed run        # gives this worktree's code and docs their vectors (about 90 chunks a second)
    uv run aleph embed status

With the worker installed, vectors are used by default in projects whose main language
has a benchmark verdict in their favour: today that is Python (ADR-002). `aleph status`
shows a project's main language and whether vectors are on, and why. `--dense`,
`--no-dense` and `ALEPH_DENSE=1|0` decide by hand. One worker serves every session and
repository; it unloads its model after ten idle minutes and exits after thirty.

## Faster queries

Every query asks git what the working tree looks like. On a large repository that is
most of a query's time. Git can remember untracked files between calls:

    git config core.untrackedCache true

On Django (7,000 files) this takes a query from about 80 ms to about 30 ms.
`aleph doctor` shows whether it is on. Aleph never changes your git configuration itself.

## Where things are

The index lives in `<git-common-dir>/aleph-zero/index.db` and is shared by every
worktree of the repository. It is a cache: deleting it is always safe. When git's
directory cannot be written, or is on a network filesystem, it lives in
`~/.cache/aleph-zero/index/` instead; `aleph status` says where and why.

What is searched is what git tracks in this worktree, as it is on disk now:

- In a sparse checkout, files outside it are not searched. `status` says how many.
- Submodules are not searched. `status` and `doctor` list them; link one as a source
  with `aleph source add path/to/submodule`.
- Shallow and partial clones work. Nothing is ever fetched to answer a query.
- A directory that is not a git repository is not searched: run `git init` there.

Content no worktree has used for 30 days is dropped by itself; `aleph gc --days N`
does it on request. A damaged index file is set aside (kept as `index.db.damaged-…`)
and built again.
Supported languages: Python, TypeScript, JavaScript, Go, Rust, Java, C++, C, plus
Markdown and reStructuredText. Other text files are searched as plain text.

Design: `docs/superpowers/specs/`. Decisions and measurements: `docs/adr/`.
