# ADR-010: Hardening: many sessions, unusual checkouts, a damaged index

Status: accepted (M3, 2026-10-12)

## Question

What does Aleph do when its assumptions do not hold: several sessions write one index for a long time, git's directory cannot be written, the checkout is sparse or partial or has submodules, the index file is damaged, months of old content have piled up, a session's memory grows?

## Decisions

### One function says where a repository's state lives

`store.state_dir(common_dir)`: beside git's data (`<git-common-dir>/aleph-zero/`) when that directory can be written and is on a local filesystem; otherwise `~/.cache/aleph-zero/index/<hash of the directory>/`, with a `repository` file naming whose state it is. The index, the list of sources, the vector file and the embedding lock all ask it. `status` and `doctor` say where the index is and why.

- The place is decided by what is true now, not remembered. Every session of one user comes to the same answer because it depends only on the directory. If `.git` becomes writable later, a new index is started there and the cache copy is left behind.
- A network filesystem is detected from the mount (macOS `statfs`, the `MNT_LOCAL` flag; Linux `/proc/self/mountinfo`). SQLite's write-ahead log shares memory between processes through a mapped file, which such filesystems do not give reliably.
- Before this, a read-only `.git` ended in a traceback.

### A damaged index is set aside and built again

When SQLite reports corruption or "not a database", on open or in the middle of a query, the file is renamed to `index.db.damaged-<time>-<pid>` (kept, with its log files) and a new index is started; the operation that met the damage runs once more. A session remembers the inode it opened: if the file at the path is a different one by now, another session has already replaced it, and this one only reopens. `status` says the index was rebuilt.

### Reading the index: one state, checked

Found by the stress run. A session used to check that the index held its view, and then search, in separate statements. A collection by another session in between removed what had been found, and the search answered without those files and without saying so.

- Search and symbol lookup now read inside one transaction, so every statement sees one state.
- That state counts only if nothing was committed by another session since the check (`PRAGMA data_version`, compared before and after), or if, looking again inside the transaction, every blob of the view is still there and no content has been removed since the list of chunks was built. Otherwise the index is synced again, up to three times; a last attempt that still finds the view incomplete answers from what is there and marks the answer as incomplete.
- The index counts removals in a `meta` table. Content that was collected and indexed again by another session comes back under new chunk ids; a session whose count is behind rebuilds its list of chunks. Before this it kept the old ids and found nothing.
- The same `data_version` makes the usual case cheaper: when the view is unchanged and no other session has written, the per-query check that joined the whole view against the index twice (2.7 ms on Django) is skipped (0.003 ms).

SQLite reports `data_version` as of the start of a transaction, so it is read outside the transaction, before and after. A write that changes no byte (`UPDATE t SET x = x`) does not change it; that is fine, nothing changed.

### What a view contains in unusual checkouts

A view is what git tracks in this worktree, as it is on disk now.

| Case | What Aleph does | Why |
|---|---|---|
| Sparse checkout | Files outside the checkout are left out; `status` says how many | They are not on disk and the agent cannot open them; in a blobless clone their content is not even local; the cone is the user's statement of scope |
| Blob git does not have | Counted as `unreadable`, asked for again once a minute, never waited for | It used to count as pending for ever, and every answer under a time limit was marked incomplete |
| Shallow clone, clone without trees | Nothing special: they work (test) | |
| Submodule | Not searched; `status` and `doctor` list them with `aleph source add <path>` | The linked-source mechanism already serves a checked-out submodule |
| Not a repository | One sentence saying so and `git init`; exit status 2; the server stays up | Listing, hashing and watching files without git is its own feature (M10) |

The design note said to keep files outside a sparse checkout and mark them. That is reversed here. The cost: an agent does not find a shared definition that lives outside its cone.

Which files a sparse checkout skips is read from git's index (`git ls-files -t --sparse`), only in a worktree that has a sparse-checkout file, and again only when that file or the index changes. Nothing is ever fetched: every git command runs with `GIT_NO_LAZY_FETCH=1`.

### Garbage is collected without being asked

After a sync that stored something, at most once a day per index, content no view has used for 30 days is dropped. One session takes the turn (a timestamp claimed under the write lock). A round works in short transactions for at most half a second and comes back a minute later for the rest. It does not run under a caller's time limit.

Content in use is protected by its last-seen mark, not by being the collector's own view: a session marks its view's content at least every 65 minutes while it is being queried. The two ages (65 minutes, 30 days) are far apart on purpose. `status` counts content that was collected under a session anyway (`lost_to_collection`); with the default ages it stays at zero.

### The vector file reuses rows

A row whose chunk is collected goes on a free list, by a trigger so that no path can forget it, and is given to the next chunk embedded before the file is extended. A search reads only the rows of chunks in its view, so nobody reads a free row. A unique index makes it impossible for two chunks to hold one row. The file stops growing and does not shrink.

The first design was compaction into a new file generation. agy showed three ways a reader could pair one generation's rows with another's bytes (mapping after the old file is unlinked; a kept mapping that is "long enough" after a shrink; a row list cached under a counter that happens to match) and proposed reuse in place.

One case is accepted: if content of a session's live view is collected anyway (only possible with `aleph gc --days 0` or similar) and its row is reused within the same query, that query ranks one chunk by another chunk's vector. The chunk shown is still a chunk of the view.

### A chunk's identity does not depend on the chunker's version

Until now the chunker's version was part of every chunk's hash, so any change to extraction would have thrown away every vector. The hash now carries its own format number (`HASH_FORMAT`, frozen at 3, the value the version had). A chunk that comes out of a newer chunker with the same kind, names and text keeps its id, its text-index row and its vector. On the Django checkout used for the benchmark, 52,574 of 52,674 vectors stayed valid after chunker version 4 re-processed it in 7 seconds.

Whoever changes how a chunk's words are indexed, or what text is embedded for it, must bump `HASH_FORMAT`. A test pins the present hash value.

Schema changes that keep the data are applied in place, one step at a time (4 to 5 to 6); only older schemas are rebuilt.

### The memory check

ADR-005 said each session reads its private memory after each call. Nothing did, and the reading cost 30 ms (it started the `footprint` tool). It is now one system call (`proc_pid_rusage`, the same figure), taken by the server after every answer has been sent. Over 130 MB (four sessions and the worker's 490 MB worst case inside 1,024 MB) the session lets go of what it can rebuild: connections to worktrees other than the one in use, SQLite's page cache, remembered trees and path words, the mapped vector file, parsers. It never refuses a call and never exits: a server that exits looks like a broken tool to the agent. `status` shows the figure, the limit and how often memory was released.

### Language items carried from M1b

Methods of an object literal bound to a name are definitions (`api.get`), nested objects continue the name, and a function-valued key in an unnamed object is found by its key. Every target of a Python chained assignment is defined. C declarations behind an unknown macro and C++ destructors by `~Name` already worked and are pinned by tests.

## The gate: thirty minutes, four sessions, three worktrees (measured 2026-10-12)

`bench/stress/run.py --minutes 30 --real-worker`: a real `aleph serve` process in each of three worktrees of one repository, each driven through edits, deletes, renames, commits, stashes and branch switches and checked after every step; a fourth session that only asks, about all three; vectors made in the background by the real embedding worker; garbage collected by the sessions themselves and by a separate process every 15 seconds, with the ages shortened (content unused for 40 seconds is collected, content in use is marked every 5).

| | Result | Required |
|---|---|---|
| Steps driven, each followed by checks | 26,634 | |
| Tool calls answered | 141,528 | |
| Calls that failed, lock errors, wrong or stale answers | 0 | 0 |
| Content collected while a session's view used it | 0 | 0 |
| Database integrity, foreign keys, text index, text-index rows = chunks, write-ahead log emptied | all clean | clean |
| Vector file: rows in use + free = rows in file, no row owned twice | 22,389 + 0 = 22,389 | equal |
| Collections that removed something | 119 runs, 2,954 blobs, 9,658 chunks | |
| Slowest single call | 224 ms | |
| Private memory, the four sessions | 63, 72, 75 and 95 MB at their highest | 130 MB each |
| Private memory, worker and its supervisor | 314 MB and 15 MB | 600 MB |
| Private memory, everything together, at its highest | 566 MB | 1,024 MB |

The session at 95 MB is the one that asks about all three worktrees: it holds a connection, with its page cache, per worktree, and so does its background embedding thread. That is bounded, not a leak: 15,000 varied queries against one fixed index left a session at 38 to 40 MB throughout.

Runs of 90 seconds with the stand-in worker, in both modes, were part of development after every task. In the harsh mode (the collector takes content that is in use) 1,765 blobs were collected under live views in one such run and every answer was still right; that mode is what found the two faults described under "Reading the index".

What the gate does not show: more than three worktrees or four sessions; a repository of real size under this load (the stress repository grows from 150 to a few thousand files); Linux.

## Not done, on purpose

Searching a directory that is not a repository; what needs Linux to verify (file names that are not valid UTF-8, the Linux branches of the memory and filesystem checks); two installed versions with different chunkers sharing one index; shrinking the vector file; a trigram index for text written without spaces. Each is in `docs/DEFERRED.md` with its reason.

## Views consulted

- **Advisor:** build the stress harness first and use it to find faults; verify what exists with a test instead of rebuilding it; decide the non-repository directory and submodules as rulings; ask agy three named questions rather than for a review.
- **agy (no boost):** against file generations for the vector file, for reuse in place (adopted); four stress invariants (adopted: content of a live view is never collected by another session, foreign keys, text-index rows equal chunks, the write-ahead log can be emptied at the end); agreed with leaving files outside a sparse checkout out and with linking submodules by hand, and named the cost of each; warned that a clone without trees breaks the listing (checked: it does not, with a checkout) and that an unreadable blob stays pending for ever (confirmed and fixed).
