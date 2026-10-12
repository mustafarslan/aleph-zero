# ADR-005: Memory budget and the shared embed worker

- **Status:** Accepted 2026-10-11, after M0 spike E. Supersedes the same-day decision to share the model by memory-mapping it in every session.
- **Date opened:** 2026-10-11

## Question

How much memory may Aleph Zero use when several agent sessions run at once, how is that measured, and where does the embedding model live?

## Decision

**Sessions hold no model. One embed worker per machine holds it, on the GPU when there is one, and serves every session's queries and all indexing.** It starts on demand and shuts down when idle. If it is absent or fails, sessions answer lexical-only and say so.

The budget is machine-wide:

`sessions × session baseline + embed worker ≤ 1024 MB`, for up to 4 sessions, with the worker itself ≤ 600 MB.

| Quantity | Limit | Measured (M0) |
|---|---|---|
| Session baseline (Python, MCP SDK, numpy, tree-sitter; no model) | target 150 MB, fail above 200 MB | 60.8 MB |
| Embed worker, GPU, capacity 4,096 tokens | 600 MB | 280 MB idle, 465–478 MB after inputs near capacity |
| Embed worker, CPU fallback, capacity 2,048 tokens | 600 MB | 214 MB idle, 459–474 MB after inputs near capacity |
| Four sessions plus the worker | 1024 MB | 708 MB at full capacity; about 530 MB when inputs stay under ~500 tokens |

This relaxes the original rule "no daemon" to "no always-on daemon": the worker is a helper that exits when nothing has used it for a while.

**The gate metric is private memory, not RSS.** In a probe run during planning on the reference machine (a 200 MB file mapped by two processes), RSS read 239 MB in each while the footprint stayed at 18 MB. `aleph_zero.memstat.private_bytes` reads `phys_footprint` on macOS and `Pss_Anon + SwapPss` on Linux. psutil's USS is not used: it read 228 MB with one mapper and 18 MB with two.

## Why the first decision was replaced

The first decision was that each session memory-maps the model on CPU so the OS shares the weights. M0 measured that and found three things.

1. **Weight sharing worked, and was not the problem.** With three processes on one file, both llama.cpp and ONNX Runtime kept the weights as shared pages.
2. **An unneeded buffer was.** llama.cpp reserves `vocabulary × output tokens` floats for logits even in an embeddings-only context. EmbeddingGemma 2 has a 262,144-word vocabulary, so that is 1.05 MB per token, kept at the size of the longest input seen: 1,119 MB after 820-token chunks on CPU and 1,335 MB on Metal (the Metal figure is from an ad-hoc probe). A one-line patch removes it (see ADR-001 and `docs/upstream/llama-cpp-logits-reservation.md`).
3. **The GPU is 5–8× faster for chunks (4.7× at 246 tokens, 6–8× at ~820), and its weights are not expected to be shareable across processes.** A Metal worker's private memory is 278 MB against 157–211 MB on CPU; weights offloaded to the GPU are not file-backed pages. This was not measured with several Metal processes; it is inferred from the larger private footprint. The owner's rule is to use the GPU wherever it speeds things up, and there is no working in-process llama.cpp binding today (ADR-001). One GPU copy per machine is the only arrangement that satisfies all three.

## Measurements (M0, 2026-10-11, Apple M4 Max, 12 performance cores, 64 GB)

Worker private memory, patched llama.cpp, by capacity (inputs near the capacity):

| Capacity | Metal Q8 | CPU Q8, 8 threads |
|---|---|---|
| 512 tokens | 289 MB | 294 MB |
| 1,024 | 305 MB | 350 MB |
| 2,048 | 343 MB | 474 MB |
| 4,096 | 478 MB | 754 MB (over the limit) |
| 8,192 | 947 MB (over the limit) | not run |

The worker keeps the footprint of the longest input it has processed, so the capacity sets its ceiling: 4,096 on GPU and 2,048 on CPU.

Speed and idle memory, final gate run (`bench/data/results/mem_share.json`; both configurations are required to pass):

| Configuration | Capacity | After load | After queries | After inputs near capacity | Query | 246-token chunk | Near-capacity chunk |
|---|---|---|---|---|---|---|---|
| Q8, Metal, 1 CPU thread | 4,096 | 277.9 MB | 280.0 MB | 464.5 MB | 8.6 ms | 16.5 ms (61/s) | 289 ms at 3,624 tokens |
| Q8, CPU, 8 threads | 2,048 | 210.8 MB | 213.9 MB | 459.3 MB | 11.3 ms | 77.5 ms (13/s) | 791 ms at 1,734 tokens |

Four sessions plus the worker: 707.7 MB on Metal, 702.5 MB on CPU. Earlier runs the same day with Q4 weights: Metal 278.9 MB after load, 9.1 ms per query, 17.4 ms per 246-token chunk; CPU 157.3 MB after load, 7.9 ms per query, 96.8 ms per chunk.

On Metal the worker needs one CPU thread: eight give the same 59 chunks per second at 183% of a core instead of 17%, with the GPU at 85–88% utilization either way.

Lifecycle check, Metal over a UNIX socket, 500 mixed requests at capacity 4,096: private memory stayed between 475.0 and 476.1 MB; the process exited with code 0 on SIGTERM.

Alternatives measured and not chosen:

| Alternative | Per session | Four sessions | Query |
|---|---|---|---|
| ONNX Runtime in every session, sentencepiece tokenizer (q8) | ~190 MB | ~760 MB private plus 200–314 MB of shared weights | 46 ms |
| ONNX Runtime in every session, HF tokenizer | ~300 MB (the tokenizer alone is 144 MB) | over budget | 20 ms |
| GPU model in every session | ~340 MB | ~1,360 MB | 9 ms |

Other M0 readings: MCP attach 1.36 s on the first launch and 0.25 s after. One server process per session is confirmed: two headless Claude Code sessions in two worktrees started two server processes, each a child of its own session, and both exited when their sessions ended.

## Worker requirements for M2

- **Private socket.** The server creates its socket file with mode 0755, so it must live in a directory only the user can enter.
- **Stale sockets.** The file is not removed on exit. A client that cannot connect takes a lock, checks the process is gone, unlinks the file and starts a new worker.
- **Start race.** Whichever session needs the worker first starts it under a file lock.
- **Idle shutdown.** `llama-server` can unload the model after idle (`--sleep-idle-seconds`); whether the process should also exit, and who decides, is settled in the M2 plan.
- **Over-length input.** The server rejects input longer than its capacity instead of truncating. The client truncates by tokens before sending; the chunker's split threshold (ADR-001) keeps this a rare safety net.
- **One connection.** The client keeps a single connection alive. Opening one per request exhausted the machine's ephemeral ports after about 16,000 calls.
- **Scope.** One worker per user and model, shared by every repository.
- **Failure.** A missing or crashed worker never blocks a query: the response is lexical-only and flagged.

## Mechanisms kept from the first decision

- Vectors are served from a memory-mapped sidecar file, shared by the OS across sessions: a 256-d scan takes 2.6 ms at 100k vectors and 7.9 ms at 300k with negligible private memory.
- A watchdog in each session reads its private memory after each call.
- Brute-force dense search caps a view at roughly 300k chunks.

## Views consulted

The advisor and agy (no boost) were consulted on the first decision and again on the failed gate. Both backed the shared worker once the measurements were in. The advisor identified the logits buffer from the arithmetic (262,144 × 4 bytes) before it was confirmed in the source; agy attributed the growth to lazy graph allocation, which the fixed 110 MiB compute buffer rules out. agy's main concern, a helper that lingers unmanaged, is addressed by the requirements above.

## Session memory with the language layer (M1c measurement, 2026-10-12)

Private memory of one process, each measured alone. "Cold" indexed the whole repository in that process; "warm" opened an existing index. Both then ran five queries.

| Repository | Files | Cold: after indexing and queries | Warm: after queries |
|---|---|---|---|
| nest (TypeScript) | 2,307 | 34.6 MB | 23.1 MB |
| tokio (Rust) | 875 | 34.6 MB | 22.0 MB |
| eslint (JavaScript) | 2,303 | 41.9 MB | 24.1 MB |
| Django (Python) | 7,080 | 54.5 MB | 35.7 MB |
| llama.cpp (C++, C) | 3,768 | 53.5 MB | 32.5 MB |
| airflow (Python) | 13,913 | 61.5 MB peak while indexing | 47.2 MB |

- **Grammars cost no private memory.** Each is a shared library mapped from disk; loading any of the nine left the footprint at 13.6 MB.
- **Two defects were found by this measurement and fixed.** A committed 108 MB file was read whole before being skipped as too large, leaving 150 MB behind; blobs over the size limit are now passed over in small steps. And the SQLite page cache was 32 MB per session for no measurable gain; it is 8 MB.
- With the hand-written MCP transport (ADR-009) a session's baseline is about 9 MB instead of 54 MB.

A session therefore sits at roughly 25–60 MB, against a 150 MB target. Four sessions plus the embed worker at full capacity come to about 480 + 4 × 60 = 720 MB in the worst case measured here.

**Latency on the largest of these (airflow, 13,913 files, 149,238 chunks):** cold index 30.7 s; building the view from git 105 ms p50 / 126 ms p95; a typical query after that 7.7 ms p50 / 21 ms p95. A query answered the stateless way is therefore about 147 ms at p95, at the edge of M1's 150 ms gate. The watcher in ADR-008 removes the git cost when nothing changed.

**The server as a whole (M1 gate).** `aleph serve` on airflow, 64 searches through the protocol: 124 ms p50 / 138 ms p95 with git's untracked cache off, 60 ms p50 / 75 ms p95 with it on; 47 MB of private memory. On Django: ready 45 ms after launch, 17 MB before the first call, 37 MB after eighteen searches. The gate was p95 under 150 ms warm on a 15,000-file repository and under 150 MB per session; airflow has 13,913 files and is the largest corpus at hand.

## The worker as built (M2, 2026-10-12)

**Who stops it.** `llama-server --sleep-idle-seconds` unloads the model but keeps the process: 278 MB awake, 106 MB asleep, 556 ms to answer the first request after sleep. Reading `/metrics` or `/health` does not wake it; `/slots` does. So the worker sleeps after two idle minutes, and a small supervisor process (15 MB) that owns it stops it after thirty, judging idleness from the request counter in `/metrics`.

**Staying one.** Starting takes a file lock. The state file records both process ids, and a process counts as ours only if its command line names our socket, so a recycled id is not mistaken for the worker. A healthy worker whose supervisor was killed is adopted by a new supervisor rather than doubled (macOS has no way to make a child die with its parent). A supervisor leaves when its worker dies. A failed start is reported with the server's own last words and not retried for a minute. The socket path is kept under the system's limit (104 bytes on macOS), falling back to a private directory under `/tmp` for a deep home directory.

**More slots, same ceiling.** One slot embedded Django's chunks at 72 per second. Eight slots give 104 to 116, but with the decode batch left at the context size the worker reached 954 MB when every slot was fed inputs at capacity. With eight slots of 2,048 tokens and decodes capped at 4,096 tokens the worst case is 490 MB, the same as one slot, and throughput stays at 104 per second. That is the configuration. An input over 2,048 tokens is cut by the client (it was 4,096 with one slot); chunks are cut at 256 tokens, so this touches only long documentation sections and files without a language profile.

| Configuration | Typical chunks | Every slot at capacity |
|---|---|---|
| 1 slot × 4,096 | 72 chunks/s, 314 MB | 479 MB, 2.9 inputs/s |
| 8 slots × 2,048, decode ≤ 4,096 (chosen) | 104 chunks/s, 381 MB | 490 MB, 7.8 inputs/s |
| 8 slots × 4,096, decode ≤ 8,192 | 116 chunks/s, 379 MB | 957 MB (over the limit) |

Several inputs in one request gain nothing (73 per second): the slots, not the request, are what run in parallel, so the client keeps one connection per slot.

**Installed size.** `aleph embed setup` builds the server statically in 25 s on this machine and leaves 665 MB under `~/.cache/aleph-zero/worker` (source, build tree, a 22 MB binary and the 310 MB model).

**Measured against the real model.** Cold start to first answer 3.2 to 4.0 s; a query vector in 6 ms when awake; Flask's 1,959 chunks embedded in 19 s.

**Views consulted** are recorded in the M2 plan: the advisor asked for the sleep behaviour to be measured before deciding who stops the worker; agy pointed out the orphaned worker on macOS and the recycled process id, which led to recording both ids and to adoption.

**A worker that grew and slowed with every request (found 2026-10-12, fixed).** During the benchmark's embedding run the worker, up for 4.5 hours, was doing 2,000 tokens per second against its average of 19,000 and held 1,008 MB of private memory. A fresh worker on the same job did 21,000 tokens per second with 408 MB. `bench/spikes/worker_uptime.py` reproduces the growth: with the server's defaults, memory rises by about 0.8 KB per request and does not level off (278 → 353 MB over 30,000 requests); with the prompt cache, context checkpoints and slot matching switched off (`--cache-ram 0 --no-cache-prompt --ctx-checkpoints 0 --slot-prompt-similarity 0`) it stays at 281 MB. Those features serve chat; for embeddings they only accumulate. The worker now runs with them off.

Independently of the cause, the supervisor now enforces the worker's 600 MB limit: it reads the worker's private memory at every poll, and a worker over the limit is stopped with the reason recorded, so the next request says why and starts a fresh one after the cool-down. A degraded worker can no longer go unnoticed for hours.

**Sleep after ten minutes, not two (2026-10-12).** With vectors on by default for Python, a worker that slept after two idle minutes would be asleep at most first queries after a pause, and a sleeping worker takes half a second to wake. A query now waits at most 0.3 s for its own vector: if the worker is waking or busy, that answer is by words and names (and says so), and the next query finds it awake. The worker sleeps after ten idle minutes and is stopped after thirty. Awake it holds 280 to 400 MB, which the budget allows: four sessions at 60 MB plus the worker at its 490 MB worst case is about 730 MB.
