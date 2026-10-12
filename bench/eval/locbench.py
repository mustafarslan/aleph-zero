"""File-level localization on Loc-Bench: Aleph against a plain BM25 baseline.

Usage: uv run --group bench python bench/eval/locbench.py CLONE --repo django/django [--limit N] [--out FILE]

For every Loc-Bench instance of one repository: check out the instance's base commit
in CLONE (fetching it by id if needed: network), search with the issue text, and see
whether the files the fix edited are among the first ten files returned.

The baseline is fixed before it is run, so it cannot be tuned against Aleph: one FTS5
row per file with the whole file as text, SQLite's default `unicode61` tokenizer, no
column weights, no identifier expansion, every word of the issue OR-ed, default bm25().
It sees exactly the files Aleph indexes.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import apsw
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

from aleph_zero import filters
from aleph_zero.engine import Engine
from aleph_zero.view import build_view

DATASET = ("czlll/Loc-Bench_V1", "data/test-00000-of-00001.parquet")
CACHE = Path(__file__).resolve().parents[1] / "data" / "locbench"
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")


def instances(repo: str) -> list[dict]:
    path = hf_hub_download(DATASET[0], DATASET[1], repo_type="dataset", local_dir=CACHE)
    rows = [row for row in pq.read_table(path).to_pylist() if row["repo"] == repo]
    for row in rows:
        edited = row["edit_functions"]
        edited = ast.literal_eval(edited) if isinstance(edited, str) else list(edited)
        row["gold"] = sorted({entry.split(":")[0] for entry in edited})
    return [row for row in rows if row["gold"]]


def checkout(clone: Path, commit: str) -> None:
    def git(*args: str, check: bool = True):
        return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, check=check)

    if git("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
        git("fetch", "--quiet", "--depth", "1", "origin", commit)
    git("checkout", "--quiet", "--force", "--detach", commit)


def first_files(paths: list[str], count: int = 10) -> list[str]:
    return list(dict.fromkeys(paths))[:count]


def aleph_files(engine: Engine, clone: Path, question: str, k: int = 10) -> tuple[list[str], float]:
    started = time.perf_counter()
    answer = engine.search(question, k=k, cwd=clone)
    return first_files([hit.path for hit in answer.hits]), time.perf_counter() - started


def baseline_files(clone: Path, question: str) -> list[str]:
    view = build_view(clone)
    conn = apsw.Connection(":memory:")
    conn.execute("CREATE VIRTUAL TABLE files USING fts5(path UNINDEXED, body)")
    rows = []
    for path in view.files:
        try:
            data = (clone / path).read_bytes()
        except OSError:
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
    return [row[0] for row in conn.execute(
        "SELECT path FROM files WHERE files MATCH ? ORDER BY bm25(files) LIMIT 10", (match,))]


def score(gold: list[str], found: list[str]) -> dict[str, float]:
    out = {}
    for cut in (1, 5, 10):
        top = set(found[:cut])
        out[f"recall@{cut}"] = len(top & set(gold)) / len(gold)
        out[f"all@{cut}"] = float(set(gold) <= top)
    first = next((position for position, path in enumerate(found, 1) if path in gold), 0)
    out["mrr"] = 1 / first if first else 0.0
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clone", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("--k", type=int, default=10,
                        help="results asked of Aleph per question; ten is what an agent asks for, and more "
                             "lets Aleph fill the ten files from deeper in its ranking than an agent sees")
    args = parser.parse_args(argv)
    clone = args.clone.resolve()
    rows = instances(args.repo)[: args.limit]
    engine = Engine(clone)
    per_instance = []
    for number, row in enumerate(rows, 1):
        checkout(clone, row["base_commit"])
        question = row["problem_statement"]
        engine.search("warm up", cwd=clone, k=1)  # index this commit before timing the question
        while engine.status(cwd=clone)["pending"]:
            engine.search("warm up", cwd=clone, k=1)
        ours, seconds = aleph_files(engine, clone, question, args.k)
        theirs = [] if args.no_baseline else baseline_files(clone, question)
        entry = {"instance": row["instance_id"], "gold": row["gold"], "aleph": score(row["gold"], ours),
                 "baseline": score(row["gold"], theirs), "aleph_ms": round(seconds * 1000, 1),
                 "aleph_files": ours, "baseline_files": theirs, "words": len(_WORD.findall(question))}
        per_instance.append(entry)
        print(f"{number:3}/{len(rows)} {row['instance_id']:28} aleph r@10 {entry['aleph']['recall@10']:.2f} "
              f"baseline r@10 {entry['baseline']['recall@10']:.2f}  {entry['aleph_ms']:.0f} ms", file=sys.stderr)
    summary = {
        "repo": args.repo, "instances": len(per_instance),
        "aleph": {key: round(statistics.mean(e["aleph"][key] for e in per_instance), 3) for key in per_instance[0]["aleph"]},
        "baseline": {key: round(statistics.mean(e["baseline"][key] for e in per_instance), 3) for key in per_instance[0]["baseline"]},
        "aleph_ms_p50": statistics.median(e["aleph_ms"] for e in per_instance),
    }
    text = json.dumps({"summary": summary, "instances": per_instance}, indent=2)
    print(json.dumps(summary, indent=2))
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
