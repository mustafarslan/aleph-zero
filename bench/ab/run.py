"""Does Claude Code find the right files with fewer tokens and tool calls when it has Aleph?

Usage:
  uv run --group bench python bench/ab/run.py tasks  bench/data/benchc --count 20
  uv run --group bench python bench/ab/run.py run    bench/data/benchc --arms none,L1 --count 2 --model haiku
  uv run --group bench python bench/ab/run.py report bench/data/results/ab/<file>.jsonl --baseline none

Each task is a Loc-Bench issue. For every task and arm a fresh `claude -p` session is
started in a temporary worktree of the repository at the issue's base commit, asked
which files must change, and its event stream is read: tokens, tool calls by name,
time, and whether the files it named are the ones the fix edited.

Arms: `none` (no plugin: Claude Code as it is), `L0` to `L3` (the plugin at that rung;
L0 is the engine's tools without steering), and `L1n` (rung 1 without the pointer in the
descriptions of Grep and Glob, which is how the pilot ran rung 1). Arms are interleaved per task, so a task's
sessions run close together. Every session is bounded by a cost limit and a wall clock.

THIS SPENDS THE OWNER'S CLAUDE PLAN USAGE. `run` refuses more than --max-sessions
(default 8) unless told otherwise.

What is fixed before any session runs (docs/adr/ADR-011): the primary measure is the
total tokens of the session (input, output, cache read, cache write; all models, so
subagents count); the comparison is the paired ratio per task, with a bootstrap
interval over tasks; tool calls are the second measure; the file hit rate of an arm
must not be lower than the baseline's by more than one task. Calls before the first
read of a gold file, and sessions that named no file, are reported and decide nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench" / "tools"))
import longrun  # noqa: E402  pause, resume, stop and status of a long job
PLUGIN = ROOT / "plugin"
ENGINE_TOOLS = "mcp__plugin_aleph-zero_aleph__*"
# What a session may do without being asked: read and search, nothing that changes anything.
ALLOWED = ["Read", "Grep", "Glob", "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(ls:*)", "Bash(cat:*)",
           "Bash(head:*)", "Bash(wc:*)", "Bash(git grep:*)", "Bash(git log:*)", "Bash(git show:*)"]
QUESTION = (
    "\n\n---\nThe text above is an issue reported against this repository. Find the source files that have to be "
    "changed to resolve it. Do not modify anything and do not run the tests. When you are confident, end your "
    "answer with one line of JSON and nothing after it, in exactly this form: "
    '{"files": ["path/relative/to/the/repository/root.py"]} listing at most five files, most likely first.'
)
ARMS = ("none", "L0", "L1n", "L1", "L2", "L3")
JOB = "A/B run"


def tasks(workdir: Path, count: int, seed: int = 11, with_dev: bool = False) -> list[dict]:
    """`count` Loc-Bench issues from the repositories already prepared for Benchmark C,
    spread over repositories, the same ones for a given seed. Tasks the steering was
    developed on (dev_tasks.txt) are left out unless `with_dev`."""
    sys.path.insert(0, str(ROOT / "bench" / "eval"))
    import benchmark_c

    prepared = {path.name.replace("__", "/", 1) for path in workdir.iterdir() if (path / ".git").exists() or path.is_symlink()}
    seen = {line.strip() for line in (Path(__file__).with_name("dev_tasks.txt")).read_text().splitlines()
            if line.strip() and not line.startswith("#")} if with_dev is False else set()
    rows = [row for row in benchmark_c.instances(prepared) if row["gold"] and row["instance_id"] not in seen]
    by_repo: dict[str, list[dict]] = {}
    for row in rows:
        by_repo.setdefault(row["repo"], []).append(row)
    chooser = random.Random(seed)
    for group in by_repo.values():
        chooser.shuffle(group)
    order = sorted(by_repo)
    chooser.shuffle(order)
    picked: list[dict] = []
    while len(picked) < count and any(by_repo.values()):
        for repo in order:
            if by_repo[repo] and len(picked) < count:
                picked.append(by_repo[repo].pop())
    return [{"id": row["instance_id"], "repo": row["repo"], "commit": row["base_commit"],
             "issue": row["problem_statement"], "gold": sorted(row["gold"])} for row in picked]


def clone_of(workdir: Path, repo: str) -> Path:
    return workdir / repo.replace("/", "__")


def command(arm: str, model: str, budget: float, prompt: str) -> list[str]:
    allowed = list(ALLOWED)
    args = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--max-budget-usd", str(budget)]
    if arm != "none":
        options = {"rung": arm[1]}
        if arm.endswith("n"):
            options["grep_pointer"] = "off"  # rung 1 as the pilot ran it: without the pointer in Grep and Glob
        allowed.append(ENGINE_TOOLS)
        args += ["--plugin-dir", str(PLUGIN),
                 "--settings", json.dumps({"pluginConfigs": {"aleph-zero@inline": {"options": options}}})]
    return [*args, "--allowedTools", *allowed]


def read_stream(text: str, gold: list[str], root: str | None = None) -> dict:
    """What one session did, from its event stream."""
    calls: list[dict] = []
    result: dict = {}
    answer = ""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    calls.append({"name": block["name"], "input": block.get("input", {}),
                                  "subagent": bool(event.get("parent_tool_use_id"))})
                elif block.get("type") == "text" and not event.get("parent_tool_use_id"):
                    answer = block["text"]
        elif kind == "result":
            result = event
    answer = result.get("result") or answer
    named = _named_files(answer, root)
    usage = result.get("modelUsage") or {}
    tokens = {key: sum(int(model.get(key, 0) or 0) for model in usage.values())
              for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")}
    names = [call["name"] for call in calls]
    searches = [_search_kind(call) for call in calls]
    first_gold = next((position for position, call in enumerate(calls) if _reads_gold(call, gold)), None)
    return {
        "ok": bool(result) and not result.get("is_error"),
        "ended": result.get("terminal_reason") or result.get("subtype") or "no result event",
        "tokens": sum(tokens.values()), "tokens_by_kind": tokens,
        "cost_usd": result.get("total_cost_usd"), "turns": result.get("num_turns"),
        "seconds": round((result.get("duration_ms") or 0) / 1000, 1),
        "tool_calls": len(calls), "tools": {name: names.count(name) for name in sorted(set(names))},
        "aleph_calls": searches.count("aleph"), "grep_calls": searches.count("grep"),
        "first_search": next((kind for kind in searches if kind), None),
        "calls_before_first_gold_read": first_gold,
        # Kept as the session reported them, so the sums above can be checked later.
        "usage": result.get("usage"), "model_usage": usage, "subagent_stats": result.get("subagent_stats"),
        "named": named, "file_hit": bool(set(named) & set(gold)),
        "file_recall": round(len(set(named) & set(gold)) / len(gold), 3) if gold else 0.0,
        "gave_up": not named,
    }


def _named_files(answer: str, root: str | None = None) -> list[str]:
    """The files of the last JSON object with a "files" list in the answer, as paths relative
    to the repository; [] if there is none. `root` is the session's worktree: an absolute
    path under it is made relative."""
    text = answer or ""
    decoder = json.JSONDecoder()
    found: list[str] | None = None
    for position in (index for index, character in enumerate(text) if character == "{"):
        try:
            value, _ = decoder.raw_decode(text, position)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("files"), list):
            found = [path for path in value["files"] if isinstance(path, str)]
    out = []
    for path in found or []:
        if root and path.startswith(root.rstrip("/") + "/"):
            path = path[len(root.rstrip("/")) + 1:]
        if path.startswith("./"):
            path = path[2:]
        out.append(path)
    return out[:5]


def _search_kind(call: dict) -> str | None:
    name = call["name"]
    if "aleph" in name:
        return "aleph"
    if name in ("Grep", "Glob"):
        return "grep"
    if name == "Bash" and re.search(r"(^|[\s;&|(])(grep|rg|find)\s|git\s+grep", str(call["input"].get("command", ""))):
        return "grep"
    return None


def _reads_gold(call: dict, gold: list[str]) -> bool:
    """A call that opens one of the files the fix edited: a Read, or the engine's outline of it."""
    if call["name"] != "Read" and "outline" not in call["name"]:
        return False
    target = str(call["input"].get("file_path") or call["input"].get("path") or "")
    return any(target == path or target.endswith("/" + path) for path in gold)


def run_one(workdir: Path, task: dict, arm: str, model: str, budget: float, seconds: int) -> dict:
    clone = clone_of(workdir, task["repo"]).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="az-ab-", dir="/tmp"))
    tree = scratch / "repo"
    git = lambda *args: subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True)  # noqa: E731
    made = git("worktree", "add", "--detach", "--force", str(tree), task["commit"])
    if made.returncode != 0:
        return {"task": task["id"], "arm": arm, "ok": False, "ended": f"worktree failed: {made.stderr[-200:]}"}
    try:
        if arm != "none":  # index before the clock starts: indexing is not what is being measured
            try:
                subprocess.run([str(ROOT / ".venv" / "bin" / "aleph"), "--cwd", str(tree), "index"], capture_output=True, timeout=1800)
            except subprocess.TimeoutExpired:
                return {"task": task["id"], "arm": arm, "model": model, "ok": False, "ended": "indexing took over 30 minutes"}
        started = time.monotonic()
        # Its own process group: on a timeout the session and everything it started (the engine's
        # server among them) are stopped together, not left behind.
        proc = subprocess.Popen(command(arm, model, budget, task["issue"] + QUESTION), cwd=tree, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            stream, _ = proc.communicate(timeout=seconds)
            ended_by = None
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                stream, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stream, _ = proc.communicate()
            ended_by = f"stopped after {seconds} s"
        record = read_stream(stream or "", task["gold"], str(tree.resolve()))
        record.update(task=task["id"], repo=task["repo"], arm=arm, model=model, gold=task["gold"],
                      wall_seconds=round(time.monotonic() - started, 1))
        if ended_by:
            record.update(ok=False, ended=ended_by)
        return record
    finally:
        git("worktree", "remove", "--force", str(tree))
        subprocess.run(["rm", "-rf", str(scratch)])


def bootstrap(ratios: list[float], rounds: int = 10_000, seed: int = 7) -> tuple[float, float, float]:
    """Geometric mean of paired ratios with a 95% interval, resampling tasks."""
    logs = [__import__("math").log(ratio) for ratio in ratios]
    chooser = random.Random(seed)
    means = sorted(statistics.fmean(chooser.choices(logs, k=len(logs))) for _ in range(rounds))
    exp = __import__("math").exp
    return exp(statistics.fmean(logs)), exp(means[int(rounds * 0.025)]), exp(means[int(rounds * 0.975)])


def report(records: list[dict], baseline: str) -> dict:
    """Arms against the baseline, paired by task. A task run more than once in an arm counts
    with the mean of its runs (so "thirty tasks twice" is thirty pairs, each steadier).
    Sessions that failed are left out and counted; tasks without a partner are named."""
    models = sorted({record.get("model") for record in records if record.get("model")})
    if len(models) > 1:
        raise ValueError(f"these records are from more than one model ({', '.join(models)}); report each by itself")
    runs: dict[str, dict[str, list[dict]]] = {}
    failed: dict[str, int] = {}
    for record in records:
        if record.get("ok"):
            runs.setdefault(record["arm"], {}).setdefault(record["task"], []).append(record)
        else:
            failed[record.get("arm", "?")] = failed.get(record.get("arm", "?"), 0) + 1
            runs.setdefault(record.get("arm", "?"), {})
    mean = lambda rows, key: statistics.fmean(float(row[key]) for row in rows)  # noqa: E731
    out: dict = {"baseline": baseline, "model": models[0] if models else None, "arms": {}}
    base = runs.get(baseline, {})
    for arm, done in sorted(runs.items()):
        rows = [row for group in done.values() for row in group]
        entry: dict = {"sessions": len(rows), "tasks": len(done), "failed_sessions": failed.get(arm, 0)}
        if rows:
            entry.update(
                tokens_median=int(statistics.median(mean(group, "tokens") for group in done.values())),
                tool_calls_median=statistics.median(mean(group, "tool_calls") for group in done.values()),
                cost_usd_total=round(sum(row.get("cost_usd") or 0 for row in rows), 3),
                file_hit_rate=round(statistics.fmean(mean(group, "file_hit") for group in done.values()), 3),
                gave_up=sum(row["gave_up"] for row in rows),
                aleph_first=sum(row.get("first_search") == "aleph" for row in rows),
            )
        if arm != baseline:
            shared = sorted(set(done) & set(base))
            entry["unpaired_tasks"] = sorted((set(done) | set(base)) - set(shared))
            if len(shared) >= 2:
                for measure in ("tokens", "tool_calls"):
                    ratios = [max(mean(done[task], measure), 1) / max(mean(base[task], measure), 1) for task in shared]
                    centre, low, high = bootstrap(ratios)
                    entry[f"{measure}_ratio"] = {"paired_tasks": len(shared), "geometric_mean": round(centre, 3),
                                                 "interval_95": [round(low, 3), round(high, 3)]}
                entry["file_hits_against_baseline"] = [round(sum(mean(done[task], "file_hit") for task in shared), 2),
                                                       round(sum(mean(base[task], "file_hit") for task in shared), 2)]
        out["arms"][arm] = entry
    return out


TRIES = 2    # a session that failed this often is not asked again: every try spends plan usage


def sessions_left(chosen: list[dict], arms: list[str], done: list[dict]) -> list[tuple[str, str]]:
    """(task, arm) for every session of the plan that has no successful record yet and has not failed
    `TRIES` times, in the plan's order."""
    have = {(record.get("task"), record.get("arm")) for record in done if record.get("ok")}
    failed: dict[tuple, int] = {}
    for record in done:
        if not record.get("ok"):
            failed[record.get("task"), record.get("arm")] = failed.get((record.get("task"), record.get("arm")), 0) + 1
    return [(task["id"], arm) for task in chosen for arm in arms
            if (task["id"], arm) not in have and failed.get((task["id"], arm), 0) < TRIES]


def foreign_tasks(chosen: list[dict], done: list[dict]) -> list[str]:
    """The tasks a results file holds that the plan does not: the file is of another run (another seed or count)."""
    planned = {task["id"] for task in chosen}
    return sorted({str(record.get("task")) for record in done} - planned)


def control_directory(target: Path) -> tuple[Path | None, str]:
    """Where the files that steer an A/B run are, from what was named: the directory of the results, or a results
    file in it. (None, why) when what is there is not an A/B run's: `run` takes the Benchmark C work directory,
    where a pause would hold the GPU job instead."""
    directory = target if target.is_dir() else target.parent      # a results file, also one its first session has yet to write
    job = longrun.job_of(directory)
    if job is None:
        return None, f"no A/B run has written its status in {directory}: name the directory of the results file (bench/data/results/ab)"
    if not job.startswith(JOB):
        return None, f"{directory} holds the status of another job ({job}), not of an A/B run: name the directory of the results file"
    return directory, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("tasks", "run", "report", "status", "pause", "resume", "stop"))
    parser.add_argument("target", type=Path, help="tasks/run: the Benchmark C work directory; report: a results file; "
                        "status/pause/resume/stop: the directory of the results file, or the file (not the work directory)")
    parser.add_argument("--count", type=int, default=2, help="how many tasks")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--arms", default="none,L1", help=f"comma-separated, from {','.join(ARMS)}")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="cost limit of one session")
    parser.add_argument("--seconds", type=int, default=900, help="wall-clock limit of one session")
    parser.add_argument("--max-sessions", type=int, default=8, help="refuse to start more sessions than this")
    parser.add_argument("--baseline", default="none")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--take-up", type=Path, metavar="FILE",
                        help="run: a results file of an interrupted run of the same command: only its missing sessions are run "
                             "(without this, running a command again adds a second run of every task)")
    args = parser.parse_args(argv)
    if args.action in ("status", "pause", "resume", "stop"):
        directory, why = control_directory(args.target)
        if directory is None:
            print(why, file=sys.stderr)
            return 2
        return longrun.main([args.action, str(directory)])
    if args.action == "report":
        records = [json.loads(line) for line in args.target.read_text().splitlines() if line.strip()]
        print(json.dumps(report(records, args.baseline), indent=2))
        return 0
    chosen = tasks(args.target, args.count, args.seed)
    if args.action == "tasks":
        for task in chosen:
            print(f"{task['id']:45} {task['repo']:32} gold {len(task['gold'])} issue {len(task['issue'])} chars")
        return 0
    arms = [arm for arm in args.arms.split(",") if arm]
    if any(arm not in ARMS for arm in arms):
        parser.error(f"arms are {', '.join(ARMS)}")
    out = args.take_up or args.out or ROOT / "bench" / "data" / "results" / "ab" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.model}.jsonl"
    if args.take_up and not out.is_file():
        parser.error(f"--take-up: no such results file as {out} (a run started without it would do, and spend, every session again)")
    if (out.parent / longrun.PAUSE).exists():
        parser.error(f"a pause is asked for in {out.parent} (left from before?): the run would start and wait at once; "
                     f"`python bench/tools/longrun.py resume {out.parent}` takes it away")
    before = [json.loads(line) for line in out.read_text().splitlines() if line.strip()] if args.take_up else []
    wanted = set(sessions_left(chosen, arms, before)) if args.take_up else None
    other = foreign_tasks(chosen, before)
    if other:
        parser.error(f"{out} holds {len(other)} tasks this command would not run ({', '.join(other[:3])}...): it is of another "
                     f"run. Give the --seed, --count and --arms it was started with")
    sessions = len(chosen) * len(arms) if wanted is None else len(wanted)     # what this command would start, and spend
    if sessions > args.max_sessions:
        parser.error(f"{sessions} sessions asked for, over --max-sessions {args.max_sessions}: this spends plan usage; "
                     f"raise the limit on purpose")
    if wanted is not None:
        given_up = len(chosen) * len(arms) - len(wanted) - len({(r.get("task"), r.get("arm")) for r in before if r.get("ok")})
        print(f"taking up {out}: {sessions} sessions to run" +
              (f"; {given_up} failed {TRIES} times and are not asked again" if given_up else ""), flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Steered from another shell through files beside the results (`run.py pause|resume|stop|status DIRECTORY`):
    # a session that has started is finished; the next one waits, or is not started.
    job = longrun.Control(out.parent, f"{JOB} into {out.name}", total=len(chosen))
    chooser = random.Random(args.seed)
    for number, task in enumerate(chosen, 1):
        order = list(arms)
        chooser.shuffle(order)  # neither arm always goes first
        for arm in order:
            if wanted is not None and (task["id"], arm) not in wanted:
                continue        # this session is in the file already
            job()
            if job.stop_wanted():
                job.finish()
                print(f"stopped on request. To go on: the same command with --take-up {out}", flush=True)
                return 3
            job.unit(number, f"{task['id']} {arm}")
            record = run_one(args.target, task, arm, args.model, args.budget_usd, args.seconds)
            with out.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            print(f"{number:3}/{len(chosen)} {task['id']:40} {arm:5} ok={record.get('ok')} tokens={record.get('tokens')} "
                  f"calls={record.get('tool_calls')} hit={record.get('file_hit')} cost={record.get('cost_usd')} "
                  f"first={record.get('first_search')} {record.get('ended')}", flush=True)
    job.finish(done=True)
    records = [json.loads(line) for line in out.read_text().splitlines()]
    print(json.dumps(report(records, args.baseline), indent=2))
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
