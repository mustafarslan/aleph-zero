# ADR-004: Compiler and language-server integration

- **Status:** LSP bridge rejected. SCIP deferred to M9 as import-only.
- **Date opened:** 2026-10-11

## Question

How does Aleph Zero obtain compiler-grade symbol and reference data without breaking the no-daemon rule and the memory budget in ADR-005?

## Decided

**No LSP bridge.** The spec's ephemeral one-shot language-server client is dropped.

- Cold starts take seconds and steady-state memory runs from 832 MB (gopls on Kubernetes) to more than 12 GB (rust-analyzer on large workspaces).
- LSP has a one-client model; attaching to an IDE's running server needs a multiplexing proxy.
- Claude Code already ships a built-in LSP tool covering definition, references, hover, symbols and call hierarchy.

**SCIP is import-only.** Aleph Zero never runs a SCIP indexer.

- Indexers need installed dependencies and large heaps (scip-python suggests 8 GB, scip-typescript up to 16 GB, scip-clang about 2 GB per core).
- No indexer documents an incremental mode.
- `aleph scip import <file>` ingests an index produced elsewhere (CI or by hand), keyed by tree id.

## Decision

LSP bridge: rejected. SCIP import: pending M9, and only if the M4 A/B shows compiler-grade edges would help.
