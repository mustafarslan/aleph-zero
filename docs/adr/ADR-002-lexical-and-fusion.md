# ADR-002: Lexical index and hybrid fusion

- **Status:** Lexical part accepted for M1. Fusion part proposed, decided by M2 (Benchmark C).
- **Date opened:** 2026-10-11

## Question

How are lexical candidates produced, and how are symbol, path, lexical and dense candidates combined?

## Decided for M1

- SQLite FTS5 with per-column `bm25()` weights over `symbol`, `signature`, `doc`, `body`. This is weighted-TF BM25: `k1` and `b` are fixed and there is no per-field length normalization, so it is not true BM25F.
- Identifiers are expanded in Python before indexing and querying, keeping the whole identifier and adding its sub-tokens. Tokenizer `porter unicode61` with `_` as a token character; stemming was added in M1a because `invoice` did not match `Invoices`.
- Starting weights symbol 5 / signature 3 / doc 2 / body 1, treated as a grid to tune.
- Exact-symbol and path matches are separate candidate generators, replacing the spec's additive +0.20 and +0.30 bonuses.
- Generators are fused with Reciprocal Rank Fusion until tuning data exists.

## Evidence so far

- Whole identifier plus sub-tokens lifts BM25 NDCG@10 on CoIR-Go from 0.309 to 0.563; sub-tokens alone reach 0.469. Gains are 27.8% on Java and 0.3% on Python (Radha and Goktas, 2026).
- Sourcegraph boosts symbol and filename term frequency about ×5 and reports low sensitivity to the exact value. No academic ablation supports 4/2/1.
- Tuned convex combination beats RRF on text retrieval, with the dense weight best in 0.6–0.8 (Bruch, Gai and Ingber, 2023). Theoretical min-max is more robust than per-query min-max.
- No fusion study on code was found. On SWE-bench-Lite the lexical-versus-dense ordering flips by embedding model (CodeRAG-Bench), so a fixed weight does not transfer.
- Four off-the-shelf rerankers all lowered text-to-code quality; no reranker is planned.

## Measured on a real corpus (M1a)

`bench/spikes/lexical_latency.py` on Django (7,080 files in the view, 37,466 chunks), M4 Max, warm:

| Query set | FTS5 only, p50 / p95 | Whole search, p50 / p95 |
|---|---|---|
| 14 typical queries | 2.1 / 7.1 ms | 4.2 / 9.7 ms |
| 7 queries made of words found in most chunks (`self`, `return`, `if not self`) | 5.1 / 10.0 ms | 6.7 / 11.9 ms |
| 6 queries with a kind or path filter, filter seen for the first time | | 14.2 / 22.5 ms |
| The same filtered queries repeated | | 1.3 / 5.9 ms |

Bringing the index up to date with an unchanged view takes 0.9 ms.

The M0 storage spike used a uniform synthetic vocabulary, which could not show the cost of long posting lists. On real code the worst queries stay near 11 ms, so the lexical stage is not where a query's time goes: building the view (two git calls) takes about 70 ms of the same query (ADR-008). The view filter must stay written as `+rowid IN (...)`; without the unary plus the same query takes seconds.

## Measured with the language layer (M1b)

With source code chunked by definition, Django has 70,481 chunks (was 37,466 line windows), 51,427 symbols and 11,907 imports. Same machine, same spike:

| | p50 / p95 |
|---|---|
| Cold index of the whole repository | 10.3 s, 86 MB peak private memory (was 5.7 s) |
| 14 typical queries, whole search | 2.9 / 11.0 ms |
| 7 queries of words found in most chunks | 10.8 / 16.8 ms |
| 6 filtered queries, first use / repeated | 17.2 / 33.9 ms, then 1.6 / 9.6 ms |
| Sync of an unchanged view | 1.1 ms |
| Query process, private memory | 69 MB |

`bench/spikes/definition_check.py` asks the opposite question: for a random sample of functions, methods and types whose name is defined once, does searching for the bare name return that definition first?

| Repository | Language | Files | Chunks | Symbols | Cold index | Definition first |
|---|---|---|---|---|---|---|
| Django | Python | 7,080 | 70,481 | 51,427 | 10.3 s | 200 / 200 |
| FastAPI | Python | 3,182 | 23,567 | 7,994 | 3.4 s | 200 / 200 |
| Flask | Python | 236 | 1,955 | 1,143 | 0.3 s | 200 / 200 |
| httpx | Python | 125 | 2,210 | 1,335 | 0.3 s | 200 / 200 |
| go-kit | Go | 290 | 2,264 | 2,473 | 0.3 s | 200 / 200 |
| spring-petclinic | Java | 133 | 877 | 318 | 0.1 s | 160 / 160 |
| eslint | JavaScript | 2,303 | 19,814 | 5,277 | 3.5 s | 200 / 200 |
| tokio | Rust | 875 | 16,190 | 10,536 | 2.2 s | 200 / 200 |
| nest | TypeScript | 2,307 | 10,130 | 7,820 | 1.6 s | 200 / 200 |
| llama.cpp | C++, C, Python, TypeScript | 3,768 | 70,590 | 55,023 | 12.1 s | 199 / 200 |

The one miss is a C++ destructor (`~name`), which ranks behind the type of the same name. This is a sanity check on unique names, not a retrieval benchmark: the benchmark with real questions and a BM25 baseline is M1c's gate.

The check found three defects, each fixed with a test: files that open with the same lines shared their first chunk, and the path generator proposed that chunk once per file; a JavaScript `#private` member could not be found without its marker; and a C++ declaration behind an unknown export macro took its return type as part of its name.

## Fusion as built

- Three generators: lexical (FTS5), symbol (a name written in the query that is exactly a definition's name), path (files whose path matches the query's words).
- Lexical and symbol are fused by reciprocal rank over chunks; the symbol generator's vote counts twice (`GENERATOR_WEIGHT`). A file proposed for its path adds a vote to its own first chunk, in that file only.
- A word of a longer query that names more than 25 definitions (`get`, `main`) proposes no symbol; asked for by itself it proposes the ten best.
- Among definitions with one name: the exact spelling first, then types and functions before fields, then code before tests.
- The pieces of one long definition are one result. One chunk held by many files is listed in three of them and the other files are counted.
- A chunk's line range is tight for functions, methods and module-level code. A class chunk is what its methods do not cover, so its range is the class and may skip over them.

All of these are starting values, to be re-examined with the M2 benchmark.

## Known limits

- **Languages written without spaces.** Words are recognised in every script, and case and diacritics are folded, so German, Russian or Greek text is searchable. Chinese, Japanese and Korean text is indexed as whole runs between spaces and punctuation, so a query finds a run only if it equals it. Closing this needs a second index with FTS5's trigram tokenizer; it is planned for M3 if the benchmark repositories show a need.
- **Kind and path filters** narrow the candidates inside the query (a temporary table per filter, reused while the view is unchanged). They are not applied after ranking: a filter that few chunks pass would otherwise find nothing among the best fifty of everything.
- **Path matches.** A one-word query matches a path only through the file's own name, and the path generator proposes nothing when more than 25 files tie for best. Both rules are heuristics to be re-examined with the M2 benchmark.

## Open for M2

Whether convex combination with theoretical min-max, tuned on at least 50 of our own queries, beats RRF; and whether hybrid beats lexical-only by 5 points recall@10 on localization. If not, dense ships off by default.

## Decision

Fusion pending M2.
