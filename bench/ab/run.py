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
L0 is the engine's tools without steering). Arms are interleaved per task, so a task's
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
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
ARMS = ("none", "L0", "L1", "L2", "L3")


def tasks(workdir: Path, count: int, seed: int = 11) -> list[dict]:
    """`count` Loc-Bench issues from the repositories already prepared for Benchmark C,
    spread over repositories, the same ones for a given seed."""
    sys.path.insert(0, str(ROOT / "bench" / "eval"))
    import benchmark_c

    prepared = {path.name.replace("__", "/", 1) for path in workdir.iterdir() if (path / ".git").exists() or path.is_symlink()}
    rows = [row for row in benchmark_c.instances(prepared) if row["gold"]]
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
        rung = arm[1]
        allowed.append(ENGINE_TOOLS)
        args += ["--plugin-dir", str(PLUGIN),
                 "--settings", json.dumps({"pluginConfigs": {"aleph-zero@inline": {"options": {"rung": rung}}}})]
    return [*args, "--allowedTools", *allowed]


def read_stream(text: str, gold: list[str]) -> dict:
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
    named = _named_files(answer)
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
        "named": named, "file_hit": bool(set(named) & set(gold)),
        "file_recall": round(len(set(named) & set(gold)) / len(gold), 3) if gold else 0.0,
        "gave_up": not named,
    }


def _named_files(answer: str) -> list[str]:
    """The files of the JSON line the session was asked to end with; [] if it did not."""
    for found in reversed(re.findall(r"\{[^{}]*\"files\"[^{}]*\}", answer or "")):
        try:
            files = json.loads(found).get("files")
        except ValueError:
            continue
        if isinstance(files, list):
            return [str(path).lstrip("./") for path in files if isinstance(path, str)][:5]
    return []


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
            subprocess.run([str(ROOT / ".venv" / "bin" / "aleph"), "--cwd", str(tree), "index"], capture_output=True, timeout=1800)
        started = time.monotonic()
        try:
            proc = subprocess.run(command(arm, model, budget, task["issue"] + QUESTION), cwd=tree, capture_output=True,
                                  text=True, timeout=seconds, stdin=subprocess.DEVNULL)
            stream, ended_by = proc.stdout, None
        except subprocess.TimeoutExpired as exc:
            stream = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            ended_by = f"stopped after {seconds} s"
        record = read_stream(stream, task["gold"])
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
    by_arm: dict[str, dict[str, dict]] = {}
    for record in records:
        if record.get("ok"):
            by_arm.setdefault(record["arm"], {})[record["task"]] = record  # the last run of a task wins
    out: dict = {"baseline": baseline, "arms": {}}
    base = by_arm.get(baseline, {})
    for arm, done in sorted(by_arm.items()):
        rows = list(done.values())
        entry = {
            "sessions": len(rows),
            "tokens_median": int(statistics.median(row["tokens"] for row in rows)),
            "tool_calls_median": statistics.median(row["tool_calls"] for row in rows),
            "cost_usd_total": round(sum(row.get("cost_usd") or 0 for row in rows), 3),
            "file_hit_rate": round(sum(row["file_hit"] for row in rows) / len(rows), 3),
            "gave_up": sum(row["gave_up"] for row in rows),
            "aleph_first": sum(row.get("first_search") == "aleph" for row in rows),
        }
        shared = sorted(set(done) & set(base))
        if arm != baseline and len(shared) >= 2:
            for measure in ("tokens", "tool_calls"):
                ratios = [max(done[task][measure], 1) / max(base[task][measure], 1) for task in shared]
                mean, low, high = bootstrap(ratios)
                entry[f"{measure}_ratio"] = {"paired_tasks": len(shared), "geometric_mean": round(mean, 3),
                                             "interval_95": [round(low, 3), round(high, 3)]}
            entry["file_hits_against_baseline"] = [sum(done[task]["file_hit"] for task in shared),
                                                   sum(base[task]["file_hit"] for task in shared)]
        out["arms"][arm] = entry
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("tasks", "run", "report"))
    parser.add_argument("target", type=Path, help="tasks/run: the Benchmark C work directory; report: a results file")
    parser.add_argument("--count", type=int, default=2, help="how many tasks")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--arms", default="none,L1", help=f"comma-separated, from {','.join(ARMS)}")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="cost limit of one session")
    parser.add_argument("--seconds", type=int, default=900, help="wall-clock limit of one session")
    parser.add_argument("--max-sessions", type=int, default=8, help="refuse to start more sessions than this")
    parser.add_argument("--baseline", default="none")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
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
    sessions = len(chosen) * len(arms)
    if sessions > args.max_sessions:
        parser.error(f"{sessions} sessions asked for, over --max-sessions {args.max_sessions}: this spends plan usage; "
                     f"raise the limit on purpose")
    out = args.out or ROOT / "bench" / "data" / "results" / "ab" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.model}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    chooser = random.Random(args.seed)
    for number, task in enumerate(chosen, 1):
        order = list(arms)
        chooser.shuffle(order)  # neither arm always goes first
        for arm in order:
            record = run_one(args.target, task, arm, args.model, args.budget_usd, args.seconds)
            with out.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            print(f"{number:3}/{len(chosen)} {task['id']:40} {arm:5} ok={record.get('ok')} tokens={record.get('tokens')} "
                  f"calls={record.get('tool_calls')} hit={record.get('file_hit')} cost={record.get('cost_usd')} "
                  f"first={record.get('first_search')} {record.get('ended')}", flush=True)
    records = [json.loads(line) for line in out.read_text().splitlines()]
    print(json.dumps(report(records, args.baseline), indent=2))
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
