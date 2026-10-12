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

Found by the review: a session that has the damaged file open may never read the damage, because its pages are in memory. It would go on indexing into the file that was set aside, where nobody else reads, and write vector rows numbered by that database into the file the new database uses. So:

- every operation compares the inode of the index path with the one it opened (one `stat`) and reopens when they differ;
- the vector files are set aside together with the database whose row numbers they follow, and a new database starts a new file;
- a vector store refuses to write when its connection's index file is no longer the one at the path;
- setting aside happens under a file lock, so the second of two sessions that saw the same damage cannot move the index the first has just made.

### Reading the index: one state, checked

Found by the stress run. A session used to check that the index held its view, and then search, in separate statements. A collection by another session in between removed what had been found, and the search answered without those files and without saying so.

- Search and symbol lookup now read inside one transaction, so every statement sees one state.
- That state counts only if nothing was committed by another session since the check (`PRAGMA data_version`, compared before and after), or if, looking again inside the transaction, every blob of the view is still there and no content has been removed since the list of chunks was built. Otherwise the index is synced again, up to three times; a last attempt that still finds the view incomplete answers from what is there and marks the answer as incomplete.
- The index counts removals in a `meta` table. Content that was collected and indexed again by another session comes back under new chunk ids; a session whose count is behind rebuilds its list of chunks. Before this it kept the old ids and found nothing.
- The same `data_version` makes the usual case cheaper: when the view is unchanged and no other session has written, the per-query check that joined the whole view against the index twice (2.7 ms on Django) is skipped (0.003 ms).

Inside a transaction `data_version` does not move with other sessions' commits (the transaction reads one state), so it is read outside the transaction, before and after. A write that changes no byte (`UPDATE t SET x = x`) does not change it; that is fine, nothing changed.

### What a view contains in unusual checkouts

A view is what git tracks in this worktree, as it is on disk now.

| Case | What Aleph does | Why |
|---|---|---|
| Sparse checkout | Files outside the checkout are left out; `status` says how many | They are not on disk and the agent cannot open them; in a blobless clone their content is not even local; the cone is the user's statement of scope |
| Blob git does not have | Counted as `unreadable`, asked for again once a minute, never waited for | It used to count as pending for ever, and every answer under a time limit was marked incomplete |
| Shallow clone, clone without trees | Nothing special: they work (test) | |
| Submodule | Not searched; `status` and `doctor` list them with `aleph source add <path>` | The linked-source mechanism already serves a checked-out submodule |
| Not a repository | Until M10: one sentence saying so and `git init`. Since M10 (below): searched as it is on disk, unless it is a directory that must not be walked, which gets that sentence; exit status 2; the server stays up | Listing, hashing and watching files without git was its own feature |

The design note said to keep files outside a sparse checkout and mark them. That is reversed here. The cost: an agent does not find a shared definition that lives outside its cone.

Which files a sparse checkout skips is read from git's index (`git ls-files -t --sparse`), only in a worktree that has a sparse-checkout file, and again only when that file or the index changes. Nothing is ever fetched: every git command runs with `GIT_NO_LAZY_FETCH=1`.

### Garbage is collected without being asked

After a sync that stored something, at most once a day per index, content no view has used for 30 days is dropped. One session takes the turn (a timestamp claimed under the write lock). A round works in short transactions for at most half a second and comes back a minute later for the rest. It does not run under a caller's time limit.

Content in use is protected by its last-seen mark, not by being the collector's own view: a session marks its view's content at least every 65 minutes while it is being queried. The two ages (65 minutes, 30 days) are far apart on purpose. `status` counts content that was collected under a session anyway (`lost_to_collection`); with the default ages it stays at zero.

### The vector file reuses rows

A row whose chunk is collected goes on a free list, by a trigger so that no path can forget it, and is given to the next chunk embedded before the file is extended. A search reads only the rows of chunks in its view, so nobody reads a free row. A unique index makes it impossible for two chunks to hold one row. The file stops growing and does not get shorter.

**The disk under free rows is given back (2026-10-12).** After a large branch was abandoned the file stayed at its high-water mark on disk. Three ways were weighed (agy and the advisor consulted): cutting the tail off (a session that has it mapped and touches it dies of a bus error: out); copying the rows in use into a new file and renumbering (complete, but a schema change and a protocol every reader must follow); and handing the file system the blocks that hold free rows only (`F_PUNCHHOLE` on macOS, `fallocate` with `PUNCH_HOLE` on Linux), which moves no row and changes no length, so no reader can notice. The third is built (`vectorfile.give_back`): after a garbage collection that removed chunks, and in `aleph gc`, under the index's write lock, and only for rows whose freeing is committed, because a hole cannot be taken back. A stretch of free rows is narrowed to the whole blocks inside it, so a block that also holds a row in use is never touched. Measured on this machine (APFS): 300,000 rows, 153.6 MB; 200,000 freed together and 16,667 scattered among rows in use: 51.2 MB on disk after 31 ms, the length unchanged. What it does not do: scattered free rows (eight rows share a block) give nothing back until they are used again; a file system without the call (exFAT, a network share) is said by `aleph gc` and nothing changes; a snapshot that still holds the blocks keeps the space until it expires; and a copy made by a tool that does not know sparse files is full size again. `aleph doctor` shows the length and what is on disk when they differ.

The first design was compaction into a new file generation. agy showed three ways a reader could pair one generation's rows with another's bytes (mapping after the old file is unlinked; a kept mapping that is "long enough" after a shrink; a row list cached under a counter that happens to match) and proposed reuse in place.

A mapping is of a file, not of a path: a search checks (one `fstat`) that the file at the path is still the one it has mapped, and maps again if not. Without that, a vector file deleted and made again (the way to shrink it) stayed readable through the old mapping, with the old vectors (found by the review).

One case is accepted: if content of a session's live view is collected anyway (only possible with `aleph gc --days 0` or similar) and its row is reused within the same query, that query ranks one chunk by another chunk's vector. The chunk shown is still a chunk of the view.

### A chunk's identity does not depend on the chunker's version

Until now the chunker's version was part of every chunk's hash, so any change to extraction would have thrown away every vector. The hash now carries its own format number (`HASH_FORMAT`, frozen at 3, the value the version had). A chunk that comes out of a newer chunker with the same kind, names and text keeps its id, its text-index row and its vector. On the Django checkout used for the benchmark, 52,574 of 52,674 vectors stayed valid after chunker version 4 re-processed it in 7 seconds.

Whoever changes how a chunk's words are indexed, or what text is embedded for it, must bump `HASH_FORMAT`. A test pins the present hash value.

Schema changes that keep the data are applied in place, one step at a time (4 to 5 to 6); only older schemas are rebuilt.

### The memory check

ADR-005 said each session reads its private memory after each call. Nothing did, and the reading cost 30 ms (it started the `footprint` tool). It is now one system call (`proc_pid_rusage`, the same figure), taken by the server after every answer has been sent. Over 130 MB (four sessions and the worker's 490 MB worst case inside 1,024 MB) the session lets go of what it can rebuild: connections to worktrees other than the one in use, SQLite's page cache, remembered trees and path words, the mapped vector file, parsers. It never refuses a call and never exits: a server that exits looks like a broken tool to the agent. The server's background embedding thread has its own connections, which this check does not reach; it closes them after a minute without calls. `status` shows the figure, the limit and how often memory was released.

### Language items carried from M1b

Methods of an object literal bound to a name are definitions (`api.get`), nested objects continue the name, and a function-valued key in an object that is exported or assigned (`export default { ... }`, `module.exports = { ... }`) is found by its key. Not in an object passed to a call: there the keys are options and hooks (`beforeEach` in every test file), as the review's comparison on Django's JavaScript showed. Every target of a Python chained assignment is defined. C declarations behind an unknown macro and C++ destructors by `~Name` already worked and are pinned by tests.

## The gate: thirty minutes, four sessions, three worktrees (measured 2026-10-12)

`bench/stress/run.py --minutes 30 --real-worker`: a real `aleph serve` process in each of three worktrees of one repository, each driven through edits, deletes, renames, commits, stashes and branch switches and checked after every step; a fourth session that only asks, about all three; vectors made in the background by the real embedding worker; garbage collected by the sessions themselves and by a separate process every 15 seconds, from each worktree in turn, with the ages shortened (content unused for 40 seconds is collected, content in use is marked every 5).

The run was made twice: before the independent review, and again on the code as merged, after the review's fixes. Both passed. The figures are of the second; the first are in brackets where they differ in kind.

| | Result | Required |
|---|---|---|
| Steps driven, each followed by checks | 26,566 | |
| Tool calls answered | 141,282 | |
| Calls that failed, lock errors, wrong or stale answers | 0 | 0 |
| Content collected while a session's view used it, the asking session included | 0 | 0 |
| Database integrity, foreign keys, text index, text-index rows = chunks, write-ahead log emptied | all clean | clean |
| Vector file: rows in use + free = rows in file, no row owned twice | 22,252 + 136 = 22,388 | equal |
| Collections that removed something | 119 runs, 3,018 blobs, 9,862 chunks | |
| Slowest single call | 227 ms | |
| Private memory, the four sessions | 71, 73, 86 and 98 MB at their highest (first run: 63 to 95) | 130 MB each |
| Private memory, worker and its supervisor | 323 MB and 15 MB | 600 MB |
| Private memory, everything together, at its highest | 628 MB (first run: 566) | 1,024 MB |
| Chip temperature (CPU and GPU share the die), read every 30 s | 61.6 to 64.1 °C, no thermal pressure | |

The session at 98 MB is the one that asks about all three worktrees: it holds a connection, with its page cache, per worktree, and so does its background embedding thread while calls keep coming. That is bounded, not a leak: 15,000 varied queries against one fixed index left a session at 38 to 40 MB throughout.

Runs of 90 seconds with the stand-in worker, in both modes, were part of development after every task. In the harsh mode (the collector takes content that is in use) 1,765 blobs were collected under live views in one such run and every answer was still right; that mode is what found the two faults described under "Reading the index".

What the gate does not show: more than three worktrees or four sessions; a repository of real size under this load (the stress repository grows from 150 to a few thousand files); Linux. What the harness cannot see, as the review pointed out: a wrong vector read for one query (words and names find the hit whatever the vector vote; the stored vectors are checked at rest, on a sample), and a transaction left open while the run is going (the write-ahead log is checked after the sessions have closed).

## Not done, on purpose

What needs Linux to verify (file names that are not valid UTF-8, the Linux branches of the memory and filesystem checks); two installed versions with different chunkers sharing one index; shrinking the vector file; a trigram index for text written without spaces. Each is in `docs/DEFERRED.md` with its reason.

## Views consulted

- **Advisor:** build the stress harness first and use it to find faults; verify what exists with a test instead of rebuilding it; decide the non-repository directory and submodules as rulings; ask agy three named questions rather than for a review.
- **Independent review of the branch (a fresh reviewer, most capable model):** found the stale mapping of a replaced vector file and the session left behind on a set-aside index (both reproduced, both fixed), the background thread's unbounded connections, the over-eager object-literal keys, and two weaknesses of the stress harness (its collector spared one worktree; the asking session was not counted). It checked the transaction logic of search against five interleavings and found it sound.
- **agy (no boost):** against file generations for the vector file, for reuse in place (adopted); four stress invariants (adopted: content of a live view is never collected by another session, foreign keys, text-index rows equal chunks, the write-ahead log can be emptied at the end); agreed with leaving files outside a sparse checkout out and with linking submodules by hand, and named the cost of each; warned that a clone without trees breaks the listing (checked: it does not, with a checkout) and that an unreadable blob stays pending for ever (confirmed and fixed).

## A directory that is not a repository (M10, 2026-10-12)

**What it is.** A view is a map from path to blob id and a way to read each path; a file that differs from HEAD was already read from disk and hashed. A directory outside any repository becomes a view in which every file is such a file. The index, the chunks and the vectors are the same; they live in the cache directory, because Aleph writes nothing into a directory that is not a repository. The path is taken only when git itself says the directory is in no repository: git missing, a bare repository, a repository git cannot read are the errors they were.

**The rule about what is walked**, fixed in the plan before any code, because a repository says what belongs to it and a directory does not:

- *Refused before anything is read:* the root and the directories directly in it, the top of any mounted disk, the temporary directories (`/tmp`, `/var/tmp` and `$TMPDIR` themselves), the home directory, and whatever holds the home directory. The home directory is recognised as the same directory, not by the spelling of its path (the default macOS file system takes `HOME` for `home`); when `HOME` is empty there is nothing to keep out of, and nothing is walked.
- *An error, not a plain directory:* a directory with a `.git` at or above it of which git says it is in no repository (a damaged `.git`, or `GIT_DIR` or `GIT_CEILING_DIRECTORIES` in the environment). Walking it would search what its `.gitignore` keeps out.
- *Refused after counting, with nothing indexed:* more than 20,000 files that would be searched, or more than 200,000 entries looked at to find them. The walk stops at the limit.
- *Left out:* everything whose name begins with a dot (without a `.gitignore`, nothing else says `.env` is not source), `node_modules`, `__pycache__`, `venv`, a directory marked with `CACHEDIR.TAG`, symbolic links, and repositories inside it (counted, and named in `status` and `doctor`). `build` and `dist` are searched: such a directory may be source.
- A folder that holds only repositories is refused with a sentence saying so. The directory searched is the one named, never a parent: unlike in a repository, `src/` searches `src/`.

**What was hashed is remembered** in a file beside the index (size, modification time, change time and blob id for each file; replaced whole by a rename). A file is hashed again when any of the three differs, or when its change time is not older than the walk that remembered it: so a file rewritten to the same size with its modification time put back is still seen, and a file edited in the very tick it was hashed is hashed once more on the next query.

**Measured** (`bench/eval/plain_directory.py`; 5,000 files, 62 MB, copied out of airflow with no `.git`; medians of 9): the first search, from a new process, hashes and indexes everything in 5.3 s. After that a new process answers in 149 ms with nothing changed and 161 ms after an edit, hashing no file whose size and times are as remembered; an open engine answers in 49 ms and 75 ms. The walk alone takes 38 ms there and 72 ms over 20,000 small files, the limit. The conditions set beforehand hold (an open engine within 150 ms on an unchanged directory; a new process hashes nothing unchanged; the walk at the limit under 100 ms). oxn has no figure to compare: it walks the directory on every run and has no rule about where.

**After the independent review.** It broke the rule in two ways the tests had not tried: the home directory reached under another spelling of its path was walked, and so was a real repository whose `.git` was damaged or hidden by the environment, ignored files included. Both are fixed as described above, with the smaller findings: an empty `HOME`, `/tmp` and disk tops that the first version did not refuse, a query under a time limit that never got further on a large directory (what was hashed is now kept when time runs out), a memory file that two threads of one process could garble (each write now has a file of its own to rename) or that crashed every query when damaged (an entry that cannot be believed is not), an unwritable cache directory (one sentence), a cache placed inside the searched directory (it is marked with a `CACHEDIR.TAG`, which the walk leaves out), and one directory of very many entries (counted while it is listed). The measurement run again afterwards: 148, 160, 48 and 74 ms; 37 and 70 ms for the walks.

**A second adversarial read, by agy (a different model family), after the merge.** Asked for ways round the rule that the first review had not tried, it named five; each was checked before it was believed (one of its claims, about `/Applications`, was wrong). Fixed, each by a test that failed first: system directories under their real names (`/var` is `/private/var`, three parts deep, and was walked: now a directory directly inside a system top directory such as `/private`, `/usr`, `/opt`, `/Volumes`, `/Users` or `/home` is refused, and nothing under `/System`, `/proc`, `/sys` or `/dev` is walked at all); macOS's second name for the whole disk (`/System/Volumes/Data`, under which the home directory lies too: the rule now reads a path through that name as the path it stands for); a path inside a repository that is a symbolic link to a directory outside it (the `.git` above the path as written counts); a directory whose `.git` is a link that points nowhere (still a repository's, not descended into); and `Node_modules` or `VENV` where case does not count. And a backstop that does not depend on any spelling: while walking, a directory that is the home directory by identity stops the walk with the refusal, and a directory on another disk is not entered.

**Known limits.** The memory of hashes trusts a file system's change time: on one that keeps times to the second or two (FAT, exFAT) a file rewritten to the same size within the tick of the walk can be missed, and on a share whose clock runs ahead every file is hashed on every query. (An engine that is open while its directory becomes a repository, or stops being one, now follows it to the index that belongs there.)

**What it does not do.** Compiler indexes and linked sources need a repository and say so. Nothing watches the directory: every query walks it (the 38 ms above), as every query in a repository asks git.

