# ADR-004: Compiler and language-server integration

- **Status:** LSP bridge rejected. SCIP accepted 2026-10-11 as import plus on-demand runs gated on measured memory. Built and measured in M9 (2026-10-12), ahead of the gate: see "As built and measured".
- **Date opened:** 2026-10-11

## Question

How does Aleph Zero obtain compiler-grade symbol and reference data without breaking the no-always-on-daemon rule and the memory budget in ADR-005?

## Decided

**No LSP bridge.** The spec's ephemeral one-shot language-server client is dropped.

- Cold starts take seconds and steady-state memory runs from 832 MB (gopls on Kubernetes) to more than 12 GB (rust-analyzer on large workspaces).
- LSP has a one-client model; attaching to an IDE's running server needs a multiplexing proxy.
- Claude Code already ships a built-in LSP tool covering definition, references, hover, symbols and call hierarchy.

**SCIP: import always; run on demand where memory fits.**

- `aleph scip import <file>` ingests an index produced elsewhere (CI or by hand).
- Aleph can also run an indexer itself in a short-lived process, per language, but only where a measurement shows the indexer's peak memory fits a stated limit. Aleph detects the indexer and tells the user how to install it; it never installs one.
- The wire reader is ported from oxn (hand-written, no protobuf dependency).

This replaces the first version of this ADR, which said Aleph never runs an indexer on the belief that indexers need 8–16 GB heaps. Nobody had measured that. oxn measured index *times* on real projects: Python 4.4 s, Go 1 s, TypeScript 3 s, JavaScript 20 s, Rust 40 s, Java 361 s cold and 6 s warm. It did not measure memory, so M9 opens with that measurement.

## When a compiler-grade edge may be trusted

A SCIP index is computed for a whole tree. What a call in file B resolves to can depend on file A's exports, a type hierarchy in file C, or an import that a new file shadows, even when B's own content has not changed. So keying an edge by the blob ids of its two endpoints is not enough.

- Each ingested index is stamped with the tree id it was computed from.
- A file's compiler-grade (L2) edges are exact in a view only if that file's blob and every blob in its transitive import set are the same as in the indexed tree.
- Otherwise that file is answered at the resolver's level (L1) and the answer says so. Edges are downgraded and labeled, never silently dropped.

## Order

SCIP ingestion is built before the name resolver's acceptance gate, because the resolver is graded against SCIP as ground truth (ADR-007). On-demand runs and the `references` tools' use of L2 edges follow in M9.

## As built and measured (M9, 2026-10-12)

### The measurement

The protocol and the limit were written into the plan before the first run. Each installed indexer ran once on its corpus of oxn's six, on a fresh copy, offline, after the project's own offline install of its dependencies. The metric is the highest summed resident memory, sampled every 100 ms, of the indexer and everything it started: an upper bound, since shared pages count once per process.

**The limit: 2,048 MB.** ADR-005 budgets 1,024 MB for what is always there. An indexer run is requested by the owner, runs alone and ends: it may take twice that at its peak. It is one setting (`ALEPH_SCIP_MEMORY_MB`).

| Language | Indexer | Corpus | Peak | Time | Index | Verdict |
|---|---|---|---|---|---|---|
| python | scip-python 0.6.6 | python-httpx | 459 MB, 2 processes | 3.6 s | 2.8 MB | on request |
| java | scip-java 0.12.3 | java-spring-petclinic | 896 MB, 4 processes | 5.5 s | 0.8 MB | on request |
| go | scip-go 0.2.7 | go-kit | not measured | | | import-only until measured |
| typescript | scip-typescript 0.4.0 | typescript-nest | not measured | | | import-only until measured |
| javascript | scip-typescript 0.4.0 | javascript-eslint | not measured | | | import-only until measured |
| rust | rust-analyzer 0.0.0 (65ac641199 2026-10-04) | rust-ripgrep | not measured | | | import-only until measured |

- **Four of six could not be measured here.** On a fresh copy their dependencies could not be installed without the network: Go lacked a module in the local cache, Rust a crate, and npm ended with a dependency-resolution error for both nest and eslint (whether the network would have changed that was not tried). An indexer run without its project's dependencies resolves nothing outside the project, so it was not run: a smaller figure from a degenerate run would say nothing. They are import-only until measured; finishing the measurement needs the network once, which is the owner's call (DEFERRED).
- The Java figure is with Maven's local repository already filled, offline. oxn's 361 s was a cold build.
- One corpus per language is thin. What keeps a larger project inside the limit is not this table but the guard below.

### What was built

- **`aleph scip import FILE`** keeps, for each document, the blob it was made from, the documents it depends on, and its references to definitions inside the tree: one compressed row per document (eslint's 54 MB index, 105,394 references: 0.6 MB; as one row per reference it would have been 7.6 MB before indexes). The stamp is the answer to "what was this made from": `HEAD` by default, `--tree REV`, or `--worktree` for an index made on the worktree as it is. A document whose path is not in that tree, or leaves the repository, is left out and counted.
- **The trust rule, as decided above.** A graph question believes a document only if the file's blob and the blob of every document it depends on, directly or through others, are the view's. The walk stops at 2,000 files and then does not believe. Otherwise the resolver answers and the answer carries the reason.
- **A believed document also knows what is outside.** A call the compiler places in a library gets no answer, where the resolver could only offer a method of the same name.
- **`aleph scip run`** runs the indexer for a language measured under the limit, in a session of its own, offline, sampling the memory of every process it starts; above the limit, or after 30 minutes, the process group and any descendant that left it are killed. The result is kept only if no file of the view changed meanwhile. Aleph installs nothing: `aleph doctor` lists what is present. For Java this runs the project's own build: that is the project's code running, which is why it is never done except on request.

### The rule at work

On httpx with oxn's index imported (60 documents), 2,044 stored calls are answered from the index, 2,005 get no answer (libraries, or nothing known), 42 fall to the resolver where the index says nothing. After a comment is appended to `httpx/_models.py`, not committed: 52 of the 60 files are no longer believed, the file itself and 51 that depend on it directly or through others, and 2,298 calls are answered by the resolver with the reason.

That is the rule doing what it says, and it is strict: in a package this connected, one edit to a central file sends 87% of the files back to the resolver until a new index is made. It was not relaxed. The first section of this record argues why an edge believed on its two endpoints alone can be wrong; whether a cheaper rule is worth that risk is a decision for when the feature is used.

### Answered and still open

- *Peak memory of each indexer on a mid-size project, and the limit:* answered for Python and Java; the limit is 2,048 MB; four languages wait for dependencies.
- *Whether Java and Rust are import-only in practice:* Java with Maven is not, once the local repository holds the dependencies (5 s, 896 MB). Gradle was not measured and is import-only. Rust is not measured.
- Not handled: a file added after the index was made that changes what an import resolves to (the rule checks the files the index knew). Two indexers covering one path: the newer import wins.
