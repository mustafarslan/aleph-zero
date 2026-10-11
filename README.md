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

## Documentation that lives in another repository

    uv run aleph source add ../team-docs --name docs        # read in place, follows that clone
    uv run aleph source add https://example.com/handbook.git   # cloned once; uses the network
    uv run aleph source refresh                                # fetch URL sources again
    uv run aleph source list

Its files are searched as `docs:path/in/that/repo.md`.

## Faster queries

Every query asks git what the working tree looks like. On a large repository that is
most of a query's time. Git can remember untracked files between calls:

    git config core.untrackedCache true

On Django (7,000 files) this takes a query from about 80 ms to about 30 ms.
`aleph doctor` shows whether it is on. Aleph never changes your git configuration itself.

## Where things are

The index lives in `<git-common-dir>/aleph-zero/index.db` and is shared by every
worktree of the repository. It is a cache: deleting it is always safe.
Supported languages: Python, TypeScript, JavaScript, Go, Rust, Java, C++, C, plus
Markdown and reStructuredText. Other text files are searched as plain text.

Design: `docs/superpowers/specs/`. Decisions and measurements: `docs/adr/`.
