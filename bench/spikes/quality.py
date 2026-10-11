"""Spike A: retrieval quality on CoSQA by weight quantization, dimension and vector type."""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

import embed_backends

DATA = Path(__file__).resolve().parents[1] / "data"
DATASET = "CoIR-Retrieval/cosqa"
LOSS_LIMIT = 0.03
REFERENCE = ("gguf-bf16-gpu", "768-f32")


def truncate(rows: np.ndarray, dim: int) -> np.ndarray:
    cut = np.asarray(rows[:, :dim], dtype=np.float32)
    return cut / np.maximum(np.linalg.norm(cut, axis=1, keepdims=True), 1e-12)


def metrics(scores: np.ndarray, relevant: list[int], k: int = 10) -> dict[str, float]:
    recall = mrr = ndcg = 0.0
    for row, target in zip(scores, relevant):
        rank = int((row > row[target]).sum()) + 1
        if rank <= k:
            recall += 1.0
            mrr += 1.0 / rank
            ndcg += 1.0 / np.log2(rank + 1)
    n = len(relevant)
    return {"recall": recall / n, "mrr": mrr / n, "ndcg": ndcg / n}


def load_dataset(n_queries: int, n_distractors: int, seed: int) -> tuple[list[str], list[str], list[int]]:
    def table(name: str):
        return pq.read_table(hf_hub_download(DATASET, name, repo_type="dataset")).to_pylist()

    corpus = {row["_id"]: row["text"] for row in table("corpus/corpus-00000-of-00001.parquet")}
    queries = {row["_id"]: row["text"] for row in table("queries/queries-00000-of-00001.parquet")}
    qrels = [row for row in table("data/test-00000-of-00001.parquet") if int(row["score"]) > 0][:n_queries]
    rng = np.random.default_rng(seed)
    relevant_ids = [row["corpus-id"] for row in qrels]
    pool = sorted(set(corpus) - set(relevant_ids))
    distractors = [pool[i] for i in rng.choice(len(pool), size=min(n_distractors, len(pool)), replace=False)]
    doc_ids = list(dict.fromkeys(relevant_ids + distractors))
    position = {doc_id: i for i, doc_id in enumerate(doc_ids)}
    return (
        [queries[row["query-id"]] for row in qrels],
        [corpus[doc_id] for doc_id in doc_ids],
        [position[doc_id] for doc_id in relevant_ids],
    )


def embed_cached(config: str, kind: str, texts: list[str], prefix: str) -> np.ndarray:
    cache = DATA / "cache" / f"cosqa_{config}_{kind}_{len(texts)}.npy"
    if cache.exists():
        return np.load(cache)
    embedder = embed_backends.load(config)
    try:
        rows = np.concatenate([embedder.embed([prefix + t for t in texts[i:i + 64]]) for i in range(0, len(texts), 64)])
    finally:
        if hasattr(embedder, "close"):
            embedder.close()  # the GGUF backend holds a llama-server process
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache, rows)
    return rows


def variants(queries: np.ndarray, docs: np.ndarray) -> dict[str, np.ndarray]:
    out = {}
    for dim in (768, 512, 256, 128):
        out[f"{dim}-f32"] = truncate(queries, dim) @ truncate(docs, dim).T
    q256, d256 = truncate(queries, 256), truncate(docs, 256)
    out["256-f16"] = q256 @ d256.astype(np.float16).astype(np.float32).T
    out["256-i8"] = q256 @ (np.clip(np.round(d256 * 127), -127, 127).astype(np.int8).astype(np.float32) / 127).T
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", nargs="+", default=["gguf-bf16-gpu", "gguf-q8-gpu", "gguf-q4-gpu", "gguf-q4"])
    parser.add_argument("--queries", type=int, default=300)
    parser.add_argument("--distractors", type=int, default=3000)
    args = parser.parse_args()
    query_texts, doc_texts, relevant = load_dataset(args.queries, args.distractors, seed=0)
    report: dict = {"queries": len(query_texts), "documents": len(doc_texts), "configs": {}}
    for config in args.configs:
        queries = embed_cached(config, "queries", query_texts, embed_backends.QUERY_PREFIX)
        docs = embed_cached(config, "docs", doc_texts, embed_backends.DOC_PREFIX)
        report["configs"][config] = {
            name: {k: round(v, 4) for k, v in metrics(scores, relevant).items()}
            for name, scores in variants(queries, docs).items()
        }
    baseline = report["configs"].get(REFERENCE[0], {}).get(REFERENCE[1], {}).get("ndcg")
    if baseline:
        for config, table in report["configs"].items():
            for name, row in table.items():
                row["ndcg_vs_reference"] = round(row["ndcg"] / baseline, 4)
        report["within_limit"] = sorted(
            f"{config}/{name}"
            for config, table in report["configs"].items()
            for name, row in table.items()
            if row["ndcg_vs_reference"] >= 1 - LOSS_LIMIT
        )
    out = DATA / "results" / "quality.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report.get("within_limit") else 1


if __name__ == "__main__":
    raise SystemExit(main())
