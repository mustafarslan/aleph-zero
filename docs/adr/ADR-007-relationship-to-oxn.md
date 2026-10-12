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

## Where each row stands (2026-10-12)

The owner's instruction when told of a tie: Aleph has to be better than oxn in every row. Each row was looked at again; three were not ahead, and they are named first (one of them is half closed since).

**Not ahead.**

- **Architectural context.** Precision at 3 on the 100 pairs is 0.820, oxn's figure exactly, in two runs (ADR-003, "Decision retrieval, measured"). By words alone the two rankers are one function. The token half of the bar is met: 355 tokens an answer. What would move the figure is not a parameter: a development set with enough cited decisions to choose a ranker on, a reranking model, or a query that includes the files being changed (outside oxn's protocol, so not comparable).
- **Entities, half closed.** Aleph has what the bar adds (constants, variables, fields, enums, chunks). Functions defined inside functions, and local names bound to a lambda or a closure, are answered by the code graph since M7b (ADR-003, "Inner functions": eslint's confident recall went from 48.5% to 81.3%, with every confident inner answer right on the six corpora). They are still not found by `symbol` or shown by `outline`, and a lambda with no name (passed straight as an argument) is not an entity at all; oxn records those for its metrics. Showing inner functions in `symbol` and `outline` changes what those print for every repository: the owner's choice (DEFERRED, entry 9).
- **Latency after an edit, on a large repository.** At the size oxn measured Aleph is ahead: on oxn's own repository 113 ms median (115 at the 95th percentile) from a cold process against oxn's 140 (150), and 18.5 ms warm against 62. On Django (6,833 files) it is 227 ms cold and 120 ms warm (177 and 70 with git's untracked cache on), above the 150 ms bar. Aleph asks git what changed, which grows with the tree; oxn's hook is told which file changed and reports a flat cost (88 ms at 1,827 files; no figure at this size). Three costs were removed while measuring (275 ms to 227 ms cold); what is left is git, the interpreter's start and SQLite's per-connection tables. A fourth was measured and not taken: loading only the modules a search needs would save at most about 15 ms of the 45 ms the imports take, which leaves Django at about 210 ms cold (160 with the untracked cache), still over the bar, for imports scattered through the engine. On a repository of this size the bar is reachable only with git's untracked cache on and a cheaper way to learn what changed, which ADR-008 tried (a watcher) and rejected.

**Ahead, with where the figure is.**

| Area | Standing |
|---|---|
| Languages | The six, plus C++ and C in search, symbols and outline. Their graph is not graded (no indexer for them here). |
| Stored edges | Built differently from this row's wording: per-file facts are stored and a call is resolved when asked (an edge depends on other files; ADR-003). Every answer carries its provenance, level and whether it is certain. Compiler indexes are kept and believed only while their files are unchanged (ADR-004). |
| Edge queries | `callers`, `callees`, the `references` tool (off for the agent until its gate; ADR-003). |
| Resolver accuracy | Confident precision 99.9 to 100% and confident recall above oxn's in every language, on oxn's protocol and corpora (ADR-003, "Against the bars"); since M7b confident recall is 81.7% on go-kit (oxn 64.1), 60.7% on ripgrep (42.3), 82.5% on nest (56.7). On the held-out Flask corpus confident precision reads 99.18% under the protocol since M7b: three answers at functions the compiler index gives one name (ADR-003, "Flask's three"). |
| Known resolver gaps | Each handled and each measured switched off alone (ADR-003). |
| Freshness, staleness | As built in M1 and M9: keyed by git blob, shared across worktrees, dirty files included; an answer says when a compiler index was not believed and why (ADR-004). |
| SCIP | Four indexers run on request inside a measured limit; JavaScript and TypeScript run once the owner names the memory they may take (eslint: 2,332 MB of 3,072 allowed). TypeScript's memory is still unmeasured. oxn runs five with no limit. |
| Architectural facts | Layers, contracts and `applies-to` are read (checked on oxn's own repository), plus routes, mounts, models, tables, environment names, tasks, queues and gRPC services (ADR-003). |

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

## What was ported in M1b

`aleph_zero.lang` holds the port (attribution in its package docstring: oxn v0.1.2 at `2c8ee6d`, MIT). It imports nothing from the rest of Aleph; a test enforces that.

| Piece | From oxn | Changed or added |
|---|---|---|
| `profile.py`, `languages.py` | The profile design, the node-kind tables for Python, TypeScript, TSX, JavaScript, Go, Rust and Java, `ImportSpec` | Metric tables left out. Added: C and C++ profiles; tables for values (constants, variables, fields, macros); the kind of each type node (class, interface, enum, alias, namespace); `holders`; `declarator_chain` |
| `definitions.py` | The containment walk, the bound-name rule for anonymous callables, receiver and `impl` ownership | Written for retrieval: signatures, doc comments and docstrings, line ranges that include decorators and the comment above. Callable bodies are not walked, so closures and locals are not name candidates (one of the oxn behaviours ADR-007 said not to copy) |
| `imports.py` | The whole extractor, copied and adapted | ECMAScript imports and re-exports record the names they take; `#include` for C and C++ |

Ported since (M7, 2026-10-12): the SCIP wire reader (`scip.py`), the grading protocol (`bench/eval/graph_grade.py`) and how Go, Rust and Java spell an import (`lang/bindings.py`). The L0/L1 resolver follows oxn's order of steps and its measured rules about qualified calls, and is otherwise written for Aleph (ADR-003).

**The parser library's own extraction was evaluated and not used.** `tree-sitter-language-pack` 1.21 offers `process()`, which returns structure, symbols and imports. On one sample file per language it returned unnamed classes and a wrong function name for C++, no constructors or fields for Java, no types or constants for Go, no docstrings for Python, and an unnamed TypeScript abstract class. A test keeps it as a floor: every named item it finds on the samples must be found by ours, and ours must find more. The library is used for parsers only.

**Known limit.** In a `.c` file, a prototype behind an unknown macro whose return type is a plain name (`API_MACRO my_type my_function(void);`) is read by the C grammar as a variable followed by a call, so the function is not recorded. Headers are read with the C++ grammar, which recovers it. Deferred to M3.

## A note for oxn itself

oxn requires `tree-sitter>=0.25` with no upper bound, and its environments have 0.26.0 installed. py-tree-sitter 0.26.0 segfaults when `start_point` and `end_byte` are read on every node during a recursive walk over `named_children` of large trees (Aleph pins 0.25.2 and carries a regression test, `tests/test_tree_sitter_stability.py`).

Checked on 2026-10-11: the published oxn 0.1.2 ran `oxn check` over Django's source (about 900 files) under tree-sitter 0.26.0 without crashing, and its output was byte-identical to the run under 0.25.2. oxn walks `children` with an explicit stack, which does not trigger the fault on this corpus. It is therefore not known to be affected. An upper bound below 0.26 is still prudent until the upstream cause is understood.

## Views consulted

**Advisor:** two products; do not chase the server start time; run SCIP indexers on demand; SCIP ingestion must precede the graded resolver; blob-keyed L2 invalidation is unsound as first proposed; port versus shared core is the owner's call, and a shared core is the more maintainable of the two.

**agy (no boost):** port, not depend or share; cross-file L1 edges are valid per view, not per blob; reject running indexers (on a memory figure that was never measured); adopt the hand-written server; the three resolver improvements listed above; the anti-patterns listed above.

**Where they differed:** shared core versus port (the owner chose port now, extract later); running indexers (the owner chose on demand, gated on measured memory); the hand-written server (measured once in M1).
