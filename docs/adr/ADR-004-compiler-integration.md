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

The protocol and the limit were written into the plan before the first run; three additions were written before the runs they concern. Each installed indexer ran once on its corpus of oxn's six, on a fresh copy. The metric is the highest summed resident memory, sampled every 100 ms, of the indexer and everything it started: an upper bound, since shared pages count once per process.

**The limit: 2,048 MB.** ADR-005 budgets 1,024 MB for what is always there. An indexer run is requested by the owner, runs alone and ends: it may take twice that at its peak. It is one setting (`ALEPH_SCIP_MEMORY_MB`).

| Language | Indexer | Corpus | Peak | Time | Index | Verdict |
|---|---|---|---|---|---|---|
| Python | scip-python 0.6.6 | httpx | 459 MB, 2 processes | 3.6 s | 2.8 MB | on request |
| Java | scip-java 0.12.3, Maven | spring-petclinic | 881 MB, 4 processes | 4.9 s | 0.8 MB | on request |
| Go | scip-go 0.2.7 | go-kit | 948 MB, 18 processes | 6.8 s | 3.2 MB | on request |
| Rust | rust-analyzer (2026-10-04) | ripgrep | 1,499 MB, 44 processes | 7.3 s | 8.3 MB | on request |
| JavaScript | scip-typescript 0.4.0 | eslint | 2,295 MB | 19 s | 55.6 MB | over the limit: not run by default; run once the owner names a limit |
| TypeScript | scip-typescript 0.4.0 | nest | not measured | | | not run by default; run once the owner names a limit |

**Revised 2026-10-12.** `ALEPH_SCIP_MEMORY_MB` was written here as the way to raise the limit for a run, but a language measured over the default limit was refused before the limit was read. It now runs once that variable names the memory the indexer may take, watched and stopped above it like the others (eslint: 17.6 s, peak 2,332 MB of 3,072 allowed, 1,470 documents). A `tsconfig.json` the indexer infers at the root of the worktree is removed again; one that was already there is left. The default limit did not move.

How these came about, in order:

- **First run, offline.** Python and Java (896 MB with `mvn clean verify`) ran. The other four could not: on a fresh copy their dependencies could not be installed without the network, and an indexer run without its project's dependencies resolves nothing outside the project, so it was not run.
- **Second run, after the owner allowed the network for installing dependencies.** Dependencies were installed with the network (not measured: `aleph scip run` never installs), then the indexer ran offline. Go, Rust and JavaScript were measured. nest's `npm ci` ends in a dependency-resolution error with or without the network; no other flags were tried, so TypeScript is not measured.
- **Java was measured twice** because the command changed after review (below): 896 MB with `clean verify`, 881 MB with `clean test-compile`, which is what is used.
- The figures move from run to run: later runs on the same corpora gave 497 and 487 MB for Python, 912 MB for Java, and 315 MB for Go once Go's build cache was warm against 948 MB cold. All under the limit; the table is the registered run.
- The limit was not revisited after eslint came in 12% over it. One corpus per language is thin either way: what keeps a project inside the limit is the guard, not this table.

### What was built

- **`aleph scip import FILE`** keeps, for each document, the blob it was made from, the documents it depends on with the blobs they had, and its references to definitions inside the tree: one compressed row per document (eslint's 54 MB index, 105,394 references: 0.6 MB; as one row per reference, 7.6 MB before indexes). What the index was made from must be known: `HEAD` when the worktree is clean, else `--tree REV` or `--worktree` must be said, because an index of an uncommitted edit stamped with HEAD would be believed the day the edit is undone. An index over 200 MB is refused: it is read whole, at about five times its size in memory.
- **The trust rule, as decided above.** A document is believed only if the file's blob and the blob of every document it depends on, directly or through others, are the view's. Each dependency is checked against the blob it had in the document's own import, so a later import of part of the tree cannot vouch for an earlier one. The walk stops at 2,000 files and then does not believe. Otherwise the resolver answers and the answer carries the reason and how to get exact answers back.
- **Joining the index to Aleph's call sites.** The index gives a line and a column; indexers count columns in UTF-16 units or bytes, Aleph in bytes, so a call records its column in each unit. The index's target must be a definition Aleph records that holds the line the index names and bears its name (for a constructor: the type or its constructor method); a target Aleph does not record, such as a class inside a function, is not replaced by a namesake and the resolver answers instead.
- **What a believed document says is outside** (a symbol of a package the index defines nothing of) gets no answer. A symbol of the index's own package with no document, as in an index of one package of several, is left to the resolver.
- **`aleph scip run`** runs the indexer for a language measured under the limit, in a session of its own, offline. Every 100 ms it lists the processes: the indexer's group, its descendants, and any process once seen among them that has since been left by its parent. Their memory is summed; above the limit, after 30 minutes, or when the processes cannot be listed, all of them are killed. A process started and abandoned between two looks is never seen: that window is a tenth of a second. The result is kept only if no file of the view changed meanwhile.
- **Builds run in a copy.** scip-java (Maven: `clean test-compile`, no packaging, no integration tests) and rust-analyzer (cargo, with the project's build scripts) run on a copy of the view's files, so nothing they clean or write touches the worktree. This is the project's code running on the machine, which is why it is never done except on request. Aleph installs nothing: `aleph doctor` lists what is present.

### The rule at work, and whether to soften it

On httpx with oxn's index imported (60 documents): 2,044 stored calls are answered from the index, each checked to be the definition the index names; 1,989 get no answer (a library, or nothing known); 10 point at something Aleph does not record; 48 fall to the resolver where the index says nothing. After a comment is appended to `httpx/_models.py`, not committed: 52 of the 60 files are no longer believed, the file itself and 51 that depend on it, and 2,298 calls are answered by the resolver with the reason.

That is strict: one edit to a central file sends 87% of the files back to the resolver. The owner asked agy and the advisor to decide whether to keep it.

- **agy:** keep the rule for what is called exact, and add a second, weaker stamp for a reference whose two files are unchanged ("compiler index, out of date"), plus the hint to re-index.
- **The advisor:** keep the rule; do not weaken what "exact" means; do not build the second stamp before measuring what it would add. Its case against believing an edge on its endpoints: `obj.run()` where `run` is declared on a base class and a class between the two gains an override; neither the calling file nor the files it refers to change.
- **The measurement** (registered before it ran: build the second stamp if it adds at least 10% of the downgraded calls rightly and under 2% of what it shows differs from a fresh index). On Flask, an index made 10, 50 and 200 commits before HEAD, against a fresh one: of 689 calls in files that had not changed but were no longer believed, the resolver answers 137 certainly, and the stale index would add 28 more rightly (4.1%) and none wrongly. Most of the rest are calls into libraries, where no index has an answer.

**Decided: the rule stays, with the hint, and no second stamp.** It would add four calls in a hundred. Re-making the index is the cure, and it costs seconds where Aleph may run the indexer.

### What the review of this milestone found

An independent reviewer reproduced, and the fix pass closed with a failing test first for each: a call joined to the wrong occurrence after non-ASCII text on its line; a namesake taken for a target Aleph does not record; a later partial import vouching for an earlier document; the guard blind to a child whose parent had left, and silent when `ps` failed; an index of part of a tree calling the rest a library; constructors never answered from the index; `mvn clean verify` run in the owner's worktree; a size cap five times what a session may hold. The first two are why "answered from the index" above is now checked against the index, not counted by its stamp.

### Answered and still open

- *Peak memory of each indexer on a mid-size project, and the limit:* five of six answered; TypeScript waits for a corpus whose dependencies install.
- *Whether Java and Rust are import-only in practice:* neither, once the machine holds their dependencies (Maven's local repository, cargo's registry). Gradle was not measured and is import-only.
- Not handled: a file added after the index was made that changes what an import resolves to (the rule checks the files the index knew). Two indexers covering one path: the newer import wins. A symbol defined in two documents (TypeScript declaration merging) depends on the first only. scip-java fetches its compiler plugin on first use, so its first run on a machine is not offline.
