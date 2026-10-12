# ADR-001: Embedding model, runtime and vector representation

- **Status:** Accepted 2026-10-11, after M0 spikes A and E. The chunk split threshold is set by the cap-quality benchmark recorded at the end.
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

## Split threshold: 256 tokens, provisional, chosen by retrieval quality

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

**Before it is final.** M2's Benchmark C repeats the sweep at 96, 128, 192, 256 and 384 on issue text with gold edit locations (Loc-Bench), adds a control that indexes a signature-only chunk beside the whole function, and records which piece won for each query.

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
