# ADR-005: Memory budget and circuit breakers

- **Status:** Budget and mechanism accepted 2026-10-11. Measured values pending M0 spike E; breakers built in M3.
- **Date opened:** 2026-10-11

## Question

How much memory may Aleph Zero use when several agent sessions run at once, how is that measured, and what happens when it would be exceeded?

## Context

Claude Code starts one stdio server per session, so three sessions in three worktrees are three processes. The original rule, "RSS ≤ 400 MB per process", bounded each process and said nothing about the machine: three sessions each loading their own model would cost about three times one. (That one server starts per session is inferred from the docs; M0 counts the processes.)

## Decision

The budget is machine-wide:

`sessions × private + shared (model file + vector sidecar, counted once) + 600 MB while one indexer runs ≤ ceiling`

| Quantity | Limit |
|---|---|
| Concurrent sessions assumed | up to 4 |
| Private memory per session | target 150 MB, fail above 200 MB |
| Machine-wide steady state | 1024 MB |
| One indexer (transient, serialized by a file lock) | 600 MB |

**Sharing is by memory-mapping, with no extra process.**

- The model is a GGUF file loaded by llama.cpp on CPU with `mmap` on, `mlock` off and `n_gpu_layers=0`. Weight pages are file-backed and clean, so the OS keeps one copy for all sessions.
- Vectors are served from a memory-mapped sidecar file, `vectors-<model_id>.f16`, rebuilt by the indexer from the `embeddings` table and opened read-only by servers.

**The gate metric is private memory, not RSS.** RSS counts a mapped model in every process. With a 310 MB model mapped, a session would report about 390 MB RSS while owning about 80 MB, so an RSS rule would fail a configuration that is fine and would trip the watchdog on the model being shared.

| Platform | Per-process metric | Shared pages |
|---|---|---|
| macOS | `phys_footprint` (`footprint <pid>`, or `psutil` USS) | resident pages of the mapped files, counted once |
| Linux | USS and PSS from `/proc/<pid>/smaps_rollup` | same |

## Mechanisms

- **Lazy load, idle unload.** The lexical tier needs no model. The model loads on the first dense query and unloads after a few idle minutes; reloading a mapped file from the page cache is fast (measured in spike E).
- **Indexing in a child process.** Bulk embedding runs in `aleph index --worker`, which exits when its queue is empty, so its peak memory returns to the OS.
- **Watchdog.** After each call the server reads its own private memory. Above 200 MB it unloads the model and answers lexical-only, saying so in the response.
- **View cap.** Brute-force dense search limits a view to roughly 100–300k chunks. Beyond that, dense is disabled for the view and the response says why.
- **Enforcement.** A check fails the build when private memory per session exceeds 200 MB, four sessions plus shared pages exceed 1024 MB, the indexer exceeds 600 MB, or MCP attach exceeds 10 s on the reference repo.

## What breaks sharing (hypotheses for spike E)

- Metal or other GPU offload copies weights into device memory per process.
- `mlock` pins pages per process.
- Some quantization types are repacked into anonymous memory by the ARM CPU backend. Q8_0 and the K-quants are believed unaffected; the private-memory measurement confirms it.
- Per-process compute and activation buffers are always private.
- ONNX Runtime is suspected of copying external weight data into private heap at session start. Measured, not assumed.

## Consequence for the model choice

At 4 sessions near 200 MB private each, 1024 MB fits the 176 MB Q4 model (976 MB) and not the 310 MB Q8 model (1110 MB). Q8 fits only if private memory lands near 170 MB or below. ADR-001 takes this as an input.

## Alternatives considered

| Option | Verdict |
|---|---|
| Keep 400 MB per session, document that N sessions cost N times | Rejected: unbounded on the machine |
| Embed each query in a short-lived subprocess | First fallback if the runtime keeps private copies; costs latency on every dense query |
| Shared helper process that starts on demand and exits when idle | Last resort: start races, stale sockets, one crash affecting every session, per-repo versus per-machine scope |

## Sources of this decision

A review note raised the per-session gap. The advisor and agy (no boost) were consulted separately and agreed on memory-mapping and on gating private memory; they differed on numbers (advisor 200 MB and 1024 MB at 4 sessions; agy 150 MB and 850 MB at 3). The owner chose 1024 MB at up to 4 sessions with a 150 MB target and 200 MB fail line.
