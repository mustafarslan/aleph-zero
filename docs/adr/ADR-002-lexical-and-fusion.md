# ADR-002: Lexical index and hybrid fusion

- **Status:** Accepted. Lexical part in M1; fusion and the per-language gate for vectors in M2 (Python decided 2026-10-12 provisionally, and decided again on the held-out questions on 2026-10-13: vectors are not on by default; C++ decided 2026-10-13: on; the other languages have no verdict yet).
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
| Issue title, the first 235 (as measured then, the figures of the section above) | 235 | 0.598 | 0.653 | +5.5 (+1.3 to +9.8) | 21 / 8 |
| Issue title, all 400 | 400 | | | **+3.9 (+0.6 to +7.2)** | |
| Whole issue text, held out | 165 | 0.486 | 0.569 | +8.3 (+3.4 to +13.3) | 18 / 3 |
| Whole issue text, all 400 | 400 | | | +11.0 (+7.5 to +14.6) | |

The rows for all 400 are worked out here from the two result files, with the same resampling (10,000 paired resamples, seed 20261012).

**By the rule fixed beforehand the gate is not met.** It asks for 5 points on titles with an interval that excludes zero. Held out: +1.6, and the interval holds zero. All 400 together: +3.9, clear of zero and under the bar. Vectors help search on Python, by about four points on a title-length query and eleven on a whole issue; that is less than what was asked of them before they cost a model, a GPU and minutes of embedding per repository by default.

**Verdict recorded:** `dense_policy.VERDICTS["python"]` is off. `--dense`, `ALEPH_DENSE=1` switch vectors on by hand, as before, and `aleph status` says why they are off. `aleph decisions` is not touched by this: a record's own vector is used whenever a worker runs, on its own evidence (ADR-003).

**What this reading rests on, and what it does not settle.** The two halves were at first scored by different versions of the code (the first 235 on 2026-10-12; their indexes were not up to date and the run left them out, saying so). The first 235 were then prepared again (under three minutes: their content had not changed, so their vectors were kept) and all 400 scored by one version (`bench/data/results/benchmark_c_400.json`, 2026-10-13): titles +3.9 (+0.6 to +7.3), better for 32 and worse for 16; the first 235 by themselves +5.5 as before, the held-out 165 +1.6 as before; whole issues +11.0 (+7.5 to +14.7). The figures above stand to the digit. The bar itself (5 points) was set on a 35-question pilot and is the owner's to keep or move. And the same pattern shows in both halves: vectors are neutral on short queries and clearly useful on long ones. Switching them on by the length of the query, instead of by language alone, is a design to write down and measure before it is built; it is not built.

## Benchmark C, C++: vectors pass the gate (2026-10-13)

Questions for the other languages come from three public sets read into this benchmark's shape (`bench/eval/question_sets.py`, where what a question and its answer are is written down). For C++ they give 108 questions in five repositories (fmt 40, nlohmann/json 40, simdjson 16, Catch2 11, cpp-httplib 1; at most 40 a repository). How the result would be read was written before the run: the same gate (the question's first line, 5 points of file recall@10, a paired interval that excludes zero), the product's settings (ten results, a pool of 300), and "an interval that holds zero is not a pass". Nothing was ever tuned on C++, so its first reading is already at settings chosen elsewhere. All 108 were prepared and scored; none was left out.

**The table is of the second run.** The first counted four fmt questions twice: Multi-SWE-bench and SWE-bench Multilingual both hold pull requests 2310, 3272, 3750 and 3863, each at a base commit of its own, and doubles were told apart by repository, commit and answer. It read +8.1 points (+3.1 to +13.5) and switched C++ on. The selection now asks a pull request once whatever commit a set gives it at, and an issue that two pull requests answer once (`question_sets.select`); the sample of 40 fmt questions was drawn again from the corrected pool, and the run made again on 108 distinct questions with the reading unchanged and written down again before it.

| Query form | Words and names | With vectors | Gain from vectors (95% interval) | Better / worse | Resampling repositories |
|---|---|---|---|---|---|
| **First line** (the gate form) | 0.389 | **0.456** | **+6.7 points (+1.4 to +12.3)** | 15 / 3 | +3.8 to +13.8 |
| Whole issue text | 0.449 | 0.515 | +6.6 (+1.8 to +12.0) | 16 / 4 | +3.7 to +9.6 |

**By the letter the gate is met**, by less than the first run said: the mean is 1.7 points over the bar and the interval's lower end is 1.4 points from zero. Two of the five repositories give 80 of the 108 questions; resampling repositories instead of questions the interval stays clear of zero.

**Verdict recorded:** `dense_policy.VERDICTS["cpp"]` is on. It takes effect in projects whose main language is C++ and where the embedding worker is installed. C is another language with its own questions (110 in six repositories) and is not run; a `.h` file counts as C++, as everywhere in Aleph. One consequence to know: a C project with more header files than `.c` files has C++ as its main language and so gets this verdict, though C was not measured; `ALEPH_DENSE=0` switches vectors off there.

What else was measured, and what it does not say. The engine's answers are read on ten results, and ten results name about six files (6.6 with words and names, 6.2 with vectors, on first lines), because several results lie in one file. Vectors alone were first read to ten files, however many chunks that took, and at that depth they reach 0.494; an earlier version of this section set that beside the fusion's 0.493 and concluded that the fusion was leaving recall on the table. That compared unlike things. Read as the engine's answers are, on their first ten chunks (6.0 files), vectors alone reach 0.430 on first lines against the fusion's 0.456 (+2.6 points for the fusion, interval −1.9 to +7.4) and are behind it at first place (0.192 against 0.257; +6.5, interval +0.4 to +12.7). So on C++ the fusion is not behind vectors alone; what the comparison did show is that ten results cover few files, which is a question about how results are laid out and not about vectors (recorded in the deferred list, entry 1c). 108 questions from five repositories is a small set: the interval is eleven points wide, and a sixth repository could move the mean by more than the 1.7 points that separate it from the bar. There is no second, held-out set for C++ as there was for Python.

## Benchmark C, Go, TypeScript, JavaScript and Java (2026-10-13, released by the owner)

The owner's words: "we can use opensource peojects with mid size of repo for languages javascript/typescript, java and go and do benchmarks there". The questions are those of the three public sets (at most 40 a repository), and the reading is C++'s, written again for the four before anything was embedded (the deferred list, entry 1b): the first line, 5 points of file recall@10, a paired interval that excludes zero; whole text, the interval by repository and the repositories at or under 50,000 chunks beside it, deciding nothing. "Mid size" was fixed as at most 150,000 chunks a commit before the chunks were counted; counted, no repository is over it, so the limit left nothing out and the reading on the smaller repositories is what answers to the owner's word. `bench/eval/verdict.py` reads every language the same way and gives C++'s recorded verdict to the digit.

| Language | Questions, repositories | First line: words and names | With vectors | Gain (95% interval) | Better / worse | By repository | Whole text: gain | Verdict |
|---|---|---|---|---|---|---|---|---|
| **Go** | 110, 8 | 0.500 | 0.520 | +2.0 points (−3.8 to +7.9) | 17 / 13 | −8.2 to +8.2 | +18.2 (+11.1 to +25.5), 31 / 4 | **not shown: off** |
| **TypeScript** | 165, 11 | 0.355 | 0.456 | **+10.2 points (+5.3 to +15.2)** | 32 / 5 | +6.2 to +15.8 | +9.9 (+5.5 to +14.6), 27 / 3 | **passes: on** |
| **JavaScript** | 230, 12 | 0.445 | 0.482 | +3.7 points (−1.4 to +8.7) | 39 / 22 | −1.7 to +9.9 | +12.4 (+7.7 to +17.2), 47 / 11 | **not shown: off** (by label; **on** since the same evening, see the next section) |
| **Java** | 296, 18 | 0.469 | 0.470 | +0.1 points (−3.2 to +3.4) | 28 / 29 | −3.8 to +4.3 | +5.3 (+1.5 to +9.2), 46 / 19 | **not shown: off** |

**Go.** Embedded in 29 minutes (the chip at 76 °C at most, no waits), all 110 scored. On first lines vectors are not shown to help: the interval holds zero and nearly as many questions get worse as better. On whole issue text they add 18 points, the largest such gain measured so far, and first place on first lines rises from 0.165 to 0.245. It is Python's pattern again, more sharply: little for a title, much for a whole issue. All eight repositories are under 50,000 chunks, so the reading beside is the same reading. cli gives 40 of the 110 questions.

**TypeScript.** Embedded in two and a half hours (the chip at 77 °C at most, no waits; paused twice by me for three minutes for a test run), all 165 scored. The gate is met clearly: ten points on first lines with an interval from 5.3 up, six times as many questions better as worse, and clear of zero when repositories are resampled. vscode, material-ui and vuejs/core give 40 questions each. On the repositories at or under 50,000 chunks (70 questions; vscode, code-server, material-ui and angular left out), which is the reading the owner's "mid size" asks for: +12.4 points (+3.6 to +21.9), 15 better and 3 worse: the same answer from fewer questions. Words and names alone find less here than in any other language so far (0.355 on first lines, 0.203 on whole text), which is where vectors have room.

**JavaScript.** Embedded in 35 minutes (the chip at 77 °C at most, no waits), all 230 scored. On first lines the gain is 3.7 points with an interval that holds zero, and 22 questions get worse for 39 that get better: not shown, by either half of the gate. On whole issue text it is 12.4 points, clear of zero. All twelve repositories are under 50,000 chunks, so the reading beside is the same reading. JavaScript and TypeScript come out on different sides of the gate, which is why they are two verdicts: the TypeScript questions come from large applications where words alone find little (0.355), the JavaScript ones from libraries where they find more (0.445).

**Java.** Embedded in two hours and eight minutes (the chip at 76 °C at most, no waits), all 296 scored. On first lines vectors change nothing: 0.469 against 0.470, as many questions worse as better, and with the largest set of the four the interval is the narrowest (−3.2 to +3.4), so this is the one "not shown" that is close to "shown not to". On whole issue text the gain is 5.3 points, the smallest of any language. On the repositories at or under 50,000 chunks (242 questions; druid, lucene and trino left out): −1.0 points (−4.8 to +2.8). Java's names are long and say what they are; words and names already find what a first line asks for.

**The four together, and with Python and C++.** The chain ran from 13:12 to 19:04, under six hours for an estimate of ten, with no waits for heat. Verdicts recorded in `dense_policy.VERDICTS`: on for TypeScript; off for Go, JavaScript and Java, each with its figures and the way to switch vectors on by hand or for one repository (`aleph setup`). Across six languages the first-line gain runs from +0.1 (Java) to +10.2 (TypeScript), and it is large where words and names alone find little: C++ 0.389 and TypeScript 0.355 pass; Python 0.563, Go 0.500, Java 0.469 and JavaScript 0.445 do not. On whole issue text every language gains, from +5.3 (Java) to +18.2 (Go), with an interval clear of zero each time. That is the same thing the A/B run's look afterwards pointed at (ADR-011): what vectors are good for is a question asked in many words. The gate reads first lines because that is nearer what an agent types, and of that there is still almost no record (two queries in sixty sessions); the verdicts stand on the gate as written, and the whole-text column is the reason a later rule for whole-text questions (rung 2's own) would be worth writing.

## Which questions a verdict is read from: the repository's main language, not the set's label (2026-10-13, late)

**The table above was read on a population the product does not use, and for JavaScript that changed the verdict.** A verdict is applied by a repository's main language (`Index.languages()`: the language most of its indexed source is written in). The four languages above were read by the question sets' own labels. Looked at because the plugin's TypeScript tasks turned out to be in JavaScript repositories for a third: of the 165 questions labelled TypeScript, 62 are in repositories the engine calls JavaScript at the question's commit (material-ui 40, tailwindcss 17, babel 4, preact 1) and one in a Go repository; 8 labelled JavaScript are in repositories it calls TypeScript. tailwindcss is JavaScript to the last file and every answer of its questions is a `.js` file: the label is the set's directory, not a fact about the code. Go's, Java's, C++'s and Python's questions are all in repositories of that language already, and no question is in two files.

**The rule from here on, for every language:** a verdict is read from the questions whose repository's main language, by the engine's own rule at the question's commit, is that language, whatever set or label they came under. `bench/eval/population.py` writes that language into a result; `bench/eval/verdict.py --main-language` reads a language from every file given. The gate is unchanged.

| Main language | Questions, repositories | Words and names | With vectors | Gain (95%) | By repository | Better / worse | Whole text | By label it read |
|---|---|---|---|---|---|---|---|---|
| **TypeScript** | 110, 9 | 0.305 | 0.377 | **+7.3 (+1.8 to +12.9)** | +1.3 to +9.5 | 18 / 4 | +8.6 (+4.0 to +14.0) | +10.2 on 165: on, and stays on |
| **JavaScript**, as asked until then | 284, 12 | 0.447 | 0.508 | +6.1 (+1.7 to +10.7) | +0.4 to +12.3 | 53 / 23 | +12.3 (+8.2 to +16.7) | +3.7 on 230, holding zero: off |
| **Go** | 111, 8 | 0.499 | 0.519 | +2.0 (−3.9 to +7.8) | −8.0 to +8.2 | 17 / 13 | +18.4 (+11.3 to +25.7) | +2.0 on 110: off, and stays off |

JavaScript passed the gate read this way, and was not switched on by that: the grouping has no parameter and is the right one, but it was looked for after the figures were seen, and with one repository left out at a time the reading fell under the bar twice in twelve (without dayjs +4.5, −0.4 to +9.6; without tailwindcss +4.8). TypeScript falls under it once in nine (without vscode, 70 questions: +5.8, −1.7 to +13.3).

**The run that decided, by a rule written before it** (the deferred list, entry 1b-2, committed before anything was fetched): a second draw by the same seed of at most 40 questions from each JavaScript repository that had questions left over the limit and never asked. Switched on only if the new questions alone pass the gate and the interval by repository of all JavaScript questions together excludes zero.

| JavaScript | Questions, repositories | Words and names | With vectors | Gain (95%) | By repository | Better / worse | Whole text |
|---|---|---|---|---|---|---|---|
| **The second draw alone** | 164, 5 | 0.350 | 0.512 | **+16.2 (+9.4 to +23.1)** | +13.1 to +18.7 | 48 / 11 | +15.5 (+9.7 to +21.5) |
| All, first and second draw | 448, 12 | 0.412 | 0.509 | +9.8 (+6.0 to +13.6) | +6.2 to +13.3 | 101 / 34 | +13.5 (+10.1 to +16.9) |

| Repository | First draw: gain, better / worse | Second draw: gain, better / worse |
|---|---|---|
| material-ui | +9.5 on 40, 8 / 1 | +13.4 on 40, 10 / 4 |
| serverless | +5.8 on 40, 5 / 2 | +20.2 on 40, 14 / 2 |
| prettier | −2.5 on 40, 6 / 5 | +18.3 on 40, 12 / 4 |
| svelte | −5.3 on 35, 3 / 5 | +14.0 on 34, 9 / 0 |
| dayjs | +15.6 on 40, 9 / 1 | +10.0 on 10, 3 / 1 |

Both halves of the rule are met, the first by a wide margin and in each of the five repositories: **`dense_policy.VERDICTS["javascript"]` is on.** Embedding and scoring took 17 minutes (22:24 to 22:41), the chip at 77 °C at most, no waits; 170 questions scored, six of svelte's left out of JavaScript because the repository was mainly TypeScript at their commits (with them TypeScript reads +6.4, +1.3 to +11.9, on 116).

**What this does not settle, and three voices.**

- In the reading of all 448, four repositories have 80 questions each: the limit of 40 a repository, which keeps one project from deciding a language, holds for each draw and not for the two together. The guard for that reading is the interval by repository, which was in the rule; the second draw alone keeps the limit.
- The second draw asks more of five repositories that were asked before. It answers "was the grouping an accident of those questions" (no) and not "does this hold for a JavaScript repository none of the sets holds". Every verdict here has that limit (C++'s rests on five repositories), and JavaScript's twelve are the second most of any language.
- Words and names find less in the second draw (0.350 against 0.447), so there is more for vectors to add; why the leftover questions are harder for words is not looked into.
- *The advisor:* the population by main language for all languages; a held-out draw before the shipped value moves, as Python had. *agy (Gemini 4 Argon, no boost), asked before the run:* keep JavaScript off; more questions of the same repositories measure those repositories again; repositories never asked are the check that counts; the interval by repository should decide. Two figures it gave did not hold when computed (leaving material-ui out does not bring the first reading under 5: +5.5; and the gain is not on TypeScript files inside JavaScript repositories: answers in `.js` files +6.1 on 224, in `.ts` files −6.2 on 27). Its objection about unseen repositories stands after the run. *Mine:* the advisor's run with agy's interval by repository in the rule; both are met.

The verdict can be set aside for a repository as before (`aleph setup --no-dense`, `--no-dense`, `ALEPH_DENSE=0`), and takes effect only where the embedding worker is installed.

## The starting values, each tried against others (2026-10-13): none is replaced

The column weights of the text index, the number of candidates the path and name generators propose, the weight of the name generator's vote and the two numbers that govern long queries were set when the ranking was first written and called starting values since. All of them act when a question is asked, so they can be tried on the prepared indexes without indexing or embedding anything (`bench/eval/retune.py`). The design was fixed first, with the advisor and agy (the deferred list, entry 1c): each value alone against what ships, on the 235 Python questions that everything was first looked at on, with words and names; file recall@10 of ten results, the mean of the first-line and whole-text forms (the long-query numbers on whole text only); at most two changes of 1.5 points or more go forward and are read once on the 165 questions they were not chosen on and on C++. No chained search: on 235 questions a step of half a point is two questions.

| Value | What ships | Tried, and the change in points |
|---|---|---|
| Column weights (symbol, signature, doc, body) | 5, 3, 2, 1 | 8, 4, 2, 1: +0.9 · 5, 3, 1, 1: +0.6 · 3, 2, 1.5, 1: +0.4 · 1, 1, 1, 1: +0.4 · 2, 1, 1, 1: −0.4 |
| Long queries: cut from how many words, to how many (whole text) | 16, 24 | 16 or 24, to 48: +1.1 · 16 or 24, to 32: −0.9 · 12, to 16: −0.4 · 12, to 24: 0.0 |
| Places the path generator proposes | 5 | 3: −1.1 · 8: −0.4 |
| Places the name generator proposes for one name | 10 | 5: +0.9 · 20: −0.2 |
| Weight of the name generator's vote | 1 | 1.5: 0.0 · 2: 0.0 (the order inside the ten changes in about one question of eight; the files among them do not) |

**Nothing reaches 1.5 points, so nothing goes forward, and the 165 questions were not read for this.** The whole table lies within 1.1 points of what ships, which is two or three questions of 235 either way: the ranking is flat in these values over the ranges tried, including with every column weighted alike. They are no longer called starting values. What this does not cover: combinations (by design), the fusion's constant (left at 60), a weight for the path generator (its vote is written without one), and other languages than Python.

## Fusing by definition instead of by piece (2026-10-13): measured, not adopted

The fusion adds up the generators' votes per chunk, and the pieces of one long definition are merged into one result only afterwards (found by agy while reviewing the chunk-size sweep, and so in the code). When the words rank one piece of a function and the vectors or the name lookup another, neither piece has two votes; and of the 300 candidates the words propose for a first line, about 85 are second or later pieces of a definition already among them.

`bench/eval/fusion.py` replaces the fusion for a run, on the prepared indexes, without changing the engine. Two arms beside the fusion as it is: **merged** (the pieces of a definition are one candidate in each generator, at the rank of its best piece, and the generators' votes for it are added) and **closed up** (the same, and each list is numbered by definitions). A definition is exactly what the engine merges after fusing. The rule was written first (the deferred list, entry 1c): read on first lines in the arm each language ships with; adopted only if not lower in either with an interval clear of zero, higher in at least one with an interval clear of zero, and first place not lower. The fusion as it is reproduces every recorded figure to the digit, which is the check on the harness.

| First lines, file recall@10 | As it is | Merged | Closed up |
|---|---|---|---|
| **Python, words and names** (ships), 400 questions | 0.563 | 0.566: +0.3 points (0.0 to +0.7) | 0.566: +0.3 (−0.5 to +1.3) |
| **C++, with vectors** (ships), 108 questions | 0.456 | 0.461: +0.5 (0.0 to +1.4) | 0.460: +0.3 (−0.3 to +1.4) |
| Python, with vectors | 0.603 | 0.618: +1.6 (+0.5 to +2.8) | 0.613: +1.1 (−0.5 to +2.7) |
| C++, words and names | 0.389 | 0.389: no question changes | 0.389 |

**By the rule it is not adopted, and the fusion stays.** In each shipped arm merging changes the files among the ten for exactly one question, for the better; an interval whose lower end is zero is not clear of zero (3,714 and 3,772 of the 10,000 resampled means are zero). Closing up fails more plainly. The rule did not foresee a bound landing on zero; the count of resamples decided it, and it decides against.

**Why so little, and where it is not little.** With words and names there is in effect one list: merging the pieces inside one list without renumbering changes nothing, and the name lookup adds a vote to few candidates. The change acts where two generators both rank pieces, which is where vectors take part: Python with vectors gains 1.6 points on first lines (+0.5 to +2.8) and 2.3 on whole issue text (+0.8 to +4.0), with first place up by 2.9 and 3.6; Python with words gains on whole text too (+1.0, +0.3 to +2.0; first place +2.9, +1.3 to +4.8). On C++ with vectors, whole text: +0.5 in the ten (0.0 to +1.3), mean reciprocal rank +2.0 (+0.3 to +4.0). No cell anywhere is lower with an interval clear of zero. So this is a change that would matter for a Python repository with vectors switched on and for questions asked as whole texts (rung 2 asks so), and that does next to nothing for what ships; those were not what the rule read. If either comes to ship, it is read again there, by a rule written then.

**What it would do to the vector verdicts, for the record.** Under merged fusion Python's gate reads +5.2 points on all 400 first lines (+2.0 to +8.7), over the bar. That is not a pass. The verdict was decided by the 165 questions held out, and there merging moves no first-line score at all in either arm: +1.6 (−3.4 to +6.7), as before. The whole movement is on the 235 questions everything was first looked at on, which is what a held-out set is for. C++ would read +7.2 (+1.9 to +13.0) and stay on.

### Read again the same evening, over all six shipped arms: still not adopted

At 19:04 TypeScript came to ship with vectors, which is what the morning's reading had named as the reason to read again. The rule was written first (the deferred list, entry 1c) and reads `merged` on first lines in the arm each of the six languages ships with; the morning's cells for Python and C++ are used as they stand.

| Shipped arm, first lines | As it is | Merged: change (95% interval) | Questions better / worse | First place |
|---|---|---|---|---|
| Python, words and names, 400 | 0.563 | +0.25 points (0.0 to +0.75) | 1 / 0 | +0.4 (0.0 to +1.0) |
| Go, words and names, 110 | 0.500 | none | 0 / 0 | none |
| JavaScript, words and names, 230 | 0.445 | none | 0 / 0 | none |
| Java, words and names, 296 | 0.469 | none | 0 / 0 | +1.0 (0.0 to +2.4) |
| C++, with vectors, 108 | 0.456 | +0.46 (0.0 to +1.4) | 1 / 0 | −0.3 (−0.9 to 0.0) |
| TypeScript, with vectors, 165 | 0.456 | +0.26 (−1.2 to +2.0) | 3 / 3 | +1.5 (−0.3 to +3.6) |

Not lower anywhere; higher nowhere with an interval clear of zero. **Not adopted; the fusion stays as it is.** TypeScript was where it was expected to show, and it changes six questions, three each way. Where it does gain with an interval clear of zero is with vectors on first lines in the languages whose default is words: Python +1.6, JavaScript +1.5, Java +1.5 (Go +1.4, its interval holds zero), and on whole text Python +2.3. So merging the votes helps a fusion that has vectors in exactly the places where vectors do not ship, and does nearly nothing where they do. That has been read twice now by rules written first; it is not read a third time unless what ships changes again.

## Two changes to the name and path generators (2026-10-14): read twice, not adopted; a gain on whole issue text is recorded

Found by agy (Gemini 4 Argon, with boost) reading the generators, and true in the code: **a name written with what it is called on finds nothing** (`self.rotate`, `pkg.Func`, `df.to_csv`: the whole dotted text is looked up, and a definition's qualified name does not hold a variable or a module), and **a file's path votes for the file's first lines, not for what was found in the file** (the file then comes twice, and the function found has one generator's word for it). Both were built behind switches that were off (`DOTTED_FALLBACK`: the name looked up without what stands before the dot when the whole finds nothing; `PATH_VOTE = "found"`: the path's vote goes to the best chunk found in the file), and the rule was written before each reading (the deferred list, entry 1d). `bench/eval/trial.py` reads a switch on the kept indexes, each language in the arm it ships with.

**First reading: every kept question (1,479; Python 400, JavaScript 448, Java 296, TypeScript 116, Go 111, C++ 108), first line.** The rule: adopted if the files among ten gain with an interval clear of zero over all questions, no language lower, first place not lower.

| | Files among ten | First place | Whole issue text, files among ten |
|---|---|---|---|
| Dotted names | +0.44 (+0.00 to +0.88), 10 better / 3 worse | +0.94 (+0.41 to +1.51) | +1.10 (+0.24 to +1.96) |
| Path vote | +0.09 (−0.28 to +0.49), 12 / 12 | +1.40 (+0.60 to +2.20) | +0.90 (+0.38 to +1.44) |
| Both | +0.53 (−0.04 to +1.13), 22 / 15 | +2.25 (+1.35 to +3.17), 52 / 16 | +2.07 (+1.15 to +3.01), 66 / 23 |

Not adopted by that rule: the files among ten barely move. First place did, most of all in Python (31.5% → 37.0%, +5.5, +3.25 to +8.0, 24 better and 2 worse), and first place was named in the rule only as "not lower". A rule is not changed on the questions that suggested the change, so a second reading was set, with its rule written before anything was fetched.

**Second reading: Loc-Bench's 160 Python questions that no run had asked, in 129 repositories none of which had been fetched** (158 scored, two over 150,000 chunks), words and names. The rule: both are switched on if first place on first lines gains with an interval clear of zero and the files among ten are not lower; otherwise both stay off and their code is taken out.

| Both together, 158 questions | First line | Whole issue text |
|---|---|---|
| Files among ten | 52.9 → 53.2, +0.32 (+0.00 to +0.95), 1 / 0 | 54.4 → 57.2, **+2.76 (+0.76 to +5.27)**, 7 / 0 |
| First place | 28.3 → 29.1, +0.84 (−1.90 to +3.80), 4 / 2 | 23.3 → 29.3, **+6.0 (+2.1 to +10.3)**, 12 / 2 |

**Not adopted: first place on first lines holds zero. The code is taken out** (it is in the history at commit `1a18dd4`). The first reading's +5.5 at first place for Python did not come again on repositories it had not seen; that is what the second reading was for.

**What both readings do show, and what it is not:** on whole issue text the two changes gain files among ten with an interval clear of zero in both (+2.07 on 1,479 questions, +2.76 on 158 unseen ones), and on the unseen ones first place gains six points, nearly all of it from the path vote. A verdict here is read on first lines because an agent's own question is nearer a first line; but the plugin's own question at a session's first prompt is the user's whole text. Whoever takes this up writes a rule that names whole text first, and reads it on questions not used here (339 unasked Go questions in one repository, about a thousand JavaScript and six hundred TypeScript ones over the limit of forty a repository are left).

*Three voices.* agy found both and proposed them as "a free recall win for Python, Go, and Java". The advisor: the rule written at 00:15 names first lines; it fails; the code comes out and the whole-text finding is written down, not acted on. Mine: the same; and the first reading is a lesson in why a second was needed.

## One result a file, the file's other places on its line (2026-10-14): adopted, read three times

Ten results named about six files: a file's second and third places took the places of other files. ADR-011 read "at most one result a file" on Python and C++ under a 5-point bar and kept the layout, since the second and third places would be lost. agy (boost) named it the largest improvement left, with those places kept on the result's line. Now each file's best result stands for it, with up to three further places of the file on its line (`Hit.places`; in the text `also at: 40-46 rotate, 88-120 save`; in the plugin's rung 2 on the file's line as before). The rule, written first: read on every kept question by the rule for free changes, and again on questions never used to choose it; adopted only if both pass.

| Reading | Questions | First line, files among ten | Better / worse | Whole text |
|---|---|---|---|---|
| Kept, each language in its shipped arm | 1,479 | +4.24 (+3.37 to +5.15) | 108 / 0 | +2.49 (+1.80 to +3.23) |
| Python, never asked, 129 repositories never fetched | 158 | +9.38 (+5.63 to +13.53) | 23 / 0 | +6.98 |
| JavaScript and TypeScript, a third draw never asked | 211 | +3.18 (+1.24 to +5.54) | 11 / 0 | +2.61 |

Per language on the kept questions: Python +3.4, TypeScript +6.0, JavaScript +3.5, Go +6.6, Java +3.9, C++ +7.0, each clear of zero. First place is unchanged by construction. **Adopted: `search.ONE_A_FILE` is on.** The vector verdicts read with it in both arms do not change: Python +4.6 (+1.3 to +7.9), TypeScript +8.1, JavaScript +7.0, Go +2.5, Java +2.7, C++ +8.1.

### Switched off again the same morning: what it cost, and what replaced it

The three readings measured files. Read at the level of the functions each fix edited (Loc-Bench names them; Python's 400 kept questions, words and names, through the renderer at the default budget of 1,500 tokens), one result a file had a cost the rule had not measured: **edited functions whose code the agent receives fell from 34.0% to 20.3% (−13.7, −17.1 to −10.3)**, while those shown or named on a line rose to 41.9%. A file's second and third results had carried code; as places they carry a name. Every shape between was a trade too (at most two results a file: files +1.3, code −6.4; three: +0.75 / −2.4; the first file three and the others one: +2.75 / −4.7), with every interval clear of zero. The owner asked for nothing to be sacrificed, so the switch is off again.

**What replaced it: the results as they were, and after them the next files of the same ranking as places only, until ten files are named** (`search(..., more_files=)`, `Answer.more_files`, printed under `more files:` with whatever room the results' text leaves; in the JSON as `more_files`). The files named are the same ten as one result a file names, so its three readings stand for them; and the results are untouched by construction. Through the engine and the renderer on Python's 400 kept questions: files named in the text the agent receives 56.3% → 58.6% (+2.25, +1.00 to +3.75; 10 better, none worse); edited functions whose code it receives 34.0% → 34.0%. Below the +3.4 of the files themselves, because further files are named only where the results' code leaves room: that is what keeps it free. Adopted.

## The two generator changes on whole issue text (2026-10-14): a third reading, not adopted

The rule written before it: the two changes together (dotted names; the path's vote to what was found in the file) adopted for every question if, on questions never asked, files among ten on whole issue text gain with an interval clear of zero, no language is lower and first lines are not lower. The third draw of JavaScript and TypeScript (211 never-asked questions): whole text +2.01 (−0.24 to +4.62); first lines +0.91 (+0.07 to +2.17). **Not adopted.** The path's vote alone gained on whole text with an interval clear of zero here (+1.66, +0.24 to +3.55) and in the first reading (+0.90), and +0.95 (+0.00 to +2.53) in the second; it was never named alone in a rule, and is left for one that does. agy, before this reading, called its design wrong: JavaScript and TypeScript with vectors were where the whole-text gain had been weakest. The switches stay in the code, off.

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
