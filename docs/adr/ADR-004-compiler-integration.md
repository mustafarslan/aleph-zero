# ADR-004: Compiler and language-server integration

- **Status:** LSP bridge rejected. SCIP accepted 2026-10-11 as import plus on-demand runs gated on measured memory; built in M9, with ingestion brought forward as the resolver's grading oracle.
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

## Open for M9

- Peak memory of each indexer on a mid-size project, and the limit under which Aleph will run it.
- Whether Java and Rust, which run the project's own build, are import-only in practice.
