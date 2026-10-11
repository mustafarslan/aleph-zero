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
- When built, the graph is an opt-in tool (`references`, callers and callees) whose edges are labeled *name-matched* or *import-resolved*.
- One-hop expansion is an explicit `search` parameter, off by default.
- Each graph feature ships only if the M4 A/B improves with it.

## Decision

Pending M7.
