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
| Embed worker, GPU, capacity 4,096 tokens | 600 MB | 279 MB idle, 478 MB after 4,100-token inputs |
| Embed worker, CPU fallback, capacity 2,048 tokens | 600 MB | 215 MB idle, 474 MB after 1,900-token inputs |
| Four sessions plus the worker | 1024 MB | 721 MB at full capacity; 532 MB when inputs stay under ~500 tokens |

This relaxes the original rule "no daemon" to "no always-on daemon": the worker is a helper that exits when nothing has used it for a while.

**The gate metric is private memory, not RSS.** With a 200 MB file mapped by two processes on the reference machine, RSS read 239 MB in each while the footprint stayed at 18 MB. `aleph_zero.memstat.private_bytes` reads `phys_footprint` on macOS and `Pss_Anon + SwapPss` on Linux. psutil's USS is not used: it read 228 MB with one mapper and 18 MB with two.

## Why the first decision was replaced

The first decision was that each session memory-maps the model on CPU so the OS shares the weights. M0 measured that and found three things.

1. **Weight sharing worked, and was not the problem.** With three processes on one file, both llama.cpp and ONNX Runtime kept the weights as shared pages.
2. **An unneeded buffer was.** llama.cpp reserves `vocabulary × output tokens` floats for logits even in an embeddings-only context. EmbeddingGemma 2 has a 262,144-word vocabulary, so that is 1.05 MB per token, kept at the size of the longest input seen: 1,119 MB after 820-token chunks on CPU and 1,335 MB on Metal. A one-line patch removes it (see ADR-001 and `docs/upstream/llama-cpp-logits-reservation.md`).
3. **The GPU is 6–8× faster for chunks and cannot share weights across processes.** On Metal each process holds its own copy (278 MB). The owner's rule is to use the GPU wherever it speeds things up, and there is no working in-process llama.cpp binding today (ADR-001). One GPU copy per machine is the only arrangement that satisfies all three.

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

Speed and idle memory:

| Configuration | After load | After queries | Query | 246-token chunk | 4,137-token chunk |
|---|---|---|---|---|---|
| Q8, Metal, 1 CPU thread | 277.9 MB | 280.0 MB | 8.5–9.0 ms | 16.7 ms (59/s) | 316 ms |
| Q4, Metal, 1 CPU thread | 278.9 MB | 280.0 MB | 9.1 ms | 17.4 ms (58/s) | 316 ms |
| Q8, CPU, 8 threads | 210.8 MB | 215.0 MB | 11.4 ms | 83.7 ms (12/s) | 2,244 ms |
| Q4, CPU, 8 threads | 157.3 MB | 161.5 MB | 7.9 ms | 96.8 ms (10/s) | not run |

On Metal the worker needs one CPU thread: eight give the same 59 chunks per second at 183% of a core instead of 17%, with the GPU at 85–88% utilization either way.

Lifecycle check, Metal over a UNIX socket, 500 mixed requests at capacity 4,096: private memory stayed between 478.2 and 479.2 MB; the process exited with code 0 on SIGTERM.

Alternatives measured and not chosen:

| Alternative | Per session | Four sessions | Query |
|---|---|---|---|
| ONNX Runtime in every session, sentencepiece tokenizer (q8) | ~190 MB | ~960–1070 MB | 46 ms |
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
