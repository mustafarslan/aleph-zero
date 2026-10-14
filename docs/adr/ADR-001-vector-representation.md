# ADR-001: Embedding model, runtime and vector representation

- **Status:** Accepted 2026-10-11, after M0 spikes A and E. The chunk split threshold is set by the cap-quality benchmark recorded at the end and was confirmed on issue text on 2026-10-13.
- **Date opened:** 2026-10-11

## Question

Which embedding model, inference runtime, weight quantization, output dimension and stored vector type does Aleph Zero use, within the memory budget of ADR-005?

## Decision

| Choice | Decision |
|---|---|
| Model | EmbeddingGemma 2 (`google/embeddinggemma-2`, Apache-2.0), GGUF from `unsloth/embeddinggemma-2-GGUF` |
| Runtime | llama.cpp's `llama-server` as the shared embed worker (ADR-005), built from commit `c83f3058` with `bench/spikes/llama-no-logits.patch` |
| Device | GPU whenever one is present (Metal on macOS), with 1 CPU thread; CPU with 8 threads otherwise |
| Weights | Q8_0 |
| Output dimension | 256 (Matryoshka truncation, renormalized) |
| Stored vector type | float16, in a memory-mapped sidecar file |
| Worker capacity | 4,096 tokens on GPU, 2,048 on CPU |
| Prompts | query `task: code retrieval | query: {q}`; document `title: none | text: {code}` |
| Core language | Python stays: the session baseline is 60.8 MB and attach takes 0.25–1.4 s |

Not used: PyTorch and sentence-transformers (memory), `llama-cpp-python` (cannot load the model, see below), a reranker (four off-the-shelf rerankers all lowered text-to-code quality in the literature review).

## Why this runtime

**There is no working in-process llama.cpp binding today.** `llama-cpp-python` 0.3.36 was released on 2026-10-01, five days before the model: its vendored llama.cpp does not know the `gemma-embedding2` architecture, its macOS arm64 wheels on the project's own index fail zip integrity checks, and its ctypes bindings crash against a newer `libllama`. llama.cpp itself, built from source in 13–33 seconds, loads the model and produces the correct 768-d normalized embedding.

**llama.cpp needs a one-line patch.** `llama_context::output_reserve()` reserves `vocabulary × output tokens` floats for logits even in an embeddings-only context. With a 262,144-word vocabulary that is 1.05 MB per input token, kept at the size of the longest input seen. The patch (`has_logits = !cparams.embeddings`) removes it. An upstream report is drafted in `docs/upstream/llama-cpp-logits-reservation.md`.

| Worker private memory | Unpatched | Patched |
|---|---|---|
| CPU Q4, after 820-token inputs | 1,119 MB | 267 MB |
| Metal Q8, after 820-token inputs (unpatched figure from an ad-hoc probe) | 1,335 MB | 303 MB |

The patch does not change the output: patched llama.cpp agrees with ONNX Runtime running the `onnx-community` export at cosine 0.99977, and CPU agrees with Metal at 0.99993.

**ONNX Runtime is the documented fallback**, not the default. It loads in-process and shares weights, but a query-only session costs about 190 MB with a sentencepiece tokenizer (about 300 MB with the Hugging Face tokenizer, which alone takes 144 MB for this vocabulary) and queries take 20–46 ms.

## Measurements (M0, 2026-10-11, Apple M4 Max)

**Speed** (median per input, final gate run `bench/data/results/mem_share.json`):

| | Query | 246 tokens | Near capacity |
|---|---|---|---|
| Metal, capacity 4,096 | 8.6 ms | 16.5 ms (61/s) | 289 ms at 3,624 tokens |
| CPU, 8 threads, capacity 2,048 | 11.3 ms | 77.5 ms (13/s) | 791 ms at 1,734 tokens |

From ad-hoc probes earlier the same day (recorded in the execution ledger, not part of a gate): an ~820-token input takes about 51 ms on Metal and 313–417 ms on CPU; a ~1,900-token input 114–123 ms on Metal.

On Metal one CPU thread gives the same throughput as eight (59 chunks/s) at 17% of a core instead of 183%, with the GPU at 85–88% utilization. Parallel slots and array batches gave no gain, which is consistent with the GPU being the bottleneck. On CPU, eight threads is the optimum; twelve is no faster.

**Worker memory by capacity** (Metal Q8, inputs near the capacity, from a capacity sweep recorded in the ledger): 512 → 289 MB; 1,024 → 305 MB; 2,048 → 343 MB; 4,096 → 465–478 MB across runs; 8,192 → 947 MB. The CPU worker at 4,096 reached 754 MB, over the 600 MB limit, so its capacity is 2,048 (459–474 MB across runs).

**Quality by weights, dimension and vector type** (CoSQA subset: 300 queries, 3,300 documents; NDCG@10 relative to BF16 weights at 768-d float32, which scored 0.9017):

| Weights | 768 f32 | 512 f32 | 256 f32 | 256 f16 | 256 int8 | 128 f32 |
|---|---|---|---|---|---|---|
| BF16 | 1.000 | 1.005 | 0.992 | 0.992 | 0.993 | 0.912 |
| Q8 | 0.997 | 1.004 | 0.989 | 0.989 | 0.991 | 0.909 |
| Q4 (Metal) | 0.998 | 0.990 | 0.991 | 0.991 | 0.985 | 0.900 |
| Q4 (CPU) | 0.998 | 0.990 | 0.987 | 0.987 | 0.978 | 0.898 |

Every 256-d variant is within 3% of the reference; every 128-d variant fails. float16 storage is indistinguishable from float32. The subset is easy (recall@10 is 0.99 at 768-d everywhere), so it bounds the loss from truncation and quantization and says little about absolute quality. Differences between 256-d and 512-d here are within noise; the choice between them is revisited in M2 on localization tasks.

Q8 and Q4 cannot be told apart on quality here: at the operating point (256-d float16) Q4 on Metal scores 0.991 and Q8 0.989, and at 768-d 0.998 and 0.997, all within noise. They are also the same speed and the same memory on Metal. Q8 is chosen as the more conservative quantization at no cost on the GPU (the two produce slightly different vectors, cosine 0.992 for the same input), and on CPU it is the faster of the two on chunks (77 ms against 97 ms), at 54 MB more idle memory. Either would pass.

**Chunk reuse across commits** (50 consecutive commits; share of chunks in changed files that needed no new embedding): Flask 92.1% with AST chunks against 54.2% with 50-line windows; FastAPI 80.7% against 33.2%.

**Install:** `uv sync` from an empty environment took 32.6 s, including compiling `llama-cpp-python` from source (which turned out to be unusable and has since been removed from the dependency list). Building the patched llama.cpp worker takes 13 s (Metal) to 33 s (CPU) and needs git, CMake and Ninja; prebuilt binaries are required before public release.

## The width read on localisation at last (2026-10-14): 256 stays

The table above could not tell 256 from 512 (its set is too easy), and the choice was to be "revisited in M2 on localization tasks". It was not, until the owner asked for every capability to be checked against what could improve it; the model's card gives 512 and 768 as better on code retrieval (77.2 and 78.7 against 76.2 on MTEB Code). `bench/eval/widths.py` made vectors of a kind of their own beside the engine's, with all 768 numbers, for every language's kept questions in repositories at or under 20,000 chunks, and scored them as if 256, 512 or 768 had been kept. The rule was written first (the deferred list, entry 1d): on the languages that ship with vectors, all their questions together, first line, file recall@10, **a wider vector replaces 256 only with +2 points or more and an interval clear of zero** (512 doubles the vector file and adds a third to the scan, 768 triples them). The script's check: at 256 it gave the engine's own kept C++ results again (recall 0.4561 and 0.4561 on first lines, 0.5152 and 0.5152 on whole text; no question's recall moved).

| Against 256 | Questions, repositories | First line, files among ten | First place | Whole text, files among ten |
|---|---|---|---|---|
| **512**, languages that ship with vectors (JavaScript, TypeScript, C++) | 549, 22 | +0.70 (−0.95 to +2.29), 22 better / 15 worse | +0.17 (−1.11 to +1.45) | −0.05 (−1.67 to +1.58) |
| **768**, the same | 549, 22 | +0.33 (−1.50 to +2.06), 24 / 21 | −1.27 (−2.77 to +0.21) | **−1.74 (−3.46 to −0.09)** |
| 512, all six languages | 973, 60 | +0.47 (−0.62 to +1.60) | +0.09 | +0.06 |
| 768, all six languages | 973, 60 | +0.13 (−1.02 to +1.28) | −1.01 (−2.06 to +0.06) | −0.97 (−2.17 to +0.21) |

Per language, 512 against 256 on first lines, deciding nothing by itself (72 to 369 questions): TypeScript +2.5 (−1.3 to +7.1), Go +2.0 (−0.9 to +5.4), JavaScript +1.4 (−0.5 to +3.4), Python 0.0 (−2.2 to +2.2), Java −1.0 (−3.4 to +1.5), C++ −3.0 (−7.1 to +0.5). No interval is clear of zero in either direction.

**Not adopted: 256 stays.** Neither width gains two points, neither interval is clear of zero, and the full 768 is lower on whole issue text with an interval clear of zero. What the model's card shows on its benchmark does not show here, with the vectors fused with words and names and the question an issue's first line. The vectors were made in 2 hours 38 minutes (01:30 to 04:09 with the readings), the chip at 79 °C at most, no waits.

## The document's title read on localisation (2026-10-14): `title: none` stays

The model's card gives `title: {title} | text: {content}` as a document's form, and the engine sends `title: none` with the breadcrumb as the text's first line. The file's path as title was not read: a vector would belong to a path and a moved file would have to be embedded again (agy). The chunk's own breadcrumb as title keeps vectors content (agy's proposal), so it was read, by the same script and on the same 973 questions, with the rule written first: **adopted with a gain clear of zero at the width kept, and no language lower.**

| Breadcrumb as title against `none` | First line, files among ten | First place | Whole text, files among ten |
|---|---|---|---|
| At 256, languages that ship with vectors (549 questions) | +1.40 (−0.41 to +3.19), 30 better / 22 worse | −0.31 (−1.71 to +1.13) | **−1.85 (−3.28 to −0.47)** |
| At 256, all six languages (973) | +0.80 (−0.46 to +2.05) | +0.23 | −0.80 (−1.77 to +0.12) |
| At 512, languages that ship with vectors | +0.90 (−0.66 to +2.52) | +0.18 | **−1.84 (−3.12 to −0.64)** |
| At 768, languages that ship with vectors | +0.72 (−0.74 to +2.26) | +1.63 (+0.29 to +3.05) | +0.05 |

**Not adopted.** At the width kept the gain on first lines holds zero, and on whole issue text it is lower with an interval clear of zero (JavaScript −2.0, −4.0 to −0.3). TypeScript alone gains on first lines (+5.3, +0.8 to +10.8, on 72 questions, 5 better and 1 worse), which by the rule decides nothing. Made in 3 hours 8 minutes (04:09 to 07:17), the chip at 78 °C at most, no waits.

## Split threshold: 256 tokens (provisional until 2026-10-13, final since: see the sweep on issue text below)

`bench/spikes/cap_quality.py`, run on 2026-10-11. Corpus: every Python function in Flask, FastAPI and Django (35,851), with its docstring removed. Queries: the first sentence of the docstring for 1,500 functions: all 292 over 512 tokens that have one, plus a random sample of shorter ones (134 between 256 and 512 tokens, 1,074 at or under 256). A function over the threshold is split at statement boundaries into pieces under it, each carrying the file path, scope and signature as a header. A function is found when any piece is retrieved. 256-d vectors, Q8 weights on Metal.

Recall@10 by function length:

| Threshold | All | ≤ 256 tokens | 256–512 | 512–1,024 | 1,024–2,048 | Chunks |
|---|---|---|---|---|---|---|
| 96 | 0.719 | 0.697 | 0.776 | 0.785 | 0.739 | 81,557 |
| 128 | 0.709 | 0.691 | 0.724 | 0.789 | 0.696 | 63,097 |
| 192 | 0.707 | 0.692 | 0.716 | 0.776 | 0.674 | 47,766 |
| **256** | **0.698** | 0.687 | 0.679 | 0.762 | 0.696 | **42,320** |
| 384 | 0.700 | 0.695 | 0.664 | 0.753 | 0.674 | 38,676 |
| 512 | 0.695 | 0.696 | 0.649 | 0.722 | 0.674 | 37,417 |
| 768 | 0.688 | 0.697 | 0.649 | 0.668 | 0.674 | 36,496 |
| 1,024 | 0.689 | 0.697 | 0.649 | 0.664 | 0.696 | 36,182 |
| 4,096 (whole) | 0.689 | 0.698 | 0.649 | 0.664 | 0.696 | 35,897 |
| truncate at 512 | 0.655 | 0.698 | 0.649 | (recall@1 0.18 against 0.34 whole) | | 35,851 |

Gain over keeping functions whole, paired on the same queries, with 95% intervals from 2,000 bootstrap resamples:

| Threshold | Recall@10, all | Recall@10, functions over 256 tokens | MRR@10, functions over 256 tokens | Chunks relative to whole |
|---|---|---|---|---|
| 96 | +0.029 [+0.014, +0.045] | +0.106 [+0.070, +0.141] | +0.079 [+0.050, +0.110] | 2.27× |
| 128 | +0.019 [+0.005, +0.034] | +0.087 [+0.056, +0.120] | +0.071 [+0.045, +0.099] | 1.76× |
| 192 | +0.017 [+0.005, +0.029] | +0.078 [+0.047, +0.108] | +0.063 [+0.038, +0.089] | 1.33× |
| 256 | +0.009 [−0.001, +0.019] | +0.059 [+0.031, +0.087] | +0.062 [+0.036, +0.088] | 1.18× |
| 384 | +0.011 [+0.002, +0.019] | +0.047 [+0.024, +0.073] | +0.039 [+0.019, +0.061] | 1.08× |
| 512 | +0.006 [−0.001, +0.013] | +0.028 [+0.007, +0.049] | +0.029 [+0.012, +0.048] | 1.04× |
| 768 | −0.001 | 0.000 | +0.016 | 1.02× |
| 1,024 and above | 0 | 0 | ≤ +0.011 | ~1.0× |

**Reading.**

- Quality rises as the threshold falls across the whole range tested. There is no interior optimum, and a larger cap never helps: from 768 tokens up the benefit is zero.
- Truncating instead of splitting is the worst option by a wide margin.
- Short functions are nearly unaffected: at most 1.1 points lower (at 256), which fits more pieces of long functions competing for the same top ten.
- The choice is therefore a trade against index size. Going from whole to 256 buys about 6 points on long functions for 18% more chunks. Going from 256 to 96 buys about 5 more for roughly double the index.

**Decision (provisional).** Split at 256 tokens. Each piece carries path, scope and signature as a header. Ranking is by function (the best piece wins) and results are returned at function level, with pieces of one function merged. Content is split, not cut off: a single statement larger than the threshold becomes its own piece and is embedded whole up to the worker's capacity; only an input beyond that capacity is truncated, by tokens, as a last resort.

**What the benchmark does and does not show.** It shows that, for docstring-style queries, taking the best of several statement-boundary pieces with a repeated header beats one vector for the whole function, and that truncation is clearly worst. The gain is significant for functions over 256 tokens (+5.9 points [+3.1, +8.7]) and not significant over all queries at 256 (+0.9 [−0.1, +1.9]). It does not show that a piece is a better unit than a function in itself. Three explanations are mixed together: more pieces give a long function more chances under best-piece ranking; small pieces are dominated by the signature, which docstrings tend to paraphrase; and pieces genuinely cover the body better. Against the second being the whole story: truncating at 512 keeps the signature and still loses about 15 points on 512–1,024-token functions, so body content matters. Two limits of the measurement itself: thresholds are nominal, because pieces are sized by an average tokens-per-character ratio and the header is not counted when deciding whether a function fits; and at the 4,096 threshold a few pieces may have exceeded the worker's capacity and been truncated.

**Before it was final.** M2's Benchmark C was to repeat the sweep at 96, 128, 192, 256 and 384 on issue text with gold edit locations, add a control that indexes a signature-only chunk beside the whole function, and record which piece won for each query. The sweep was made on 2026-10-13 and is the next section. The control was not: it was to explain a slope, and the sweep found none; it is asked again only if a later measurement shows one.

### The sweep on issue text (2026-10-13): no setting is better than 256, and 256 is final

`bench/eval/split_sweep.py` chunks every question's commit again at each setting, into scratch indexes and scratch vector files of its own; the prepared indexes that the recorded verdicts rest on are not opened. At 256 it gives the benchmark's own figures to the digit on first lines, with words and names (Python 0.563, C++ 0.389) and with vectors (C++ 0.456 in the first ten, 0.257 in first place), so the new path measures what the benchmark measures. The rules were written before each part was run (the deferred list, entry 1c).

*With words and names* (no GPU; all 400 Python questions and the 108 C++ ones): file recall@10 on first lines is 0.556, 0.562, 0.570, 0.563, 0.558 for Python and 0.380, 0.380, 0.398, 0.389, 0.371 for C++ at 96, 128, 192, 256, 384. No difference against 256 has an interval clear of zero. That was expected and decides nothing by itself: with words alone the pieces of a definition are merged back into one result. It was run as a guard on what Python ships with, and nothing was dropped.

*With vectors*, on C++, the language that ships with them (108 questions, five repositories; each setting embedded again, 15 to 20 minutes each, the chip at 66 °C at most):

| Threshold | Chunks against 256 | First line: first ten | against 256 (95% interval) | First place | Whole text: first ten | against 256 |
|---|---|---|---|---|---|---|
| 96 | 1.51× | 0.461 | +0.5 points (−3.7 to +4.4) | 0.264 | 0.478 | −3.7 (−7.5 to −0.6) |
| 128 | 1.30× | 0.471 | +1.5 (−0.4 to +3.6) | 0.244 | 0.510 | −0.5 (−2.4 to +1.2) |
| 192 | 1.10× | 0.459 | +0.3 (−2.5 to +2.6) | 0.253 | 0.513 | −0.2 (−2.0 to +1.3) |
| **256** | 1.00× | 0.456 | | 0.257 | 0.515 | |
| 384 | 0.91× | 0.450 | −0.6 (−4.1 to +2.3) | 0.250 | 0.512 | −0.3 (−2.2 to +1.5) |

**By the rule, 256 stays and is no longer provisional.** A setting was to replace it only with 5 points or more on first lines and an interval clear of zero; a gain clear of zero under 5 points was to be recorded as real and under the bar. Neither occurred: the largest gain is 1.5 points at 128 and its interval holds zero. The only interval clear of zero is a loss: 96 on whole issue text, 3.7 points lower, for half as many chunks again.

**What this does and does not show.** The spike above found quality rising steadily as the threshold fell, on docstring-style questions asked of vectors alone. On issue text, with vectors fused with words and names and results cut to ten, there is no such slope in either direction between 96 and 384. Three things are different here and are not told apart: the questions (an issue names behaviour, not one function's purpose), the fusion (the generators vote for pieces, not for definitions, so more pieces split the votes: the deferred list, entry 1c), and the measure (files, not functions). 108 questions see about 4 points to either side, so a difference of a point or two between settings would not have shown; what is excluded is that a smaller threshold is worth 30% to 50% more chunks, or a larger one a loss. Python was not run with vectors: over a day of GPU for a language whose vectors are off by default. The three voices on whether to run this at all are in the deferred list: agy was against (too little power to clear the bar), the advisor and I for (the measurement this section promised).

**History.** The first value, 512, was set as a defence against memory growth that the llama.cpp patch then removed. A review note asked whether it should be raised. It should not: the measurement shows a smaller threshold is better, for a reason unrelated to memory.

## What is embedded, as built (M2, 2026-10-12): no path

The header benchmark above put the path, the scope and the signature in front of each piece. In the index a chunk is content: it is stored once and shared by every file, branch and worktree that contains it, and paths are not stored at all. A vector that depended on the path would have to be made per file, which undoes the sharing and multiplies the worker's work.

So a chunk is embedded as its scope (the breadcrumb, such as `TokenStore.get` or `Guide > Install`), its signature and its text, with the document prefix. This is a deviation from what M0 measured. Its cost is one of the things Benchmark C reports; the path still takes part in ranking through the path generator.

Vectors are stored with their kind (`embeddinggemma-2-q8/p1/256`: model, prefix version, width), one file per kind, so a change of model or prefix never mixes with older vectors.

## Which chunks get a vector: code and documentation, not data files (M2, 2026-10-12)

A file with no language profile (JSON, YAML, SQL fixtures, templates, configuration) is chunked by line window, kind `block`. Those chunks get no vector and are searched by their words only.

- **Cost.** They were 58% of all text to embed in moto and 72% in sqlfluff, and many are long enough to fill a worker slot: those repositories embedded at 17 to 36 chunks per second against Django's 87.
- **Quality.** On the 35-question Loc-Bench pilot (Django), leaving them out of the vector candidates raised recall@10 with vectors from 0.771 to 0.800 on the whole issue text and from 0.743 to 0.771 on the issue title, and vectors alone from 0.629 to 0.686. Thirty-five questions cannot prove a gain, and they are part of the set the verdict was later read on, so this is not independent evidence. The decision rests on the cost.

Coverage figures count only chunks that should have a vector.

## Rows are used again (M3, 2026-10-12)

Until M3 a row of the vector file was never reused, and the file grew with every chunk ever embedded. A row whose chunk is collected now goes on a free list and is given to the next chunk; the file stops growing and does not get shorter (since 2026-10-12 the disk under free rows is given back, ADR-010). A chunk's hash no longer includes the chunker's version, so a newer chunker keeps the vectors of chunks it leaves unchanged. Both are described in ADR-010.
