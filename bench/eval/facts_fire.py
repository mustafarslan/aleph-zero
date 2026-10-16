"""How often the facts hand-over fires on issues it is not made for (DEFERRED entry 0-graph, part 4; free, read first).

For each prepared Benchmark C question, the whole issue as the plugin would see it: which routes and environment names
`archfacts.mentioned` reads in it, and, where it reads any, what `facts(mentioned=…)` would hand the agent at the issue's
commit (files of routes, mounts, environment names and handlers, from source code only) and whether one is an answering
file. Questions that name nothing cost no call and are only counted.

    facts_fire.py WORKDIR --set locbench|others [--language L] --out FILE [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from graph_expand import GRADED  # noqa: E402

from aleph_zero import archfacts  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402


def score_row(row, workdir: Path) -> dict | None:
    names = archfacts.mentioned(row["problem_statement"][:4000])
    entry = {"instance": row["instance_id"], "repo": row["repo"], "language": row.get("language", "python"),
             "names": [[m.kind, m.name] for m in names], "gold": sorted(row["gold"]), "files": [], "kinds": []}
    if not names:
        return entry
    try:
        view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
    except Exception:  # noqa: BLE001 - not fetched: counted by its absence
        return None
    index = Index(open_db(index_file(view.common_dir)))
    try:
        if index.sync(view, budget_seconds=0.0).pending:
            return None
        while archfacts.prepare(index, view, 600.0):
            pass
        found = archfacts.matching(archfacts.load(index), names)
        found += archfacts.handlers(index, view, [each for each in found if each.kind == "route"])
        files = list(dict.fromkeys(each.path for each in found))
        entry.update(files=files, kinds=sorted({each.kind for each in found}), hit=bool(set(files) & set(row["gold"])))
        return entry
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
    with args.out.open("a") as out:
        for number, row in enumerate(rows, 1):
            if row["instance_id"] in done:
                continue
            entry = score_row(row, workdir)
            if entry is None:
                print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
                continue
            out.write(json.dumps(entry) + "\n")
            out.flush()
            if entry["names"]:
                print(f"{number:4}/{len(rows)} {row['instance_id']:40} names {len(entry['names'])} files {len(entry['files'])} "
                      f"hit {entry.get('hit')}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
