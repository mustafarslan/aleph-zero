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

## Known limits

- **Languages written without spaces.** Words are recognised in every script, and case and diacritics are folded, so German, Russian or Greek text is searchable. Chinese, Japanese and Korean text is indexed as whole runs between spaces and punctuation, so a query finds a run only if it equals it. Closing this needs a second index with FTS5's trigram tokenizer; it is planned for M3 if the benchmark repositories show a need.
- **Kind and path filters** narrow the candidates inside the query (a temporary table per filter, reused while the view is unchanged). They are not applied after ranking: a filter that few chunks pass would otherwise find nothing among the best fifty of everything.
- **Path matches.** A one-word query matches a path only through the file's own name, and the path generator proposes nothing when more than 25 files tie for best. Both rules are heuristics to be re-examined with the M2 benchmark.

## Open for M2

Whether convex combination with theoretical min-max, tuned on at least 50 of our own queries, beats RRF; and whether hybrid beats lexical-only by 5 points recall@10 on localization. If not, dense ships off by default.

## Decision

Fusion pending M2.
