"""Benchmark C: does searching by meaning as well as by words find more of the right files?

    uv run --group bench python bench/eval/benchmark_c.py prepare WORKDIR [--repos a/b,c/d] [--max-chunks N]
    uv run --group bench python bench/eval/benchmark_c.py run WORKDIR [--repos ...] --out FILE

Questions are Loc-Bench instances: the text of an issue, and the files the fix edited.
For every instance the repository is read at the issue's base commit straight from
git's object store (no checkout), so `prepare` can index and embed every commit once
and `run` can then compare configurations without touching a working tree.

`prepare` uses the network (it fetches each base commit by id from GitHub) and the
embedding worker (GPU, about a hundred chunks a second). It can be interrupted and
resumed. `run` uses neither.

Configurations compared on the same questions:
  bm25     one FTS5 row per file, default tokenizer, every word OR-ed (fixed in advance)
  lexical  Aleph without vectors
  dense    vectors only
  hybrid   Aleph with vectors (rank fusion)
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import apsw
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

from aleph_zero import embedder, filters, gitio
from aleph_zero.embed import worker
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient, EmbedPool, Unavailable
from aleph_zero.indexer import Index
from aleph_zero.search import search
from aleph_zero.store import index_file, open_db
from aleph_zero.vectors import VectorStore
from aleph_zero.view import View

DATASET = ("czlll/Loc-Bench_V1", "data/test-00000-of-00001.parquet")
CACHE = Path(__file__).resolve().parents[1] / "data" / "locbench"
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
MODES = ("bm25", "lexical", "dense", "hybrid")


def instances(repos: set[str] | None) -> list[dict]:
    path = hf_hub_download(DATASET[0], DATASET[1], repo_type="dataset", local_dir=CACHE)
    rows = []
    for row in pq.read_table(path).to_pylist():
        if repos is not None and row["repo"] not in repos:
            continue
        edited = row["edit_functions"]
        edited = ast.literal_eval(edited) if isinstance(edited, str) else list(edited)
        row["gold"] = sorted({entry.split(":")[0] for entry in edited})
        if row["gold"]:
            rows.append(row)
    return rows


def clone_of(workdir: Path, repo: str) -> Path:
    return workdir / repo.replace("/", "__")


def fetch(workdir: Path, row: dict) -> Path:
    """Make sure the instance's base commit is in a local repository. Network."""
    clone = clone_of(workdir, row["repo"])
    if not (clone / ".git").exists():
        clone.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(clone), "init", "--quiet"], check=True)
        subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", f"https://github.com/{row['repo']}"], check=True)
    have = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{row['base_commit']}^{{commit}}"], capture_output=True)
    if have.returncode != 0:
        subprocess.run(["git", "-C", str(clone), "fetch", "--quiet", "--depth", "1", "origin", row["base_commit"]],
                       check=True, capture_output=True, timeout=1800)
    return clone


def view_at(clone: Path, commit: str) -> View:
    """The repository as it was at `commit`, read from the object store."""
    info = gitio.repo_info(clone)
    tree = gitio.git(clone, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{tree}}").decode().strip()
    return View(info.root, info.common_dir, info.object_format, tree, gitio.ls_tree(clone, tree), frozenset())


def prepare(workdir: Path, rows: list[dict], max_chunks: int) -> None:
    sock = worker.ensure()
    done = 0
    for number, row in enumerate(rows, 1):
        started = time.monotonic()
        try:
            clone = fetch(workdir, row)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"{number:4}/{len(rows)} {row['instance_id']:40} fetch failed: {str(exc)[:80]}", flush=True)
            continue
        view = view_at(clone, row["base_commit"])
        index = Index(open_db(index_file(view.common_dir)))
        while index.sync(view, budget_seconds=600).pending:
            pass
        store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
        have, total = store.coverage(index)
        if total > max_chunks:
            print(f"{number:4}/{len(rows)} {row['instance_id']:40} skipped: {total} chunks is over the limit", flush=True)
            index.conn.close()
            continue
        pool = EmbedPool(sock, capacity=worker.capacity_tokens(), connections=worker.slots())
        report = embedder.embed_pending(index, store, pool, batch=embedder.BATCH * worker.slots())
        pool.close()
        index.conn.close()
        done += 1
        print(f"{number:4}/{len(rows)} {row['instance_id']:40} files {len(view.files):6} chunks {total:7} "
              f"embedded {report.embedded:6} left {report.remaining} {time.monotonic() - started:6.1f} s {report.reason}", flush=True)
    print(f"prepared {done} of {len(rows)} instances", flush=True)


def first_files(paths: list[str], count: int = 10) -> list[str]:
    return list(dict.fromkeys(paths))[:count]


def bm25_files(view: View, question: str) -> list[str]:
    conn = apsw.Connection(":memory:")
    conn.execute("CREATE VIRTUAL TABLE files USING fts5(path UNINDEXED, body)")
    rows = []
    with gitio.Readers() as readers:
        for path in view.files:
            data = view.read(path, readers, filters.MAX_BYTES)
            if not isinstance(data, bytes):
                continue
            _, text = filters.classify(filters.family(path), data)
            if text is not None:
                rows.append((path, text))
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO files(path, body) VALUES (?, ?)", rows)
    conn.execute("COMMIT")
    words = list(dict.fromkeys(word.lower() for word in _WORD.findall(question)))
    if not words:
        return []
    match = " OR ".join(f'"{word}"' for word in words)
    return [row[0] for row in conn.execute("SELECT path FROM files WHERE files MATCH ? ORDER BY bm25(files) LIMIT 10", (match,))]


def dense_files(index: Index, store: VectorStore, vector) -> list[str]:
    paths: list[str] = []
    for chunk_id, _ in store.search(index, vector, 200):
        for oid, family in index.conn.execute(
                "SELECT bc.blob_oid, bc.family FROM blob_chunks bc JOIN temp.view_blobs v "
                "ON v.blob_oid = bc.blob_oid AND v.family = bc.family WHERE bc.chunk_id = ?", (chunk_id,)):
            paths.extend(index.paths_for(oid, family))
        if len(set(paths)) >= 10:
            break
    return first_files(paths)


def score(gold: list[str], found: list[str]) -> dict[str, float]:
    out = {}
    for cut in (1, 5, 10):
        top = set(found[:cut])
        out[f"recall@{cut}"] = len(top & set(gold)) / len(gold)
        out[f"all@{cut}"] = float(set(gold) <= top)
    first = next((position for position, path in enumerate(found, 1) if path in gold), 0)
    out["mrr"] = 1 / first if first else 0.0
    return out


def paired_bootstrap(a: list[float], b: list[float], rounds: int = 10_000, seed: int = 7) -> tuple[float, float, float]:
    """Mean of a - b over the same questions, with a 95% interval."""
    rng = random.Random(seed)
    differences = [x - y for x, y in zip(a, b)]
    count = len(differences)
    means = sorted(sum(differences[rng.randrange(count)] for _ in range(count)) / count for _ in range(rounds))
    return statistics.mean(differences), means[int(0.025 * rounds)], means[int(0.975 * rounds)]


def run(workdir: Path, rows: list[dict], out: Path | None, with_bm25: bool) -> dict:
    sock = worker.ensure()
    client = EmbedClient(sock, capacity=worker.capacity_tokens(), timeout=60)
    results = []
    for number, row in enumerate(rows, 1):
        clone = clone_of(workdir, row["repo"])
        try:
            view = view_at(clone, row["base_commit"])
        except gitio.GitError:
            continue  # not prepared
        index = Index(open_db(index_file(view.common_dir)))
        if index.sync(view, budget_seconds=0.0).pending:
            index.conn.close()
            continue
        store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
        have, total = store.coverage(index)
        if have < total:
            index.conn.close()
            continue  # not fully embedded: skipped rather than scored on partial vectors
        question = row["problem_statement"]
        try:
            vector = client.embed([question], kind="query")[0]
        except Unavailable as exc:
            raise SystemExit(f"the worker is needed for the query vectors: {exc}") from exc
        nearest = lambda limit, scoped: [chunk_id for chunk_id, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
        found = {
            "lexical": first_files([hit.path for hit in search(index, view, question, k=50)]),
            "hybrid": first_files([hit.path for hit in search(index, view, question, k=50, dense=nearest)]),
            "dense": dense_files(index, store, vector),
            "bm25": bm25_files(view, question) if with_bm25 else [],
        }
        index.conn.close()
        entry = {"instance": row["instance_id"], "repo": row["repo"], "gold": row["gold"], "chunks": total,
                 "files": found, "scores": {mode: score(row["gold"], found[mode]) for mode in MODES}}
        results.append(entry)
        print(f"{number:4}/{len(rows)} {row['instance_id']:40} " + "  ".join(
            f"{mode} {entry['scores'][mode]['recall@10']:.2f}" for mode in MODES), file=sys.stderr, flush=True)
    if not results:
        raise SystemExit("no prepared instances found; run `prepare` first")
    summary: dict = {"instances": len(results), "repos": sorted({entry["repo"] for entry in results})}
    for mode in MODES:
        summary[mode] = {key: round(statistics.mean(entry["scores"][mode][key] for entry in results), 3)
                         for key in results[0]["scores"][mode]}
    for name, (first, second) in {"hybrid_minus_lexical": ("hybrid", "lexical"), "dense_minus_lexical": ("dense", "lexical"),
                                  "lexical_minus_bm25": ("lexical", "bm25")}.items():
        summary[name] = {}
        for key in ("recall@10", "recall@1", "all@10", "mrr"):
            mean, low, high = paired_bootstrap([entry["scores"][first][key] for entry in results],
                                               [entry["scores"][second][key] for entry in results])
            summary[name][key] = {"mean": round(mean, 3), "low": round(low, 3), "high": round(high, 3)}
    print(json.dumps(summary, indent=2))
    if out:
        out.write_text(json.dumps({"summary": summary, "instances": results}, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("prepare", "run", "list"))
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--repos", help="comma-separated owner/name; default: every repository of Loc-Bench")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-chunks", type=int, default=250_000, help="prepare: skip a commit with more chunks than this")
    parser.add_argument("--no-bm25", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    rows = instances(set(args.repos.split(",")) if args.repos else None)[: args.limit]
    workdir = args.workdir.resolve()
    if args.action == "list":
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["repo"]] = counts.get(row["repo"], 0) + 1
        for repo, count in sorted(counts.items(), key=lambda item: -item[1]):
            print(f"{count:4}  {repo}")
        print(f"{len(rows)} instances in {len(counts)} repositories")
    elif args.action == "prepare":
        prepare(workdir, rows, args.max_chunks)
    else:
        run(workdir, rows, args.out, not args.no_bm25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
