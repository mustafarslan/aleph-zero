# ADR-002: Lexical index and hybrid fusion

- **Status:** Lexical part accepted for M1. Fusion part proposed, decided by M2 (Benchmark C).
- **Date opened:** 2026-10-11

## Question

How are lexical candidates produced, and how are symbol, path, lexical and dense candidates combined?

## Decided for M1

- SQLite FTS5 with per-column `bm25()` weights over `symbol`, `signature`, `doc`, `body`. This is weighted-TF BM25: `k1` and `b` are fixed and there is no per-field length normalization, so it is not true BM25F.
- Identifiers are expanded in Python before indexing and querying, keeping the whole identifier and adding its sub-tokens. Tokenizer `unicode61` with `_` as a token character.
- Starting weights symbol 5 / signature 3 / doc 2 / body 1, treated as a grid to tune.
- Exact-symbol and path matches are separate candidate generators, replacing the spec's additive +0.20 and +0.30 bonuses.
- Generators are fused with Reciprocal Rank Fusion until tuning data exists.

## Evidence so far

- Whole identifier plus sub-tokens lifts BM25 NDCG@10 on CoIR-Go from 0.309 to 0.563; sub-tokens alone reach 0.469. Gains are 27.8% on Java and 0.3% on Python (Radha and Goktas, 2026).
- Sourcegraph boosts symbol and filename term frequency about ×5 and reports low sensitivity to the exact value. No academic ablation supports 4/2/1.
- Tuned convex combination beats RRF on text retrieval, with the dense weight best in 0.6–0.8 (Bruch, Gai and Ingber, 2023). Theoretical min-max is more robust than per-query min-max.
- No fusion study on code was found. On SWE-bench-Lite the lexical-versus-dense ordering flips by embedding model (CodeRAG-Bench), so a fixed weight does not transfer.
- Four off-the-shelf rerankers all lowered text-to-code quality; no reranker is planned.

## Open for M2

Whether convex combination with theoretical min-max, tuned on at least 50 of our own queries, beats RRF; and whether hybrid beats lexical-only by 5 points recall@10 on localization. If not, dense ships off by default.

## Decision

Fusion pending M2.
