"""Part 17's guard: does placing the best modules' files first lower search on issues people wrote?

Part 1's questions (the 1,398 of Benchmark C in six languages), asked by their first line as part 1 asked them, through
the engine's `search` with `search.AREAS` "off", "M2floor" and "M2": recall@10 of the first ten distinct files, and
whether the step changed the ten at all.

    area_guard.py WORKDIR --set locbench|others [--language L] --out FILE
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from graph_expand import GRADED, SHIPS_WITH_VECTORS, draw_of  # noqa: E402

from aleph_zero import search as search_module  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

ARMS = ("off", "M2floor", "M2")


def score_row(row, workdir: Path, client, ships_vectors: bool) -> dict | None:
    try:
        view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
    except Exception:  # noqa: BLE001
        return None
    index = Index(open_db(index_file(view.common_dir)))
    try:
        if index.sync(view, budget_seconds=0.0).pending:
            return None
        question = bench.question_forms(index, row["problem_statement"])["title"]
        nearest = None
        if ships_vectors and client is not None:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            if total and have == total:
                vector = client.embed([question], kind="query")[0]
                nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
        gold = set(row["gold"])
        files = {}
        for arm in ARMS:
            search_module.AREAS = arm
            files[arm] = bench.first_files([hit.path for hit in bench.search(index, view, question, k=bench.DEEP, dense=nearest)], 10)
        search_module.AREAS = "off"
        return {"instance": row["instance_id"], "repo": row["repo"], "language": row.get("language", "python"),
                "draw": draw_of(row["instance_id"]), "recall": {arm: len(set(f) & gold) / len(gold) for arm, f in files.items()},
                "changed": {arm: files[arm] != files["off"] for arm in ARMS if arm != "off"}}
    finally:
        index.conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = [row for row in bench.instances(None, args.set, args.language, 40) if row["repo"] not in GRADED]
    done = {json.loads(line)["instance"] for line in args.out.read_text().splitlines() if line.strip()} if args.out.exists() else set()
    ships = (args.language or "python") in SHIPS_WITH_VECTORS
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60) if ships else None
    with args.out.open("a") as out:
        for number, row in enumerate(rows, 1):
            if row["instance_id"] in done:
                continue
            entry = score_row(row, args.workdir.resolve(), client, ships)
            if entry is None:
                print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
                continue
            out.write(json.dumps(entry) + "\n")
            out.flush()
            print(f"{number:4}/{len(rows)} {row['instance_id']:40} " + " ".join(f"{k} {v:.2f}" for k, v in entry["recall"].items()),
                  file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
