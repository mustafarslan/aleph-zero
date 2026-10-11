# ADR-007: Relationship to Oxygen (oxn)

- **Status:** Accepted 2026-10-11.
- **Date opened:** 2026-10-11

## Context

Oxygen (`oxn`, `github.com/mustafarslan/oxn`, MIT, v0.1.2) is the owner's architecture and quality gatekeeper for coding agents. It parses six languages with tree-sitter into a SQLite code graph, computes complexity, size, class and architecture metrics, and holds agents to ceilings and layer contracts through a post-edit hook and four read-only MCP tools. It does not retrieve code.

Aleph Zero and oxn overlap in their foundations (per-language parsing, import and name resolution, SCIP, a code graph, freshness without a daemon), not in their purpose.

## Decision

1. **Two products.** Aleph Zero is retrieval and navigation; oxn is metrics and gating. Aleph does not grow a metrics gate.
2. **Wherever Aleph does something oxn already does, Aleph's version must be measurably better**, by the criteria below.
3. **Port now, extract later.** The shared pieces are copied from oxn into Aleph with attribution and extended there: the per-language profiles, import extraction and module resolution, the L0/L1 name resolver, and the SCIP wire reader. They live in one self-contained package (`aleph_zero.lang`) with no dependency on the rest of Aleph, so they can be lifted into a shared core both projects import if both keep evolving.
4. **oxn's graded evaluation harness is ported too**, as the guard against the two copies drifting and as the acceptance test for criterion 2.
5. **SCIP: import, and run on demand where memory fits** (ADR-004).

Aleph does not depend on the `oxn` package: its API is unpromised, its schema version has changed fifteen times, and it brings typer, pydantic, rich and pyyaml.

## What "better than oxn" means, per area

| Area | oxn at v0.1.2 | Aleph's bar |
|---|---|---|
| Languages | python, typescript (+tsx), javascript, go, rust, java | The same six plus C++ |
| Entities | file, module, class, interface, function, method, lambda | Those plus constants, variables and fields, and chunks |
| Stored edges | Only after a SCIP index is ingested; the L1 resolver's results are never stored | L0/L1 edges are persisted; every edge carries provenance, resolution level and confidence |
| Edge queries | None: no per-symbol query, no tool | `references`, callers and callees tools |
| Resolver accuracy, graded against SCIP | Confident precision 99.1–100%; confident recall 42.3% (Rust), 56.7% (TypeScript), 64.1% (Go); overall precision at full recall 52.8–90.4% | Confident precision no lower in any language, confident recall higher in every language, on the same protocol and corpora |
| Known resolver gaps | Rust `pub use`, multi-hop re-exports, receiver calls (`x.method()`), Python multi-package and Go multi-module layouts | Each handled, each measured separately |
| Freshness | Cache per checkout keyed by path and content hash; every file read and hashed per run; not worktree-aware; discovery by `os.walk` | Keyed by git blob id, shared across worktrees, discovery through git (so ignore rules apply), dirty files included |
| Staleness of compiler-grade edges | Editing a file deletes its SCIP edges until the next full index; nothing marks it | Every answer states which level produced it; a file's L2 edges are labeled stale, never silently dropped |
| Latency after an edit | 140 ms median for a cold one-file check | A one-file change is reflected in under 150 ms cold |
| SCIP | Runs five indexers or ingests a file; memory never measured | Same capability, with indexer memory measured and bounded |
| Architectural context | BM25 over constraints and ADR text; budget of 7 items; precision@3 0.82 on 100 pairs | Above 0.82 on the same 100 pairs, with a token budget |
| Architectural facts | Layers, contracts, ADR `applies-to` | Those (read from `oxn.yaml` and ADR frontmatter when present) plus routes, ORM models and env reads |

## What it does not mean

- **A metrics gate in Aleph.** That is oxn's purpose.
- **Full call coverage.** Without type inference there is a ceiling; oxn's 42–64% confident recall reflects a real limit that Aleph raises but cannot remove. Remaining calls are answered at lower confidence and labeled.
- **Compiler-grade edges keyed only by blob.** What a call resolves to can change when a third file changes. See ADR-004 for the sound rule.
- **Matching oxn's 14 ms server start at any cost.** oxn hand-wrote its MCP server because it runs as a cold process after every edit. Aleph's server lives for a session; 60.8 MB and 0.25 s attach are acceptable. A hand-written transport is measured once in M1 and adopted only if it materially cuts the session baseline.

## What Aleph does not copy from oxn

- `os.walk` discovery and the per-checkout, path-keyed cache.
- Rebuilding the import graph and symbol table in memory on every run.
- Deleting a file's edges on re-parse without marking the loss.
- Registering private closures and lambdas as project-wide name candidates, which inflates the `1/n` candidate sets.
- A full-table edge update on every file write.

## Order

SCIP ingestion comes before the resolver's acceptance gate, because the resolver is graded against SCIP: ingest first, grade second, tools third. The symbol and import port moves into M1, where it immediately improves the `symbol` tool and lexical ranking. Cross-file resolution and stored edges stay in M7.

## A note for oxn itself

oxn requires `tree-sitter>=0.25` with no upper bound, and its environments have 0.26.0 installed. py-tree-sitter 0.26.0 segfaults when `start_point` and `end_byte` are read on every node during a recursive walk over `named_children` of large trees (Aleph pins 0.25.2 and carries a regression test, `tests/test_tree_sitter_stability.py`).

Checked on 2026-10-11: the published oxn 0.1.2 ran `oxn check` over Django's source (about 900 files) under tree-sitter 0.26.0 without crashing, and its output was byte-identical to the run under 0.25.2. oxn walks `children` with an explicit stack, which does not trigger the fault on this corpus. It is therefore not known to be affected. An upper bound below 0.26 is still prudent until the upstream cause is understood.

## Views consulted

**Advisor:** two products; do not chase the server start time; run SCIP indexers on demand; SCIP ingestion must precede the graded resolver; blob-keyed L2 invalidation is unsound as first proposed; port versus shared core is the owner's call, and a shared core is the more maintainable of the two.

**agy (no boost):** port, not depend or share; cross-file L1 edges are valid per view, not per blob; reject running indexers (on a memory figure that was never measured); adopt the hand-written server; the three resolver improvements listed above; the anti-patterns listed above.

**Where they differed:** shared core versus port (the owner chose port now, extract later); running indexers (the owner chose on demand, gated on measured memory); the hand-written server (measured once in M1).
