"""The call graph's neighbours ranked by the question's words (DEFERRED entry 0-graph, part 10; rule written before any run).

Part 1's questions and search, as `graph_expand.py` asks them. A neighbour file (one step from the definitions of search's
first a results: the caller's file for a caller, the target's for a callee) is scored by how many of the question's words
its calls share: the call as written, the definitions at both ends, and the file's path, split at case changes and
underscores; certain calls first among equals. Arms R{a}n{n}: search's first a files, then the n best neighbour files with
a score above zero, then search's next files, ten in all; a of 5 or 7 and n of 1 or 3, chosen on draw 1.

Part 15: arms T{a}n{n}, the neighbour files ranked by their own text instead: their place among search's fifty files (the
index's words, and vectors where the language ships them, scored against the question); a neighbour search did not
rank at all is not added.

Part 18: arms V{a}n{n}, where vectors are shipped: every neighbour file scored by the best cosine of its own chunks'
vectors against the question's, the n nearest added after search's first a files.

    graph_rank.py WORKDIR --set locbench|others [--language L] --out FILE [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from graph_expand import DEFINITIONS_PER_HOP, GRADED, SHIPS_WITH_VECTORS, _definitions, _neighbours, draw_of  # noqa: E402

from aleph_zero import callgraph  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

ARMS = [(a, n) for a in (5, 7) for n in (1, 3)]
STOP = frozenset("the and for with from that this when not are was were has have does into can should would will get set new "
                 "use used using add fix bug error issue support allow make test".split())


def words(text: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return {word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if len(word) >= 3 and word not in STOP}


def ranked_files(references, question: set[str]) -> list[str]:
    best: dict[str, tuple[int, bool]] = {}
    for ref in references:
        said = words(" ".join((ref.written, ref.caller, ref.target)))
        for path in (ref.path, ref.target_path):
            if not path:
                continue
            score = len(question & (said | words(path)))
            if score and (path not in best or (score, ref.certain) > best[path]):
                best[path] = (score, ref.certain)
    return [path for path, _ in sorted(best.items(), key=lambda item: (-item[1][0], not item[1][1]))]


_CHUNK_FILES: dict = {}


def _chunk_files(index) -> dict[int, list[str]]:
    """The view's chunks and the files each lies in (a chunk many files share lies in each)."""
    key = (index.conn.filename, index._view_epoch)
    if key not in _CHUNK_FILES:
        _CHUNK_FILES.clear()
        out: dict[int, list[str]] = {}
        for chunk_id, oid, family in index.conn.execute(
                "SELECT bc.chunk_id, bc.blob_oid, bc.family FROM blob_chunks bc JOIN temp.view_chunks v ON v.chunk_id = bc.chunk_id"):
            out.setdefault(chunk_id, []).extend(index.paths_for(oid, family))
        _CHUNK_FILES[key] = out
    return _CHUNK_FILES[key]


def _files_of_all(references) -> list[str]:
    return [path for ref in references for path in (ref.path, ref.target_path) if path]


def arm(search_files: list[str], near: list[str], a: int, n: int) -> list[str]:
    out = list(search_files[:a])
    out += [path for path in near if path not in out][:n]
    for path in search_files[a:]:
        if len(out) == 10:
            break
        if path not in out:
            out.append(path)
    return out[:10]


def score_row(row, workdir: Path, client, ships_vectors: bool) -> dict | None:
    from aleph_zero import scipstore

    try:
        view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
    except Exception:  # noqa: BLE001
        return None
    index = Index(open_db(index_file(view.common_dir)))
    try:
        if index.sync(view, budget_seconds=0.0).pending:
            return None
        while callgraph.prepare(index, view, 600.0):
            pass
        graph = callgraph.load(index, view)
        compiled = scipstore.Compiled(index.conn, view.files)
        question = bench.question_forms(index, row["problem_statement"])["title"]
        nearest = None
        file_nearness: dict[str, float] = {}
        if ships_vectors and client is not None:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            if total and have == total:
                vector = client.embed([question], kind="query")[0]
                nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
                chunk_files = _chunk_files(index)
                for chunk_id, score in store.search(index, vector, 10 ** 7):
                    for path in chunk_files.get(chunk_id, ()):
                        if score > file_nearness.get(path, -2.0):
                            file_nearness[path] = score
        hits = bench.search(index, view, question, k=bench.DEEP, dense=nearest)
        search_files = bench.first_files([hit.path for hit in hits], 50)
        asked = words(question)
        gold = set(row["gold"])
        arms = {"baseline": search_files[:10]}
        for a in (5, 7):
            heads = [hit for hit in hits if hit.path in search_files[:a]]
            near = ranked_files(_neighbours(index, view, graph, compiled, _definitions(index, view, heads, DEFINITIONS_PER_HOP)), asked)
            for n in (1, 3):
                arms[f"R{a}n{n}"] = arm(search_files, near, a, n)
            neighbours = set(_files_of_all(_neighbours(index, view, graph, compiled, _definitions(index, view, heads, DEFINITIONS_PER_HOP))))
            by_text = [path for path in search_files[a:] if path in neighbours]
            for n in (1, 3):
                arms[f"T{a}n{n}"] = arm(search_files, by_text, a, n)
            if file_nearness:
                by_meaning = sorted((path for path in neighbours if path in file_nearness), key=lambda path: -file_nearness[path])
                for n in (1, 3):
                    arms[f"V{a}n{n}"] = arm(search_files, by_meaning, a, n)
        return {"instance": row["instance_id"], "repo": row["repo"], "draw": draw_of(row["instance_id"]),
                "language": row.get("language", "python"), "gold": sorted(gold),
                "recall": {name: len(set(files) & gold) / len(gold) for name, files in arms.items()}}
    finally:
        index.conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    workdir = args.workdir.resolve()
    rows = [row for row in bench.instances(None, args.set, args.language, 40) if row["repo"] not in GRADED][: args.limit]
    done = {json.loads(line)["instance"] for line in args.out.read_text().splitlines() if line.strip()} if args.out.exists() else set()
    ships_vectors = (args.language or "python") in SHIPS_WITH_VECTORS
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60) if ships_vectors else None
    with args.out.open("a") as out:
        for number, row in enumerate(rows, 1):
            if row["instance_id"] in done:
                continue
            entry = score_row(row, workdir, client, ships_vectors)
            if entry is None:
                print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
                continue
            out.write(json.dumps(entry) + "\n")
            out.flush()
            r = entry["recall"]
            print(f"{number:4}/{len(rows)} {row['instance_id']:40} " + " ".join(f"{k} {v:.2f}" for k, v in r.items()), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
