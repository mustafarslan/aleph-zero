"""The call graph on finding the files of an issue (DEFERRED entry 0-graph, part 1; rules written before any run).

For each prepared Benchmark C question, by its first line, in the arm its language ships with:

- baseline: the first ten distinct files of search (from fifty results);
- reach: which answering files the definitions of search's first five results reach in one step (callers and callees,
  any certainty), and in two; the ceiling of any expansion;
- arms E5 and E7: search's first a files, then the files of the one-step neighbours of the first a results'
  definitions (certain ones first), then search's next files, ten in all.

Results are appended a question at a time to OUT (JSON lines); a run started again goes on where it was.

    graph_expand.py WORKDIR --set locbench|others [--language L] --out FILE [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402

from aleph_zero import callgraph, symbols  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

# The repositories the code graph was graded on (ADR-003): not asked here.
GRADED = {"go-kit/kit", "BurntSushi/ripgrep", "nestjs/nest", "encode/httpx", "eslint/eslint", "spring-projects/spring-petclinic",
          "fastapi/fastapi", "tiangolo/fastapi", "typestack/routing-controllers", "pallets/flask", "blevesearch/bleve",
          "rayon-rs/rayon", "image-rs/image"}
SHIPS_WITH_VECTORS = {"javascript", "typescript", "rust", "c", "cpp"}
DEFINITIONS_PER_HOP = 40


def draw_of(instance: str) -> int:
    return 1 + int(hashlib.sha256(instance.encode()).hexdigest(), 16) % 2


def _definitions(index, view, hits, limit: int) -> list[symbols.SymbolHit]:
    """The definitions search's hits belong to, in their order."""
    found: list[symbols.SymbolHit] = []
    for hit in hits:
        name = (hit.symbol or "").split(".")[-1].lstrip("#")
        if not name:
            continue
        first, last = hit.scope_start_line or hit.start_line, hit.scope_end_line or hit.end_line
        for each in symbols.lookup(index, view, name, limit=50, nested=False):
            if each.path == hit.path and first <= each.line <= last and each not in found:
                found.append(each)
                break
        if len(found) >= limit:
            break
    return found


def _neighbours(index, view, graph, compiled, defs) -> list[callgraph.Reference]:
    if not defs:
        return []
    calling, _ = callgraph.callers(index, graph, defs, compiled)
    called, _ = callgraph.callees(index, view, graph, defs, compiled)
    return calling + called


def _files_of(references, certain_first: bool) -> list[str]:
    """Each file a neighbour is in: the caller's file for a caller, the target's for a callee."""
    order = sorted(references, key=lambda ref: (not ref.certain, ref.candidates)) if certain_first else references
    files: list[str] = []
    for ref in order:
        for path in (ref.path, ref.target_path):
            if path and path not in files:
                files.append(path)
    return files


def _second_hop(index, view, graph, compiled, references) -> list[str]:
    defs: list[symbols.SymbolHit] = []
    for ref in references:
        for path, qualified in ((ref.target_path, ref.target), (ref.path, ref.caller)):
            name = (qualified or "").split(".")[-1]
            if not name or not path:
                continue
            for each in symbols.lookup(index, view, name, limit=20, nested=False):
                if each.path == path and each not in defs:
                    defs.append(each)
                    break
        if len(defs) >= DEFINITIONS_PER_HOP:
            break
    return _files_of(_neighbours(index, view, graph, compiled, defs), certain_first=False)


def expanded(search_files: list[str], hits, a: int, index, view, graph, compiled) -> list[str]:
    head = search_files[:a]
    firsts = [hit for hit in hits if hit.path in head]
    near = _files_of(_neighbours(index, view, graph, compiled, _definitions(index, view, firsts, DEFINITIONS_PER_HOP)), True)
    out = list(head)
    for path in near + search_files[a:]:
        if path not in out:
            out.append(path)
        if len(out) == 10:
            break
    return out


def score_row(row, workdir: Path, client, ships_vectors: bool) -> dict | None:
    from aleph_zero import scipstore

    try:
        view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
    except Exception:  # noqa: BLE001 - not fetched: counted by its absence
        return None
    index = Index(open_db(index_file(view.common_dir)))
    try:
        if index.sync(view, budget_seconds=0.0).pending:
            return None
        while callgraph.prepare(index, view, 600.0):          # the graph facts of every file, before any score
            pass
        graph = callgraph.load(index, view)
        compiled = scipstore.Compiled(index.conn, view.files)
        question = bench.question_forms(index, row["problem_statement"])["title"]
        nearest = None
        if ships_vectors and client is not None:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            if total and have == total:
                vector = client.embed([question], kind="query")[0]
                nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
        hits = bench.search(index, view, question, k=bench.DEEP, dense=nearest)
        search_files = bench.first_files([hit.path for hit in hits], 50)
        baseline = search_files[:10]
        top5 = [hit for hit in hits if hit.path in search_files[:5]]
        one_refs = _neighbours(index, view, graph, compiled, _definitions(index, view, top5, DEFINITIONS_PER_HOP))
        one = _files_of(one_refs, certain_first=False)
        two = _second_hop(index, view, graph, compiled, one_refs)
        gold = set(row["gold"])
        arms = {"baseline": baseline, "E5": expanded(search_files, hits, 5, index, view, graph, compiled),
                "E7": expanded(search_files, hits, 7, index, view, graph, compiled)}
        return {"instance": row["instance_id"], "repo": row["repo"], "draw": draw_of(row["instance_id"]),
                "language": row.get("language", "python"), "vectors": nearest is not None, "gold": sorted(gold),
                "recall": {arm: len(set(files) & gold) / len(gold) for arm, files in arms.items()},
                "reach": {"baseline": sorted(gold & set(baseline)), "hop1": sorted(gold & set(one)),
                          "hop2": sorted(gold & (set(one) | set(two)))},
                "neighbour_files": len(one)}
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
            print(f"{number:4}/{len(rows)} {row['instance_id']:40} base {r['baseline']:.2f} E5 {r['E5']:.2f} E7 {r['E7']:.2f} "
                  f"hop1 +{len(set(entry['reach']['hop1']) - set(entry['reach']['baseline']))}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
