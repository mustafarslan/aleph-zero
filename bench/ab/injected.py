"""What rung 2 put before the agent in each session of a run, worked out afterwards.

On rung 2 the mod asks the engine once, with the session's first prompt, and adds its best locations
to the prompt if the first of them was proposed by two generators (plugin/hooks/logic.js): five places
of any kind until 2026-10-13 (`--variant places`), five files of code with their places since.
The harness does not keep whether that happened, and the mod's own meter keeps its last session only.
The engine's answer depends on the index and the prompt alone while vectors are off, so it is asked
again here, of the same commit in a worktree of its own, as a session had it.

  uv run --group bench python bench/ab/injected.py bench/data/benchc bench/data/results/ab/<file>.jsonl [--variant places]
  uv run --group bench python bench/ab/injected.py bench/data/benchc      # every prepared question, several variants, no sessions

This is a reading made after the run and not one of the measures fixed before it (ADR-011): it says
which pairs could have gained anything from rung 2, not whether rung 2 passes.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as ab  # noqa: E402

LOCATIONS = 5        # as the mod asks and shows
PROMPT_CHARS = 2000  # of the prompt, as the mod cuts it
NAMED = "locations added, a file of the answer among them"
NOT_NAMED = "locations added, no file of the answer among them"
NOTHING = "nothing added"
VARIANTS = {    # name -> (hits asked for, kind, one line a file)
    "places": (LOCATIONS, "any", False),            # the mod until 2026-10-13, and in that day's run: five places of any kind
    "places, code": (LOCATIONS, "code", False),
    "files": (15, "any", True),
    "files, code": (15, "code", True),              # the mod since: code only, a line a file, five files
}
AS_RUN = "places"
NOW = "files, code"
SPANS = 3            # places of one file on its line, when a file gets one line


def added(hits: list[dict], gold: list[str], files: int = LOCATIONS, per_file: bool = False) -> dict:
    """What the mod makes of the engine's hits: nothing unless two generators proposed the first. As the mod has it,
    a place is a line (`files` of them). With `per_file` a file is a line, with the further places of that file on it."""
    confident = bool(hits) and len(hits[0].get("sources") or ()) >= 2
    spans: dict[str, list[str]] = {}      # file -> its places, in the order the files were first proposed
    if confident and per_file:
        for hit in hits:
            if hit["path"] not in spans and len(spans) >= files:
                continue                  # a later file: the lines are full, but a listed file may still gain a place
            listed = spans.setdefault(hit["path"], [])
            span = f"{hit['start_line']}-{hit['end_line']}"
            if span not in listed and len(listed) < SPANS:
                listed.append(span)
        locations = [f"{path}:{', '.join(listed)}" for path, listed in spans.items()]
    else:
        places = _first_places(hits, files) if confident else []
        for hit in places:
            spans.setdefault(hit["path"], []).append(f"{hit['start_line']}-{hit['end_line']}")
        locations = [f"{hit['path']}:{hit['start_line']}-{hit['end_line']}" for hit in places]
    return {"added": confident, "locations": locations, "files": list(spans), "answer_named": bool(set(spans) & set(gold))}


def _first_places(hits: list[dict], count: int) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for hit in hits:
        where = (hit["path"], hit["start_line"], hit["end_line"])
        if where not in seen:
            seen.add(where)
            out.append(hit)
        if len(out) >= count:
            break
    return out


def asked_again(workdir: Path, task: dict, variant: str = NOW) -> dict:
    """The engine's answer to the session's prompt at the task's commit, asked and read as that variant of the mod does."""
    k, kind, per_file = VARIANTS[variant]
    from aleph_zero import render
    from aleph_zero.engine import Engine

    clone = ab.clone_of(workdir, task["repo"]).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="az-ab-", dir="/tmp"))
    tree = scratch / "repo"
    git = lambda *args: subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True)  # noqa: E731
    made = git("worktree", "add", "--detach", "--force", str(tree), task["commit"])
    if made.returncode != 0:
        raise SystemExit(f"no worktree of {task['id']}: {made.stderr[-200:]}")
    try:
        engine = Engine()
        answer = render.as_json(engine.search((task["issue"] + ab.QUESTION)[:PROMPT_CHARS], k=k, kind=kind, cwd=tree))
        return {**added(answer["hits"], task["gold"], per_file=per_file), "vectors": answer["dense"], "pending": answer["pending"]}
    finally:
        git("worktree", "remove", "--force", str(tree))
        subprocess.run(["rm", "-rf", str(scratch)])


def groups(records: list[dict], given: dict[str, dict], baseline: str, arm: str) -> dict:
    """The pairs of a run by what rung 2 gave the task: how many, the geometric mean of the ratios of tokens and
    of tool calls (arm over baseline), and in how many the right file was named ([arm, baseline])."""
    runs: dict[str, dict[str, list[dict]]] = {}
    for record in records:
        if record.get("ok"):
            runs.setdefault(record["arm"], {}).setdefault(record["task"], []).append(record)
    mean = lambda rows, key: statistics.fmean(float(row[key]) for row in rows)  # noqa: E731
    sorted_into: dict[str, list[str]] = {NAMED: [], NOT_NAMED: [], NOTHING: []}
    for task in sorted(set(runs.get(arm, {})) & set(runs.get(baseline, {})) & set(given)):
        sorted_into[NOTHING if not given[task]["added"] else NAMED if given[task]["answer_named"] else NOT_NAMED].append(task)
    out: dict = {}
    for name, tasks in sorted_into.items():
        entry: dict = {"tasks": len(tasks)}
        if tasks:
            for measure in ("tokens", "tool_calls"):
                ratios = [max(mean(runs[arm][task], measure), 1) / max(mean(runs[baseline][task], measure), 1) for task in tasks]
                entry[f"{measure}_ratio"] = round(statistics.geometric_mean(ratios), 3)
            entry["file_hits"] = [round(sum(mean(runs[side][task], "file_hit") for task in tasks)) for side in (arm, baseline)]
        out[name] = entry
    return out


def offline(workdir: Path, rows: list[dict], say=lambda text: None) -> dict:
    """For every prepared question: what each variant of rung 2 would add to the session's prompt. Words and names only,
    asked of the commit's tree in the index (no worktree, no worker): what the engine answers while vectors are off."""
    import benchmark_c
    from dataclasses import asdict

    from aleph_zero import gitio
    from aleph_zero.indexer import Index
    from aleph_zero.search import search
    from aleph_zero.store import index_file, open_db

    counts = {name: {"added": 0, "answer_named": 0, "files": 0} for name in VARIANTS}
    scored = 0
    for number, row in enumerate(rows, 1):
        try:
            view = benchmark_c.view_at(benchmark_c.clone_of(workdir, row["repo"]), row["base_commit"])
        except gitio.GitError:
            continue
        index = Index(open_db(index_file(view.common_dir)))
        if index.sync(view, budget_seconds=0.0).pending:
            index.conn.close()
            continue
        scored += 1
        prompt = (row["problem_statement"] + ab.QUESTION)[:PROMPT_CHARS]
        for name, (k, kind, per_file) in VARIANTS.items():
            hits = [asdict(hit) for hit in search(index, view, prompt, k=k, kind=kind)]
            entry = added(hits, row["gold"], per_file=per_file)
            counts[name]["added"] += entry["added"]
            counts[name]["answer_named"] += entry["answer_named"]
            counts[name]["files"] += len(entry["files"])
        index.conn.close()
        if number % 50 == 0:
            say(f"{number} of {len(rows)}")
    return {"questions": scored, "variants": {
        name: {"added": entry["added"], "added_share": round(entry["added"] / scored, 3) if scored else None,
               "answer_named": entry["answer_named"], "answer_named_share": round(entry["answer_named"] / scored, 3) if scored else None,
               "files_when_added": round(entry["files"] / entry["added"], 2) if entry["added"] else None}
        for name, entry in counts.items()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path, help="the Benchmark C work directory the run used")
    parser.add_argument("results", type=Path, nargs="?", help="the run's results file; without one, every prepared question is read (`offline`)")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--baseline", default="none")
    parser.add_argument("--arm", default="L2")
    parser.add_argument("--out", type=Path, help="keep what was added per task here")
    parser.add_argument("--variant", choices=sorted(VARIANTS), default=NOW,
                        help=f"the mod the run was made with; the run of 2026-10-13 was made with '{AS_RUN}'")
    args = parser.parse_args(argv)
    if args.results is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
        import benchmark_c

        report = offline(args.target, benchmark_c.instances(None), say=lambda text: print(text, file=sys.stderr, flush=True))
        print(json.dumps(report, indent=2))
        if args.out:
            args.out.write_text(json.dumps(report, indent=2) + "\n")
        return 0
    records = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    wanted = {record["task"] for record in records}
    given: dict[str, dict] = {}
    for task in ab.tasks(args.target, args.count, args.seed):
        if task["id"] in wanted:
            given[task["id"]] = asked_again(args.target, task, args.variant)
            entry = given[task["id"]]
            print(f"{task['id']:44} {'added' if entry['added'] else 'silent':6} "
                  f"{'answer among them' if entry['answer_named'] else '':18} {', '.join(entry['files'])}", file=sys.stderr, flush=True)
    if args.out:
        args.out.write_text(json.dumps({"variant": args.variant, "tasks": given}, indent=2) + "\n")   # what it was read with stays with it
    print(json.dumps({"variant": args.variant, "tasks": len(given), "locations_added": sum(entry["added"] for entry in given.values()),
                      "answer_among_them": sum(entry["answer_named"] for entry in given.values()),
                      "with_vectors": sum(entry["vectors"] == "used" for entry in given.values()),
                      "groups": groups(records, given, args.baseline, args.arm)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
