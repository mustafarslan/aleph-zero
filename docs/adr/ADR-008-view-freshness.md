# ADR-008: How a query learns what the worktree looks like

- **Status:** Accepted. Revised 2026-10-12: the watcher chosen below for M1c was built, measured and rejected; see "Outcome in M1c" at the end. The view is rebuilt from git on every query, in a form git's own untracked cache can speed up.
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
- **Up to 50 ms more appears on a freshly checked-out tree.** Files written in the same second as the index look "possibly changed" to git, so it re-reads them. Git normally fixes this by rewriting the index, but we call it with `GIT_OPTIONAL_LOCKS=0` so that a background reader never holds `index.lock`. The cost disappears the first time the user or the agent runs any ordinary git command. It is time only: git still reports the right answer. ("Up to 50 ms" was Django's figure. Measured on 2026-10-13 on a fresh worktree of vscode, 5,176 files: 340 to 390 ms for every status until an ordinary git command has run, 36 ms after one, 13 ms with the untracked cache in the index. See "The fresh-checkout cost, measured again" at the end.)

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

## Views consulted

**agy (no boost):** ranked the options E > A > C > D > B > F. Listed seven ways option D returns a stale view (nested directories, nested and global ignore files, symlinked directories, case-only renames, hard links, overwrites within timestamp resolution, sparse checkouts). Confirmed that `git status` under `GIT_OPTIONAL_LOCKS=0` is always correct and only slower. Called the `index.lock` collision risk of option B severe. Proposed option E in the form adopted here: events as a flag only, a full rebuild when it is set, overflow sets it.

**Advisor:** rejected option D for the same reason (the invalidation rules would be ours to maintain, and git already enforces them). First recommended option B, run sparingly, and option C as an opt-in; withdrew both after agy's lock-collision point, noting that the untracked cache also lives in the index file a background reader cannot write. Placed option E in M1c, where a long-lived process exists, and asked for the per-session hash cache keyed on a file's own stat.

**Author (Claude):** profiled the steps above and initially leaned towards option D. Dropped it after both reviews. Added the synchronous event flush before trusting the flag, since event delivery is asynchronous and an edit made just before a query must not be missed.

**Where they differed:** only on option B, and on when to build option E (agy: now; advisor: with the server in M1c). All three converge on rejecting D and on E as the design.

## Outcome in M1c (2026-10-12): the watcher is rejected, git's untracked cache is the fast path

**The watcher was built and failed its own condition.** Option E is sound only if every change has produced an event by the time a query asks. `bench/spikes/fsevents_lag.py` changes a file, asks at once, and keeps asking, flushing the event stream synchronously before each answer:

| Kind of change | Events per directory | Events per file |
|---|---|---|
| Same file rewritten | 6 of 80 about one second late | 4 of 80 |
| Different files | 3 of 80 | 2 of 80 |
| File replaced atomically | 0 of 80 | 5 of 80 |
| New files | 8 of 80 | 0 of 80 |

Nothing was ever lost, and the usual delay is 0.1 ms, but between 1% and 10% of changes were reported about 1,010 ms late. A synchronous flush does not help: the delay is before the event reaches the service that is flushed. So for up to a second after an edit, "no event" does not mean "nothing changed", and an agent that edits a file and searches at once could be answered from the old content. This is the case named under "What would change this decision", so the watcher was removed rather than patched. An idle check also cost 3 ms, not the sub-millisecond assumed.

**What replaced it is option C, in the form that needs nothing from us.** Git has its own cache of untracked files (`core.untrackedCache`), kept correct by git with the same directory-timestamp technique option D would have re-implemented. Two facts make it usable from a background reader:

- It is only consulted for `--untracked-files=normal`, where a wholly untracked directory is one entry. Aleph asked for `all`. It now asks for `normal` and lists such directories with a second call (`git ls-files --others --exclude-standard`), which yields the same files.
- Git can read the cache without writing the index. Aleph still never takes `index.lock`; the cache is refreshed by the user's and the agent's ordinary git commands. After files were created in three directories and two tracked files edited, with no index write possible, the call still took 20.8 ms.

Measured on Django (7,080 files):

| | `git status` alone | Whole view build, p50 / p95 |
|---|---|---|
| Untracked cache off (git's default) | 66 ms | 74 / 84 ms |
| Untracked cache on | 20 ms | 26 / 37 ms |

**Decision.** The view is rebuilt from git on every query: exact, with no state of ours to go stale. Aleph does not change the user's git configuration. `aleph doctor` reports whether the untracked cache is on and what turning it on would save, and `aleph status` shows it. Turning it on is one command for the user: `git config core.untrackedCache true`.

**What is given up:** the "5 ms when nothing changed" that the watcher promised. A query costs about 30 ms on a 7,000-file repository with the cache on and about 80 ms with it off.

**Left open:** on Linux, inotify delivers events synchronously from the kernel queue and might not have this lag. It was not built, because it cannot be tested on this machine; if it is ever added it must pass the same "change, then ask at once" test before it is trusted. The agent's own edits could also be reported to the engine by the Claude Code mod (M4) through its tool hooks; that would be exact for those edits but not for the user's editor, so it cannot replace the git check either.

## The fresh-checkout cost, measured again (2026-10-13)

Found while asking why the plugin's rung 2 was out of time on TypeScript repositories: in the A/B harness every question spent 320 to 620 ms on the view, all of it in one `git status`.

| A fresh worktree of vscode (5,176 files), `git status` as Aleph asks (`GIT_OPTIONAL_LOCKS=0`) | Time |
|---|---|
| Right after `git worktree add`, four times | 372, 384, 339, 323 ms |
| The same 2.5 s later | 387, 345, 338 ms |
| After one ordinary `git status` (which rewrites the index) | 37, 36, 36 ms |
| With the untracked cache in the index | 14, 13, 13 ms |

So the figure above was low by a factor of six on a repository of this size, and waiting does not help: the files stay "possibly changed" until some git command that may write the index has run.

**The decision stands: Aleph never takes `index.lock`** (option B, above). What follows from the measurement:

- *Who meets it.* The first questions after `git clone`, `git worktree add` or a checkout that rewrites many files, in a large repository, until any ordinary git command runs: a shell prompt that shows the branch's state, an editor's git panel, the agent's own `git status` or `git diff`. In a worktree someone works in, that is seconds. A worktree made by a script and only ever read by Aleph stays slow for good.
- *What does not heal it.* `aleph index`, `aleph update` and the hooks it prints: they are Aleph's git calls and write no index.
- *What does.* Any ordinary git command that comes a second or more after the checkout ended (git compares whole seconds: an index rewritten in the second the last file was written in keeps that file "possibly changed"; on django a status at once left 180 ms a status, one a second later 66); and `git update-index --untracked-cache` (what `aleph setup` offers) takes the cost to 13 ms either way.
- *The A/B harness* was in exactly the state nobody's worktree stays in: it makes a worktree, indexes it and starts a session, and no ordinary git command ever runs. It now waits a second and runs one `git status` in the worktree first, in both arms (`bench/ab/run.py`, `as_after_a_first_git_command`). Pass 1's Python worktrees were in that state too; its median for rung 2's question was 154 ms against a limit of 400, so its result is not moved.
- *Not built, a question for the owner (the deferred list, entry 3):* a hook that runs one ordinary `git status` after a checkout would heal it, and takes the lock this decision refuses to take from a background reader. In a hook the user's own git command has just ended, so the collision is less likely than from a query, and it is still theirs to choose.

## Git's two calls side by side (2026-10-14)

A view asks git twice: for the repository's facts (`rev-parse`: 9 to 11 ms) and for what differs from HEAD (`status`). They ran one after the other. `status` is now started before `rev-parse` is waited for (`gitio.StatusAhead`): two processes at once, no thread, nothing kept between questions. It can be started where the question was asked from, since git gives `status` paths from the worktree's top wherever it runs; what is listed afterwards (the files of a wholly untracked directory) is asked for at the top, and a test asks for a view from a sub-directory. Where there is no repository the process has ended by itself and is only reaped. Measured, settled worktrees: vscode's view 45.6 → 38.9 ms, django 74.6 → 67.8, material-ui 110.8 → 101.8. agy (boost) proposed it over remembering the facts between questions, which it showed to be wrong: HEAD's tree is among them and would go stale after a commit.

## Sparse checkouts, partial clones, submodules (M3, 2026-10-12)

What a view contains in those cases, and how a query makes sure the index it reads still holds the view it checked, are in ADR-010.
