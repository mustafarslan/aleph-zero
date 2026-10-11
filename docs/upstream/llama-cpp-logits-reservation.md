# Draft upstream report: llama.cpp reserves a logits buffer for embeddings-only contexts

> Draft for the owner to review and file at `github.com/ggml-org/llama.cpp`. Nothing has been posted. Numbers below were measured on 2026-10-11 and can be reproduced with `bench/spikes/build_llama.sh` and `bench/spikes/mem_share.py`.

## Suggested title

Embeddings-only context reserves `n_vocab × n_outputs` logits: ~1 MB per token with a 262k vocabulary

## Summary

`llama_context::output_reserve()` sets `has_logits = true` unconditionally, so a context created with `embeddings = true` still reserves `n_vocab * n_outputs_max` floats for logits it never produces. For embedding inputs every token is marked as an output, so the reservation scales with the input length.

With a small vocabulary this is hard to notice. EmbeddingGemma 2 (`general.architecture = gemma-embedding2`) has `n_vocab = 262144`, which makes it 262,144 × 4 bytes = **1.05 MB per input token**. The memory is touched, so it is real private memory, and it is never returned: the process keeps the size of the longest input it has seen.

## Environment

- llama.cpp `c83f3058b6f876f2f7e6ccbada858b2446860c85` (2026-10-11), built with CMake and Ninja, `-DBUILD_SHARED_LIBS=ON`, both `-DGGML_METAL=OFF` and `-DGGML_METAL=ON`
- macOS on Apple M4 Max (12 performance cores, 64 GB), Apple Clang 21
- Model: `unsloth/embeddinggemma-2-GGUF`, `embeddinggemma-2-UD-Q4_K_XL.gguf` and `embeddinggemma-2-Q8_0.gguf`

## Reproduction

```sh
llama-server -m embeddinggemma-2-UD-Q4_K_XL.gguf --embedding -ngl 0 \
  --host 127.0.0.1 --port 8080 -c 1024 -b 1024 -ub 1024 -t 8 --parallel 1
```

Then send embedding requests of increasing length to `/v1/embeddings` and read the process's private memory after each (`footprint <pid>` on macOS).

## Observed

Private memory (macOS `phys_footprint`) of the server process:

| After | CPU, Q4 | Metal, Q8 |
|---|---|---|
| load | 157 MB | 280 MB |
| 20 short queries | 178 MB | 296 MB |
| inputs of ~117 tokens | 304 MB | 428 MB |
| inputs of ~246 tokens | 458 MB | 573 MB |
| inputs of ~486 tokens | 747 MB | 840 MB |
| inputs of ~820 tokens | 1,119 MB | 1,335 MB |

Growth is linear at about 1.15 MB per token of the longest input and does not shrink afterwards.

It is not the compute buffer: `sched_reserve` reports `CPU compute buffer size = 110.03 MiB` for the whole 1024-token context. `--cache-ram 0`, `--ctx-checkpoints 0` and a smaller `-c` make no difference. Flash attention is already in use (`FLASH_ATTN_EXT` nodes in the graph).

## Cause

`src/llama-context.cpp`, `llama_context::output_reserve()`:

```cpp
bool has_logits     = true;
bool has_embd       = cparams.embeddings;
...
logits.size     = has_logits     ? n_vocab*n_outputs_max     : 0;
```

Also relevant, logged at startup for an embeddings context: `init: embeddings required but some input tokens were not marked as outputs -> overriding`, which makes `n_outputs` equal to the number of input tokens.

## Proposed fix

```diff
-    bool has_logits     = true;
+    bool has_logits     = !cparams.embeddings;
     bool has_embd       = cparams.embeddings;
```

The existing T5 special case directly below still forces both on for encoder-decoder models.

## Result with the fix

| After | CPU, Q4 | Metal, Q8 |
|---|---|---|
| load | 156 MB | 278 MB |
| 20 short queries | 160 MB | 279 MB |
| inputs of ~250 tokens | 194 MB | 284 MB |
| inputs of ~820 tokens | 267 MB | 303 MB |

Embeddings are unchanged. For the same input, cosine similarity between the patched CPU and patched Metal outputs is 0.99993, and between the patched llama.cpp output and ONNX Runtime running the `onnx-community/embeddinggemma-2-ONNX` export (an independent implementation) it is 0.99977.

Latency is unaffected: ~17 ms for a 250-token input and ~51 ms for an 820-token input on Metal, before and after.

## Open questions for maintainers

- Is there any embeddings path that reads logits when `cparams.embeddings` is true (for example reranking with `--pooling rank`, or a context that switches between generation and embeddings via `llama_set_embeddings`)? If so the condition should be narrower, or the reservation made lazily.
- Would it be preferable to reserve logits only for tokens whose `batch.logits[i]` was set by the caller, rather than for the overridden all-outputs set?

## Related, separate observation

`llama-cpp-python` 0.3.36 (2026-10-01) cannot load this model (`gemma-embedding2` is unknown to its vendored llama.cpp), and its `py3-none-macosx_11_0_arm64` wheels on `abetlen.github.io/llama-cpp-python/whl/{cpu,metal}` for 0.3.35 and 0.3.36 fail zip integrity checks (same SHA-256 on repeated download; size equals `Content-Length`). That belongs in a report to that project, not this one.
