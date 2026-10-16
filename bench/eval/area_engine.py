"""Module summaries through the engine's own search (DEFERRED entry 0-graph, part 17; rules written before any run).

The 72 area questions (`area_questions_*.json`, six repositories), each asked of `search` with `search.AREAS` off, "M2"
and "M2floor": the first ten distinct files of its results (from fifty), as part 14 read them.

    area_engine.py --out FILE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from facts_bench import SHIPS_WITH_VECTORS  # noqa: E402

from aleph_zero import search as search_module  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FACTS = ROOT / "bench" / "data" / "benchc-facts"
HELDOUT = ROOT / "bench" / "data" / "heldout"
SETS = {   # repository: (questions, language, clone, commit or None for part 2's)
    "immich-app/immich": ("area_questions_immich.json", "typescript", FACTS / "immich-app__immich", None),
    "mealie-recipes/mealie": ("area_questions_mealie.json", "python", FACTS / "mealie-recipes__mealie", None),
    "zulip/zulip": ("area_questions_zulip.json", "python", FACTS / "zulip__zulip", None),
    "paperless-ngx/paperless-ngx": ("area_questions_paperless.json", "python", FACTS / "paperless-ngx__paperless-ngx", None),
    "knadh/listmonk": ("area_questions_listmonk.json", "go", FACTS / "knadh__listmonk", None),
    "healthchecks/healthchecks": ("area_questions_healthchecks.json", "python", HELDOUT / "healthchecks", "9b9e6f964b4221964fdb6ba5782681ef092866e5"),
}
ARMS = ("off", "M2", "M2floor")


def draw_of(instance: str) -> int:
    return 1 + int(hashlib.sha256(instance.encode()).hexdigest(), 16) % 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    commits = {row["repo"]: row["base_commit"] for row in json.loads((HERE / "facts_questions.json").read_text())}
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    results = []
    for repo, (name, language, clone, commit) in SETS.items():
        view = bench.view_at(clone, commit or commits[repo])
        index = Index(open_db(index_file(view.common_dir)))
        try:
            index.sync(view, budget_seconds=600.0, symbols=True)
            from aleph_zero import archfacts
            while archfacts.prepare(index, view, 600.0):
                pass
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            dense = language in SHIPS_WITH_VECTORS and total and have == total
            for number, row in enumerate(json.loads((HERE / name).read_text()), 1):
                nearest = None
                if dense:
                    vector = client.embed([row["question"]], kind="query")[0]
                    nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
                gold = set(row["gold"])
                recall = {}
                for arm in ARMS:
                    search_module.AREAS = arm
                    found = bench.first_files([hit.path for hit in bench.search(index, view, row["question"], k=bench.DEEP, dense=nearest)], 10)
                    recall[arm] = len(set(found) & gold) / len(gold)
                search_module.AREAS = "off"
                instance = f"{repo.split('/')[1]}-area-{number}"
                results.append({"instance": instance, "repo": repo, "language": language, "draw": draw_of(instance),
                                "gold": sorted(gold), "recall": recall})
                print(f"{instance:28} " + " ".join(f"{k} {v:.2f}" for k, v in recall.items()), file=sys.stderr, flush=True)
        finally:
            index.conn.close()
    args.out.write_text(json.dumps(results, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
