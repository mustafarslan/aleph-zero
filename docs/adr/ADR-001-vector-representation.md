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
| Metal Q8, after 820-token inputs | 1,335 MB | 303 MB |

The patch does not change the output: patched llama.cpp agrees with ONNX Runtime running the `onnx-community` export at cosine 0.99977, and CPU agrees with Metal at 0.99993.

**ONNX Runtime is the documented fallback**, not the default. It loads in-process and shares weights, but a query-only session costs about 190 MB with a sentencepiece tokenizer (about 300 MB with the Hugging Face tokenizer, which alone takes 144 MB for this vocabulary) and queries take 20–46 ms.

## Measurements (M0, 2026-10-11, Apple M4 Max)

**Speed** (median per input):

| | Query | ~250 tokens | ~820 tokens | ~1,900 tokens | ~4,100 tokens |
|---|---|---|---|---|---|
| Metal | 9 ms | 17 ms (59/s) | 51 ms | 123 ms | 316 ms |
| CPU, 8 threads | 8–12 ms | 78–97 ms (10–13/s) | 313–381 ms | 882 ms | 2,244 ms |

On Metal one CPU thread gives the same throughput as eight (59 chunks/s) at 17% of a core instead of 183%, with the GPU at 85–88% utilization. Parallel slots and array batches gave no gain, which is consistent with the GPU being the bottleneck. On CPU, eight threads is the optimum; twelve is no faster.

**Worker memory by capacity** (Metal Q8, inputs near the capacity): 512 → 289 MB; 1,024 → 305 MB; 2,048 → 343 MB; 4,096 → 478 MB; 8,192 → 947 MB. The CPU worker at 4,096 reaches 754 MB, over the 600 MB limit, so its capacity is 2,048 (474 MB).

**Quality by weights, dimension and vector type** (CoSQA subset: 300 queries, 3,300 documents; NDCG@10 relative to BF16 weights at 768-d float32, which scored 0.9017):

| Weights | 768 f32 | 512 f32 | 256 f32 | 256 f16 | 256 int8 | 128 f32 |
|---|---|---|---|---|---|---|
| BF16 | 1.000 | 1.005 | 0.992 | 0.992 | 0.993 | 0.912 |
| Q8 | 0.997 | 1.004 | 0.989 | 0.989 | 0.991 | 0.909 |
| Q4 (Metal) | 0.998 | 0.990 | 0.991 | 0.991 | 0.985 | 0.900 |
| Q4 (CPU) | 0.998 | 0.990 | 0.987 | 0.987 | 0.978 | 0.898 |

Every 256-d variant is within 3% of the reference; every 128-d variant fails. float16 storage is indistinguishable from float32. The subset is easy (recall@10 is 0.99 at 768-d everywhere), so it bounds the loss from truncation and quantization and says little about absolute quality. Differences between 256-d and 512-d here are within noise; the choice between them is revisited in M2 on localization tasks.

Q8 and Q4 are the same speed and the same memory on Metal, so Q8 is used for its slightly higher fidelity. On CPU Q8 is also the faster of the two on chunks.

**Chunk reuse across commits** (50 consecutive commits; share of chunks in changed files that needed no new embedding): Flask 92.1% with AST chunks against 54.2% with 50-line windows; FastAPI 80.7% against 33.2%.

**Install:** `uv sync` from an empty environment took 32.6 s, including compiling `llama-cpp-python` from source (which is no longer needed and is removed from the dependency list in M2). Building the patched llama.cpp worker takes 13 s (Metal) to 33 s (CPU) and needs git, CMake and Ninja; prebuilt binaries are required before public release.

## Split threshold: 256 tokens, chosen by retrieval quality

`bench/spikes/cap_quality.py`, run on 2026-10-11. Corpus: every Python function in Flask, FastAPI and Django (35,851), with its docstring removed. Queries: the first sentence of the docstring for 1,500 functions (all 426 over 256 tokens that have one, plus a random sample of shorter ones). A function over the threshold is split at statement boundaries into pieces under it, each carrying the file path, scope and signature as a header. A function is found when any piece is retrieved. 256-d vectors, Q8 weights on Metal.

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
| truncate at 512 | 0.655 | 0.698 | 0.649 | (recall@1 0.18 against 0.36) | | 35,851 |

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
- Short functions are unaffected, within one point at every threshold.
- The choice is therefore a trade against index size. Going from whole to 256 buys about 6 points on long functions for 18% more chunks. Going from 256 to 96 buys about 5 more for roughly double the index.

**Decision.** Split at 256 tokens. Each piece carries path, scope and signature as a header. Ranking is by function (the best piece wins) and results are returned at function level, with pieces of one function merged. Nothing is truncated: a single statement larger than the threshold becomes its own piece and is embedded whole, which the worker's 4,096-token capacity allows.

**Caveat.** Docstring sentences often paraphrase the function's name, and every piece repeats the signature, so this benchmark favours small pieces. M2's Benchmark C repeats the sweep at 96, 128, 192, 256 and 384 on issue text with gold edit locations (Loc-Bench), and adds a control that indexes a signature-only chunk beside the whole function, before 256 is treated as final.

**History.** The first value, 512, was set as a defence against memory growth that the llama.cpp patch then removed. A review note asked whether it should be raised. It should not: the measurement shows a smaller threshold is better, for a reason unrelated to memory.
