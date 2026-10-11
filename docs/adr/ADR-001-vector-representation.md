# ADR-001: Vector representation and quantization

- **Status:** Proposed. Decided by M0 spikes E (memory) and A (quality).
- **Date opened:** 2026-10-11

## Question

Which embedding model, inference runtime, weight quantization, output dimension and stored vector type does Aleph Zero use, given the machine-wide memory budget in ADR-005 (private memory ≤ 200 MB per session, 1024 MB across up to 4 sessions with the model and vectors shared by memory-mapping)?

## Evidence so far

- EmbeddingGemma 2 (`google/embeddinggemma-2`, released 2026-10-06, Apache-2.0) has a 270M text path, 768-d output and Matryoshka dimensions 128/256/512/768.
- MTEB Code v1 mean NDCG@10: 78.68 at 768-d, 77.24 at 512-d (98.2%), 76.18 at 256-d (96.8%), 71.41 at 128-d (90.8%).
- Text-only exports: ONNX q8 314 MB, q4 175 MB; GGUF Q8_0 310 MB, Q4 176 MB.
- No published RSS under llama.cpp or ONNX Runtime on a desktop, and no retrieval evaluation of any quantized build.
- The model card forbids float16 inference. float16 for stored vectors is unaffected.
- `import torch` alone costs about 160 MB, so PyTorch is excluded.
- Fallback candidate: CodeRankEmbed (137M, MIT, int8 ONNX 139 MB, no documented Matryoshka support).

## Candidates to measure

| Axis | Options |
|---|---|
| Model and runtime | EG2 GGUF Q8 / Q4 on llama.cpp; EG2 ONNX q8 / q4 on ONNX Runtime; CodeRankEmbed int8 |
| Dimension | 768, 512, 256 |
| Stored type | float32, float16, int8 |
| Document prompt | `title: none` vs `title: <basename>` |

## Decision rule

Pick the highest-quality configuration that fits the ADR-005 budget and loses at most 3% recall against 768-d float32 on the same model. The runtime must share weights across processes by memory-mapping; a runtime that keeps private copies is disqualified unless a fallback in ADR-005 is adopted. If none fits: Q4, then a smaller model, then a compiled core.

## Decision

Pending M0.
