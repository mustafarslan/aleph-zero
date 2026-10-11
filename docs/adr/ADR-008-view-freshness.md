# ADR-008: How a query learns what the worktree looks like

- **Status:** Accepted for M1 (stateless). The faster path is designed here and built in M1c, when a long-lived server exists to own it.
- **Date opened:** 2026-10-11

## Question

Every query must answer for the worktree as it is at that moment: uncommitted edits, new files, deletions, branch switches. Asking git on every query is correct by construction but is most of a query's time. Can it be made cheaper without ever answering from a stale picture?

## Measured (Django, 7,084 tracked files, M4 Max, warm)

| Step | Time |
|---|---|
| `git status --porcelain=v2 -z --untracked-files=all` on a freshly cloned tree | 115.5 ms |
| The same after any git command has rewritten the index once | 66 ms |
| Tracked files only (`--untracked-files=no`) | 16.6 ms |
| Untracked files only (`git ls-files --others --exclude-standard`) | 53.4 ms |
| `git diff-files` | 15.6 ms |
| `git diff-index --cached HEAD` | 5.5 ms |
| `lstat` of every tracked file from Python | 10.5 ms |
| Walking the tree from Python (`os.walk`) | 49 ms |
| With git's untracked cache enabled and `--untracked-files=normal` | 20.7 ms |
| Whole view build as shipped in M1a (two git calls) | 72 ms p50, 99 ms p95 |
| Everything after the view is built (sync of an unchanged view + search) | about 5 ms |

Two things explain the cost:

- **About 50 ms is the search for untracked files.** Git walks every directory and applies the ignore rules.
- **Up to 50 ms more appears on a freshly checked-out tree.** Files written in the same second as the index look "possibly changed" to git, so it re-reads them. Git normally fixes this by rewriting the index, but we call it with `GIT_OPTIONAL_LOCKS=0` so that a background reader never holds `index.lock`. The cost disappears the first time the user or the agent runs any ordinary git command. It is time only: git still reports the right answer.

## Options considered

| | Option | Outcome |
|---|---|---|
| A | Ask git on every query (as shipped) | **Kept as the base and the fallback.** Correct by construction. |
| B | Let our own git call rewrite the index to clear the fresh-checkout cost | Rejected. It takes `index.lock`; a user's `git commit` that meets the lock fails outright, because git does not wait for it. This is exactly what `GIT_OPTIONAL_LOCKS=0` exists to prevent. |
| C | Turn on git's untracked cache or its file-system monitor for the user | Rejected as a default. It changes the user's repository configuration, and the cache lives in the index file that a background reader must not write. May be offered later as an explicit opt-in. |
| D | Track untracked files ourselves: remember directory modification times, re-list only directories that changed | Rejected. It re-implements part of git and is wrong in known cases: a directory's time does not change when something changes inside a sub-directory; nested and global ignore files; symlinked directories; case-only renames; hard links; an overwrite within the timestamp's resolution. Each is a rule git already enforces and we would have to own. |
| E | Watch the file system and use the events only as a "something changed" flag | **Chosen for M1c.** See below. |
| F | Learn about changes only from the agent's own edit tools | Rejected as a source of truth: it misses the user's editor, git operations, formatters and code generators. |

## Decision

**M1 (now):** the view is rebuilt from git on every query. No state is kept between queries, so there is nothing to go stale.

**M1c (with the server process):** keep that rebuild exactly as it is, and add a watcher whose only job is to say whether it can be skipped.

- The server watches the worktree and the repository's git directory (FSEvents on macOS, inotify on Linux).
- Any event sets one flag. Event contents are never used to decide *what* changed, so a missed detail cannot produce a wrong view; only a missed event could, and the next two rules cover that.
- A query first drains pending events synchronously (FSEvents: `FSEventStreamFlushSync`; inotify: read the queue), so an edit made just before the query is seen.
- If the flag is clear, the cached view is used (well under 1 ms). If it is set, or the watcher reports an overflow or a dropped event, or the watcher is not running, the view is rebuilt from git as today.
- Events under our own index directory are ignored, otherwise our writes would set the flag on every query.
- Working-copy files are hashed once per content: a per-session cache keyed on the file's own size, modification time and inode, never trusted for a file modified in the last two seconds (the same guard git uses).

Expected result: about 5 ms for a query when nothing changed since the last one, and today's cost for the first query after a change.

## What would change this decision

- The watcher costs more private memory than the budget in ADR-005 allows, or cannot be made reliable on Linux with large trees (inotify needs one watch per directory). Then M1c ships option A alone and offers option C as an opt-in.
- A measured case where the flag is clear and the view is stale. That would be a correctness bug and the watcher would be removed, not patched.

## Consequences

- M1c's gate must include a test that edits a file and queries immediately, in a loop, and never sees the old content.
- The one-shot command line keeps option A; it has no process to hold a watcher.
