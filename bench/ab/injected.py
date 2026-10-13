"""What rung 2 put before the agent in each session of a run, worked out afterwards.

On rung 2 the mod asks the engine once, with the session's first prompt, and adds the five best
locations to the prompt if the first of them was proposed by two generators (plugin/hooks/logic.js).
The harness does not keep whether that happened, and the mod's own meter keeps its last session only.
The engine's answer depends on the index and the prompt alone while vectors are off, so it is asked
again here, of the same commit in a worktree of its own, as a session had it.

  uv run --group bench python bench/ab/injected.py bench/data/benchc bench/data/results/ab/<file>.jsonl

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


def added(hits: list[dict], gold: list[str]) -> dict:
    """What the mod makes of the engine's hits: nothing unless two generators proposed the first."""
    confident = bool(hits) and len(hits[0].get("sources") or ()) >= 2
    locations: list[str] = []
    files: list[str] = []
    if confident:
        for hit in hits:
            where = f"{hit['path']}:{hit['start_line']}-{hit['end_line']}"
            if where in locations:
                continue
            locations.append(where)
            if hit["path"] not in files:
                files.append(hit["path"])
            if len(locations) >= LOCATIONS:
                break
    return {"added": confident, "locations": locations, "files": files, "answer_named": bool(set(files) & set(gold))}


def asked_again(workdir: Path, task: dict) -> dict:
    """The engine's answer to the session's prompt at the task's commit, read as the mod reads it."""
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
        answer = render.as_json(engine.search((task["issue"] + ab.QUESTION)[:PROMPT_CHARS], k=LOCATIONS, cwd=tree))
        return {**added(answer["hits"], task["gold"]), "vectors": answer["dense"], "pending": answer["pending"]}
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path, help="the Benchmark C work directory the run used")
    parser.add_argument("results", type=Path, help="the run's results file")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--baseline", default="none")
    parser.add_argument("--arm", default="L2")
    parser.add_argument("--out", type=Path, help="keep what was added per task here")
    args = parser.parse_args(argv)
    records = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    wanted = {record["task"] for record in records}
    given: dict[str, dict] = {}
    for task in ab.tasks(args.target, args.count, args.seed):
        if task["id"] in wanted:
            given[task["id"]] = asked_again(args.target, task)
            entry = given[task["id"]]
            print(f"{task['id']:44} {'added' if entry['added'] else 'silent':6} "
                  f"{'answer among them' if entry['answer_named'] else '':18} {', '.join(entry['files'])}", file=sys.stderr, flush=True)
    if args.out:
        args.out.write_text(json.dumps(given, indent=2) + "\n")
    print(json.dumps({"tasks": len(given), "locations_added": sum(entry["added"] for entry in given.values()),
                      "answer_among_them": sum(entry["answer_named"] for entry in given.values()),
                      "with_vectors": sum(entry["vectors"] == "used" for entry in given.values()),
                      "groups": groups(records, given, args.baseline, args.arm)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
