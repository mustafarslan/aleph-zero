# ADR-003: Graph scope and depth

- **Status:** Deferred to M7 (code graph) and M8 (knowledge graph). Nothing graph-related is built in v1.
- **Date opened:** 2026-10-11

## Question

Should retrieval results be expanded with call-graph and architectural-graph neighbours, at what depth, and automatically or on request?

## Evidence so far

- Flattened 1-hop context raised task success in RepoGraph (29.67% against a 27.33% baseline); flattened 2-hop lowered it to 26.00% while costing 10.5k tokens.
- In LocAgent, removing graph traversal cost 2 points of file accuracy; removing the lexical index cost 13.
- Syntactic Python call graphs have high precision and low recall: 98.7% / 14.4% (Depends), 74.4% / 20.6% (Pyan). Inter-procedural analysis reaches 69.9% recall (PyCG).
- Only 10.3% of gold functions sit one hop from the entities named in an issue; 74.0% sit within two (KGCompass).

## Direction

- No automatic expansion of search results.
- The graph is an opt-in tool (`references`, callers and callees). One-hop expansion is an explicit `search` parameter, off by default.
- Each graph feature ships only if the M4 A/B improves with it.

## Edges (added 2026-10-11, after the oxn review; see ADR-007)

- **Every edge carries three stamps:** provenance (syntactic, import-resolved, SCIP), resolution level (L0 same-file, L1 cross-file by imports and names, L2 compiler-grade) and confidence (1 over the number of candidates for name-matched edges).
- **Edges are persisted.** oxn computes L1 in memory on every run and stores nothing unless a SCIP index was ingested; Aleph stores L0 and L1 edges so the tools can answer per-symbol queries.
- **Validity depends on the level.**
  - L0 edges depend only on the file's own blob, so they are keyed by blob id and shared across branches and worktrees.
  - L1 edges depend on the file and on what it imports, so they are valid per view. They are stored against the blob together with a fingerprint of the import set they were resolved in, and recomputed when that fingerprint differs.
  - L2 edges follow the rule in ADR-004.
- **The resolver is ported from oxn and extended**: multi-hop re-exports and barrel files (including Rust `pub use`), receiver calls resolved through local constructor bindings and typed parameters, and multi-package layouts (`go.work`, Python workspaces).
- **Private closures and lambdas are not project-wide candidates.**

## Acceptance

Graded against SCIP with oxn's protocol and corpora, per language:

| | oxn v0.1.2 (confident precision / confident recall) | Aleph's bar |
|---|---|---|
| Python | 100% / not reported | precision ≥ 99%, recall reported and above oxn's once measured |
| Go | 99.1% / 64.1% | precision ≥ 99%, recall > 64.1% |
| Rust | 99.1% / 42.3% | precision ≥ 99%, recall > 42.3% |
| TypeScript | 100% / 56.7% | precision ≥ 99%, recall > 56.7% |
| JavaScript | 99.8% / not reported | precision ≥ 99% |
| Java | 100% / not reported | precision ≥ 99% |
| C++ | not supported | reported |

Each of the three resolver improvements is measured separately, so a gain is attributed to its cause.

## Decision

Pending M7.
