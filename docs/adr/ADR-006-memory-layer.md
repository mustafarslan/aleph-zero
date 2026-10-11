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

## Open points for the M6 spec

- Whether open-per-operation is fast enough at 5k and 50k events (M0 probe), or a session holds the store open for a whole exchange.
- Whether memory is per repository only, or also per developer across repositories.
- Stored conversation text is plaintext on disk and cannot yet be erased through Python. Acceptable for personal use; a consent and erasure story is required before public release.
- How a wheel is produced so that installing the `memory` extra does not need a C++ toolchain.

## Consequences

- A second persistent store sits beside SQLite and must be reconciled with it.
- Aleph gains a dependency that today needs a source build.
- Aleph's benchmark harness can give Aeon the recall-on-real-data numbers its paper lacks.
