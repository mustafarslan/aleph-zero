# aleph-zero

Local code search for coding agents. Results always reflect the working tree.

    uv sync
    uv run aleph index            # index the current worktree
    uv run aleph search "refresh token"
    uv run aleph search "rate limit" --kind docs --json
    uv run aleph status
    uv run aleph gc --days 30

The index lives in `<git-common-dir>/aleph-zero/index.db` and is shared by every
worktree of the repository. Design: `docs/superpowers/specs/`. Decisions: `docs/adr/`.
