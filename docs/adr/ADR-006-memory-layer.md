# ADR-006: Long-term memory on Aeon v4

- **Status:** Decision accepted 2026-10-11 (the owner chose Aeon v4). Integration shape proposed; confirmed or revised by the M0 Aeon probe, then specified in its own design document before milestone M6.
- **Date opened:** 2026-10-11

## Decision

Aleph Zero's long-term memory layer is Aeon v4 (`github.com/mustafarslan/aeon`, branch `v4`). Bugs found in Aeon are fixed on a branch off `v4`, pushed and merged later. Nothing is committed or pushed in the Aeon repository without the owner's explicit go-ahead, and work there follows Aeon's own `CLAUDE.md`.

## What is remembered

Side-chat questions and answers, notes the user inserted into the main conversation, and what the agent searched and concluded. Recalled across sessions by meaning and by recency. Expected volume: thousands of events per repository.

## Facts about Aeon v4 that shape the integration

Read from the source on branch `v4`; each was checked against the file named.

| Fact | Where |
|---|---|
| The Trace store works without a model: `append_event(..., embedding=...)` and `semantic_search(query, top_k)` take caller vectors | `shell/aeon_py/core.pyi` |
| Atlas `navigate_raw` is a tree descent returning one node per level, not top-k, so Trace is the memory store | `core/src/atlas.cpp` |
| Two processes opening one store is unsafe: no lock, counters cached at open, `BlobArena::close()` truncates to its own offset, compaction renames a new file over the path | `core/src/trace.cpp`, `core/include/aeon/blob_arena.hpp` |
| WAL replay assigns new event ids (`ev.id = next_event_id_++`) | `core/src/trace.cpp:1090` |
| The search index is rebuilt before the WAL is replayed, so crash-recovered events are not searchable | `core/src/trace.cpp:67-74` |
| Search over-fetches only `top_k + top_k/2 + 4` candidates and has no filter; results carry no score; a wrong-size query returns an empty list | `core/src/trace.cpp:619-646` |
| An event has no free metadata field; `session_id` is truncated to 35 bytes; `timestamp` is a monotonic clock, not epoch time | `core/include/aeon/schema.hpp`, `core/src/trace.cpp` |
| `tombstone_event` exists in C++ and the C API but is not bound in Python; there is no `close()` | `core/src/bindings.cpp` |
| Importing the package loads only numpy and requests, but the declared dependencies include sentence-transformers, FastAPI and cryptography, so a normal install downloads PyTorch | `pyproject.toml` |
| Source build only: C++23, CMake, SIMDe from the system; existing builds target Python 3.14 | `core/CMakeLists.txt` |

## Proposed integration shape

1. **In-process, one owner at a time.** Each Aleph server takes an exclusive `flock` on `<store>.lock`, opens `TraceManager`, performs its operations, drops the object so it closes, and releases the lock. No Aeon server, no daemon. A side-chat question and its answer are written under one lock, not two.
2. **Aleph's SQLite is the catalogue; Aeon is the episodic store.** A `memories` table holds what a Trace event cannot: kind, cited path, blob id and line range, a stale flag, a deleted flag, the full session id and epoch time. Recall asks Aeon for more candidates than needed and filters through SQLite.
3. **Aleph's own memory id is the key, not Aeon's event id**, until WAL replay preserves ids. The id is written as the first line of the event text and the catalogue is reconciled against the store on open.
4. **One store per repository**, under `<git-common-dir>/aleph-zero/memory/`, shared by all worktrees. Cross-repository memory is out of scope.
5. **Staleness comes from Aleph.** A memory cites code by path and blob id; when that blob leaves the view, the memory is flagged stale. This works with any store and is the one thing neither project has alone.
6. **Installed as an optional extra.** Aleph supplies 256-d L2-normalized vectors; Aeon loads no model.

## Aeon changes, on a branch off `v4`

**Needed before Aleph's first write**

| Change | Why |
|---|---|
| Move sentence-transformers, FastAPI, uvicorn, cryptography and JWT into optional extras | The base install must not download PyTorch |
| Replay the WAL before rebuilding the search index | Crash-recovered events are otherwise never searchable |
| Preserve event ids across WAL replay | Lets the catalogue key on Aeon's id and removes the workaround in point 3 |

**Worked around until fixed**

| Change | Workaround in Aleph |
|---|---|
| `session_id` truncated to 35 bytes, mismatching after reopen | Use 32-character hex session ids |
| `timestamp` is not epoch time | Always pass `event_time` |
| Search returns no scores and silently returns nothing for a wrong-size query | Aleph checks the dimension before calling; ranks by Aeon's order |
| No `close()` or context manager on `TraceManager` | Drop the object and collect before unlocking |

**Before stale memories accumulate**

| Change | Why |
|---|---|
| Bind `tombstone_event` in Python and log it in the WAL | Deleted and stale memories should be pruned inside the search, not after it |
| Set and honour the superseded flag on Trace events | Same, for memories replaced by newer ones |
| Fix the missing comma in `aeon_py/__init__.py`'s `__all__` | Two names are silently concatenated |

Not planned: a lock inside the kernel. The `flock` in Aleph covers it.

## Views consulted

**agy (boost), on whether to integrate at all:** keep Aeon out; a SQLite table covers cross-session recall with no new dependency; only the INT8 kernel has a concrete gain. Overruled by the owner's decision.

**agy (no boost), on the shape:** agrees with the lock, the per-repository store, the probe and the placement after the side chat. Warns that open-per-operation rereads every embedding on each open, that replay reassigns ids and leaves recovered embeddings unindexed, and that filtering only in SQLite reduces Aeon to a store for text and vectors; wants tombstones and scores treated as blockers because of the fixed over-fetch.

**Advisor:** agrees on the shape. A process crash loses nothing under the lock (dirty mapped pages stay in the page cache); only an OS crash can lose the unsynced header. The over-fetch risk is overstated because the caller can ask for more candidates; the sharper risk is id reassignment on replay. Reorders the fix list by what blocks the first write. Notes that at this volume Aeon's speed is not the reason to use it; the supersession and provenance model and the owner using their own project are.

**Where they differ:** agy treats tombstones and scores as blocking; the advisor treats them as needed before stale memories pile up. This ADR follows the advisor and records agy's point as the direction for the second iteration: once tombstone and supersede are bound, move the stale and deleted flags into Trace so Aeon prunes inside the index.

## Measurements (M0 probe, 2026-10-11, Aeon `v4` at `05e36d6`)

- **Build:** the core extension built out of tree for Python 3.13 in 2.5 s (CMake 4.3.1, Ninja, Homebrew SIMDe 0.8.2). The Aeon checkout was not modified.
- **Import:** loading the extension alone pulled in neither PyTorch nor sentence-transformers; the probe process ended at 75.5 MB private.
- **Open-per-operation is viable.** Lock, open, search, close: 1.9 ms at 5,000 events and 10.9 ms at 50,000. Lock, open, append, close: 2.2 ms and 10.4 ms. Bulk appends cost 0.08 ms each. Disk: 9.5 MB and 86.1 MB.
- **Nothing lost under the lock.** Two processes interleaving 500 appends each ended with 1,000 events, 500 distinct texts per session and unique ids.
- **Crowding is real and the over-ask fixes it.** With 60 events from one session and 5 from another sharing an embedding, `semantic_search(top_k=5)` returned none of the 5; `top_k=100` returned all 5. The catalogue filter therefore asks Aeon for 10–20× the results it needs.

## Open points for the M6 spec

- Whether memory is per repository only, or also per developer across repositories.
- Stored conversation text is plaintext on disk and cannot yet be erased through Python. Acceptable for personal use; a consent and erasure story is required before public release.
- How a wheel is produced so that installing the `memory` extra does not need a C++ toolchain.

## Consequences

- A second persistent store sits beside SQLite and must be reconciled with it.
- Aleph gains a dependency that today needs a source build.
- Aleph's benchmark harness can give Aeon the recall-on-real-data numbers its paper lacks.

## Aleph's side as built (M6a, 2026-10-12)

Built and tested before any change in Aeon, on the owner's standing instruction to keep working; the design draft (`docs/superpowers/specs/2026-10-12-m6-memory-layer-design.md`) is not yet approved, and its open decisions were taken with their proposed answers.

- **The catalogue** is a small database of its own, `memory/memories.db` in the repository's state directory, with a table of memories and a table of what they cite. **Not the index.** The first version put the tables in the index; the review pointed out that the index is a cache which is set aside and rebuilt when damaged or when its shape changes, and that would have taken every memory with it. The catalogue is the authority: a memory it marks deleted is never recalled, whatever the vector store still holds. It keeps the text too (the proposal above had the text in Aeon only): recall then works by words and recency without Aeon, and forgetting removes the text from the place Aleph reads.
- **Recall** fuses candidates by meaning from the store (asked for twenty times what is wanted, each id counted once) with matches by words among the newest 2,000 memories, newest first among equals. What the store proposes is looked up by itself, so a memory older than those 2,000 is still recalled by meaning. Ids the catalogue does not know are ignored. With a view, each memory says whether the content it cites is still at that path.
- **Forgetting** empties the memory's text in the catalogue, with SQLite's secure delete on, and empties the write-ahead log, where the old page would otherwise stay until a later checkpoint. **With Aeon as the store, a copy of the text stays in Aeon's file** (`memory/trace.blobs`) until Aeon can delete an event; nothing in Aleph removes that file. The command's help says so.
- **`AeonStore`** implements the store on `TraceManager` exactly as the probe did: exclusive lock, open, one operation, drop, unlock; Aleph's memory id is the first line of each event; session ids are cut to 32 characters; the event time is always passed. Aeon is found through `ALEPH_AEON_LIB` (a directory holding a build of its core extension) or as an installed package; Aleph never builds it. Tested against the extension the M0 probe built out of tree: a round trip across two sessions, two processes writing 60 memories each with nothing lost, a small group not crowded out, a store file removed (recall goes on by words). **Nothing was written or built in the Aeon checkout.**
- **Without Aeon** nothing fails: memories are kept and recalled by words and recency.
- **Vectors** come from the embedding worker when it is installed and running (never started for this); otherwise the memory is kept without one. A vector of the wrong size, or a store whose file was cut short, costs the vector and not the memory; the count of memories with a vector can then be too high.
- **Surface:** engine tools `recall` and `remember` (both on demand; steering rung 1 leaves them deferred), `aleph memory list | recall | forget [--all]`, counts and the store in `status`. Recalled text is set off line by line, so a memory cannot pass for another memory's heading or for the closing note. The side chat remembers each answered question with its citations, and each inserted note, and its pane says so; a failure to remember never costs the answer or the insert. Remembering waits for a vector no longer than a query does (0.3 s): the server answers one call at a time, and a search must not queue behind a memory.
- **Not built:** adopting store events the catalogue does not know, or any other reconciling of the two (an unknown id is simply ignored); recording the agent's own searches; recall inside the side chat's retrieval; memories that got no vector being embedded later; erasure inside Aeon's file (`forget` empties the catalogue's text; the event stays in Aeon's file until tombstones are bound).

**What still waits for the owner:** the three changes in Aeon listed above (they affect crash recovery and installation, not what was tested here), and the design's five decisions.
