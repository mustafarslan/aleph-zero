"""Summaries over the graph, the free variant (DEFERRED entry 0-graph, part 14; rule written before any run).

Questions about a whole area of a project ("how is a recipe imported from a URL"), drawn by fresh agents from each
project's documentation and answered by them with grep (`area_questions_*.json`). A module is a directory of source
code; its summary is made without a model: the words of its path, the names and signatures of what its files define,
and the routes, models and environment names its files state. Summaries are ranked by BM25 against the question.

- search: search's first ten files;
- M1: the files of the best module first (four at most, in search's order, then the rest by their own words), then
  search's; M2: the two best modules, three files each. Chosen on draw 1, read on draw 2;
- ceiling: the answering files in the three best modules that search's ten miss.

    area_bench.py WORKDIR --out FILE
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from facts_bench import SHIPS_WITH_VECTORS, _ten  # noqa: E402

from aleph_zero import archfacts  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

HERE = Path(__file__).resolve().parent
SETS = {"immich-app/immich": ("area_questions_immich.json", "typescript"), "mealie-recipes/mealie": ("area_questions_mealie.json", "python"),
        "zulip/zulip": ("area_questions_zulip.json", "python"), "paperless-ngx/paperless-ngx": ("area_questions_paperless.json", "python")}


def draw_of(instance: str) -> int:
    return 1 + int(hashlib.sha256(instance.encode()).hexdigest(), 16) % 2


class BM25:
    def __init__(self, docs: dict[str, collections.Counter], k1: float = 1.2, b: float = 0.75) -> None:
        self.docs, self.k1, self.b = docs, k1, b
        self.avg = sum(sum(c.values()) for c in docs.values()) / max(1, len(docs))
        self.df = collections.Counter(word for c in docs.values() for word in c)

    def rank(self, words: set[str]) -> list[tuple[str, float]]:
        n = len(self.docs)
        out = []
        for key, counts in self.docs.items():
            size = sum(counts.values())
            score = 0.0
            for word in words & counts.keys():
                idf = math.log(1 + (n - self.df[word] + 0.5) / (self.df[word] + 0.5))
                tf = counts[word]
                score += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * size / max(1.0, self.avg)))
            if score > 0:
                out.append((key, score))
        return sorted(out, key=lambda pair: -pair[1])


def summaries(index: Index) -> tuple[dict[str, collections.Counter], dict[str, collections.Counter]]:
    """By module and by file: the words of paths, definitions and facts."""
    by_file: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    rows = index.conn.execute("SELECT s.blob_oid, s.family, s.name, s.qualified, s.signature FROM symbols s "
                              "JOIN temp.view_blobs v ON v.blob_oid = s.blob_oid AND v.family = s.family WHERE s.nested = 0")
    for oid, family, name, qualified, signature in rows:
        words = archfacts.words_of(" ".join(filter(None, (name, qualified, (signature or "").split("\n")[0]))))
        for path in index.paths_for(oid, family):
            by_file[path].update(words)
    for fact in archfacts.load(index):
        by_file[fact.path].update(archfacts.words_of(fact.name + " " + fact.detail))
    by_file = {path: counts for path, counts in by_file.items() if not archfacts.NEVER_AN_ANSWER.search(path)}
    for path in by_file:
        by_file[path].update(archfacts.words_of(path))
    by_module: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for path, counts in by_file.items():
        by_module[str(PurePosixPath(path).parent)].update(counts)
    return dict(by_module), by_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    commits = {row["repo"]: row["base_commit"] for row in json.loads((HERE / "facts_questions.json").read_text())}
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    results = []
    for repo, (name, language) in SETS.items():
        if not (HERE / name).exists():
            print(f"{repo}: no questions yet, left out", file=sys.stderr)
            continue
        view = bench.view_at(bench.clone_of(args.workdir.resolve(), repo), commits[repo])
        index = Index(open_db(index_file(view.common_dir)))
        try:
            index.sync(view, budget_seconds=600.0, symbols=True)
            while archfacts.prepare(index, view, 600.0):
                pass
            modules, files = summaries(index)
            module_rank, file_rank = BM25(modules), BM25(files)
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            dense = language in SHIPS_WITH_VECTORS and total and have == total
            for number, row in enumerate(json.loads((HERE / name).read_text()), 1):
                question, gold = row["question"], set(row["gold"])
                nearest = None
                if dense:
                    vector = client.embed([question], kind="query")[0]
                    nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
                searched = bench.first_files([hit.path for hit in bench.search(index, view, question, k=bench.DEEP, dense=nearest)], 50)
                asked = archfacts.words_of(question)
                best = [module for module, _ in module_rank.rank(asked)[:3]]
                own = {path: place for place, (path, _) in enumerate(file_rank.rank(asked))}

                def module_files(module: str, cap: int) -> list[str]:
                    inside = [path for path in files if str(PurePosixPath(path).parent) == module]
                    return sorted(inside, key=lambda path: (searched.index(path) if path in searched else 50, own.get(path, 10**6)))[:cap]
                arms = {"search": searched[:10],
                        "M1": _ten((module_files(best[0], 4) if best else []) + searched),
                        "M2": _ten([path for module in best[:2] for path in module_files(module, 3)] + searched)}
                reach = {path for module in best for path in module_files(module, 10**6)}
                instance = f"{repo.split('/')[1]}-area-{number}"
                results.append({"instance": instance, "repo": repo, "draw": draw_of(instance), "question": question, "gold": sorted(gold),
                                "modules": best, "recall": {arm: len(set(found) & gold) / len(gold) for arm, found in arms.items()},
                                "beyond": sorted((gold & reach) - set(searched[:10]))})
                print(f"{instance:28} " + " ".join(f"{k} {v:.2f}" for k, v in results[-1]["recall"].items()), file=sys.stderr, flush=True)
        finally:
            index.conn.close()
    args.out.write_text(json.dumps(results, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
