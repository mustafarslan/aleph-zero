# ADR-002: Lexical index and hybrid fusion

- **Status:** Accepted. Lexical part in M1; fusion and the per-language gate for vectors in M2 (Python decided 2026-10-12 provisionally, and decided again on the held-out questions on 2026-10-13: vectors are not on by default; other languages have no verdict yet).
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
| llama.cpp | C++, C, Python, TypeScript | 3,768 | 70,590 | 55,023 | 12.1 s | 200 / 200 |

This is a sanity check on unique names, not a retrieval benchmark: the benchmark with real questions and a BM25 baseline is M1c's gate.

The check found three defects, each fixed with a test: files that open with the same lines shared their first chunk, and the path generator proposed that chunk once per file; a JavaScript `#private` member could not be found without its marker; and a C++ declaration behind an unknown export macro took its return type as part of its name.

## Fusion as built

- Three generators: lexical (FTS5), symbol (a name written in the query that is exactly a definition's name), path (files whose path matches the query's words).
- Lexical and symbol are fused by reciprocal rank over chunks, with equal weights. A file proposed for its path adds a vote to its own first chunk, in that file only.
- **A query that is one name** puts that name's definitions first, in the order the lookup gives them, ahead of everything else. Rank fusion cannot keep that order: first and second place differ by 1/61 against 1/62, so a short test double outranked `QuerySet.get_or_create` on text length alone.
- **In a longer query** only identifier-shaped words count as names (an underscore, a digit, an inner capital, a qualifier): in a sentence `total` is a word, `refresh_token` is a name. A name with more than 25 definitions proposes none.
- Among definitions with one name: the exact spelling first, then types and functions before fields, then code before tests.
- The pieces of one long definition are one result. One chunk held by many files is listed in three of them and the other files are counted.
- A chunk's line range is tight for functions, methods and module-level code. A class chunk is what its methods do not cover, so its range is the class and may skip over them.

All of these are starting values, to be re-examined with the M2 benchmark.

## Against a plain BM25 baseline (M1 gate, 2026-10-12)

`bench/eval/locbench.py` runs the 35 Django instances of Loc-Bench (the repository with the most instances). For each, the repository is checked out at the issue's base commit, the issue text is the query, and the question is whether the files the fix edited are among the first ten files returned. The baseline was fixed before it was run: one FTS5 row per file, default `unicode61` tokenizer, no column weights, no identifier expansion, every word of the issue OR-ed; it sees exactly the files Aleph indexes.

| | Aleph | Plain BM25 over whole files |
|---|---|---|
| Edited files in the first ten (recall@10) | 0.743 | 0.514 |
| ... in the first five | 0.671 | 0.343 |
| ... in first place | 0.500 | 0.171 |
| All edited files in the first ten | 0.714 | 0.514 |
| Mean reciprocal rank of the first edited file | 0.578 | 0.258 |

These figures were taken while asking Aleph for 50 results and reading the first ten files among them, which is deeper than an agent reads. Re-measured on 2026-10-12 asking for ten results, with the code as it stands after M2 and vectors off (`--k 10`, the default now): 0.743 in the first ten, 0.743 in the first five, 0.571 in first place, mean reciprocal rank 0.652. The comparison holds at the depth the product is used at.

M1's gate asks that Aleph at least match the baseline. It is 23 points ahead at ten and 33 points ahead in first place, without any embedding model. Thirty-five questions from one Python repository are a small sample: a difference of one question is 3 points.

**Long queries keep their rarest words.** An issue is hundreds of words. Taking the first 48 terms gave recall@10 0.757 in 172 ms; taking the 24 terms found in the fewest chunks (`lexical.select_terms`, from FTS5's own term counts) gives 0.743 in 118 ms. The quality difference is half a question; the time difference is a third. Variants that kept the first 8 to 16 words and filled up with rare ones scored 0.714 to 0.771, all inside the noise, so the simplest rule was kept. Queries of 16 terms or fewer are not touched.

## Benchmark C, Python: vectors pass the gate, provisionally (2026-10-12; superseded by the held-out check below)

`bench/eval/benchmark_c.py`, 235 Loc-Bench questions from 18 Python repositories (Django, yt-dlp, dask, sqlglot, prefect, mypy, prowler, modin, jax, diffusers, sentence-transformers, docling, flet, pydantic, sqlfluff, moto, trl, aiohttp). Each repository is read at the issue's base commit from git's object store; every commit was indexed and embedded beforehand. Aleph is asked for ten results, as an agent would ask (the default is eight). File recall@10 unless stated.

| Query form | BM25 over whole files | Aleph, words and names | Aleph with vectors | Gain from vectors (95% interval) |
|---|---|---|---|---|
| **Issue title** (the gate form) | | 0.598 | **0.653** | **+5.5 points (+1.3 to +9.8)** |
| Whole issue text | 0.403 | 0.516 | 0.646 | +13.0 points (+8.3 to +18.1) |

On titles vectors changed the answer for 29 questions of 235 (better for 21, worse for 8); on whole issues for 40 (better for 36, worse for 4). Resampling repositories instead of questions, since 18 repositories contribute very unevenly: +0.4 to +10.8 on titles, +6.4 to +20.4 on whole issues. On the 200 questions that were not in the pilot: +7.2 (+2.5 to +12.0) on titles.

**The rule was fixed on the 35-question pilot:** read the gate on the title form, the closest to what an agent types; pass at 5 points with a paired bootstrap interval that excludes zero. By its letter it passes: +5.5, interval +1.3 to +9.8.

**Why this is provisional, and how the first reading was wrong.** The first run of this benchmark asked Aleph for 50 results per question. That also made the candidate pool six times deeper than the product's (300 candidates per generator instead of 50) and gave +5.8. The independent review of M2 caught it and re-ran the questions at the product's depth: **+3.2 (−1.1 to +7.4), which does not pass.** Vectors need the deeper pool: +3.2 at 50 candidates, +4.1 at 100, +5.5 at 300, while words and names alone do not change (0.606, 0.602, 0.598). The pool costs little (a typical query on Django goes from 2.9 to 5.2 ms at the median), so the product now uses 300. But that number was chosen on these same questions, after seeing them. The 5-point bar lies inside every interval above: the data show that vectors help Python (the interval excludes zero in every reading at this depth) and cannot tell +5.5 from +2. The 165 Loc-Bench questions in repositories not yet fetched are the held-out check; that run is in `docs/DEFERRED.md`.

What else the numbers say:

- **The gain grows with the length and naturalness of the question**: small on a title, large on a whole issue, where it is clear of the bar in every reading. Vectors alone are worse than words and names on short queries (0.609 against 0.670 at the first depth) and earn their place only in combination.
- **Rank fusion stays.** A weighted sum of normalised scores was below it at every weight tried, though it ran without the name and path signals, so the comparison is not like for like.
- **Data files get no vectors** (chunks of files with no language profile). That was decided for cost: they were 58 to 72% of the text to embed in two repositories. The pilot's quality gain for it was measured on 35 questions that are part of this set and is not evidence by itself.
- Still unmeasured: the 50 hand-written questions; other languages; documentation questions; the split-threshold sweep of ADR-001; any tuning of the text ranking, which is as untuned as the fusion.

**Verdict recorded:** `dense_policy.VERDICTS["python"]` is on, marked provisional. It takes effect only where the embedding worker has been installed, which is itself a deliberate step.

## Benchmark C, Python: the held-out check does not pass (2026-10-13)

The 165 Loc-Bench questions of the 18 large repositories that the first run left out (vllm, pandas, scikit-learn, transformers, ray, scipy, keras, sympy, qiskit, ultralytics, matplotlib, numpy, mlflow, phidata, streamlit, pytorch-lightning, checkov, pypdf) were prepared overnight and run with the product's settings as they stand (ten results, a pool of 300). They are the check this record asked for: the pool had been chosen on the first 235.

| Query form | Questions | Words and names | With vectors | Gain from vectors (95% interval) | Better / worse |
|---|---|---|---|---|---|
| **Issue title** (the gate form), held out | 165 | 0.514 | 0.530 | **+1.6 points (−3.4 to +6.9)** | 11 / 8 |
| Issue title, the first 235 (as measured then) | 235 | 0.598 | 0.653 | +5.5 (+1.3 to +10.0) | 21 / 8 |
| Issue title, all 400 | 400 | | | **+3.9 (+0.6 to +7.2)** | |
| Whole issue text, held out | 165 | 0.486 | 0.569 | +8.3 (+3.4 to +13.3) | 18 / 3 |
| Whole issue text, all 400 | 400 | | | +11.0 (+7.5 to +14.6) | |

**By the rule fixed beforehand the gate is not met.** It asks for 5 points on titles with an interval that excludes zero. Held out: +1.6, and the interval holds zero. All 400 together: +3.9, clear of zero and under the bar. Vectors help search on Python, by about four points on a title-length query and eleven on a whole issue; that is less than what was asked of them before they cost a model, a GPU and minutes of embedding per repository by default.

**Verdict recorded:** `dense_policy.VERDICTS["python"]` is off. `--dense`, `ALEPH_DENSE=1` switch vectors on by hand, as before, and `aleph status` says why they are off. `aleph decisions` is not touched by this: a record's own vector is used whenever a worker runs, on its own evidence (ADR-003).

**What this reading rests on, and what it does not settle.** The two halves were scored by different versions of the code (the first 235 on 2026-10-12, before later changes to the index; their indexes are not up to date and the run left them out, saying so). Scoring all 400 with one version needs the first 235 prepared again; it cannot change the held-out figure. The bar itself (5 points) was set on a 35-question pilot and is the owner's to keep or move. And the same pattern shows in both halves: vectors are neutral on short queries and clearly useful on long ones. Switching them on by the length of the query, instead of by language alone, is a design to write down and measure before it is built; it is not built.

## The gate is per language (owner's instruction, 2026-10-12)

Whether search by meaning is on is decided for each project from the language most of its source is written in, using the benchmark for that language. A result on Python questions says nothing reliable about C++ or Go, and Loc-Bench is Python only.

- `aleph status` reports a worktree's main language and each language's share of its source (documentation and data files are not counted).
- `dense_policy.VERDICTS` holds one verdict per language, filled in from Benchmark C. A language without a verdict is off: nothing is switched on by default on the strength of another language's result. `--dense`, `--no-dense` and `ALEPH_DENSE=1|0` are the owner's override.
- The benchmarks are run in the order of the projects Aleph is activated on. Measured on 2026-10-12, by size of source: aleph-zero 99% Python; oxn 100% Python; private repository C 100% Python; aeon 63% Python, 35% C++. So Python first (under way), then C++.
- Question sets with the same shape as Loc-Bench (issue text, files edited by the fix) exist for the other languages: SWE-bench Multilingual (300 questions: Go 38, Java 35, Rust 35, C 27, JavaScript 25, TypeScript 17, C++ 12, and PHP and Ruby, which Aleph has no profile for) and Multi-SWE-bench (repositories in C, C++, Go, Java, JavaScript, TypeScript, Rust). About 35 questions per language cannot resolve a 5-point gate, so each language needs both sets. None has been fetched or run; they are listed in `docs/DEFERRED.md`.

## Known limits

- **Languages written without spaces.** Words are recognised in every script, and case and diacritics are folded, so German, Russian or Greek text is searchable. Chinese, Japanese and Korean text is indexed as whole runs between spaces and punctuation, so a query finds a run only if it equals it. Closing this needs a second index with FTS5's trigram tokenizer; it is planned for M3 if the benchmark repositories show a need.
- **Kind and path filters** narrow the candidates inside the query (a temporary table per filter, reused while the view is unchanged). They are not applied after ranking: a filter that few chunks pass would otherwise find nothing among the best fifty of everything.
- **Path matches.** A one-word query matches a path only through the file's own name, and the path generator proposes nothing when more than 25 files tie for best. Both rules are heuristics to be re-examined with the M2 benchmark.

## Open for M2

Whether convex combination with theoretical min-max, tuned on at least 50 of our own queries, beats RRF; and whether hybrid beats lexical-only by 5 points recall@10 on localization. If not, dense ships off by default.

## Decision

Fusion pending M2.
