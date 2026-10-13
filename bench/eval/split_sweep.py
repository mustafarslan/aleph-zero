"""The split threshold of the chunker (ADR-001), swept with words and names alone: the part that needs no GPU.

  uv run --group bench python bench/eval/split_sweep.py bench/data/benchc --out bench/data/results/split_sweep_python.json
  uv run --group bench python bench/eval/split_sweep.py bench/data/benchc-cpp --set others --language cpp --out ...

Every setting chunks every question's commit again, into a scratch index of its own that is deleted when its
repository is done. The prepared indexes beside the clones are read by every recorded verdict and are never opened.

What this can decide (the rule is in docs/DEFERRED.md, entry 1c, written before the first run): it is a guard on
the arm Python ships with, not the choice of the threshold. The threshold exists for vectors; with words alone the
pieces of a definition are merged back into one result. A setting is dropped if its file recall@10 on first lines
is lower than at the shipped setting with a paired interval clear of zero. Beside it: the chunks each setting makes,
which is what embedding it would cost, and how many files ten results name.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from temps import Guard, TooHot  # noqa: E402  (bench/tools is on the path once benchmark_c is imported)

from aleph_zero import gitio  # noqa: E402
from aleph_zero.chunking import code as chunker  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.search import search  # noqa: E402
from aleph_zero.store import open_db  # noqa: E402

SETTINGS = (96, 128, 192, 256, 384)
SHIPPED = chunker.SPLIT_TOKENS
FORMS = ("title", "full")
MEASURES = ("recall@10", "recall@1", "mrr")


@contextmanager
def split_at(tokens: int):
    """The chunker cuts at `tokens` inside the block. The module's own name is set: the copy that the package
    re-exports is not what the chunker reads."""
    before = chunker.SPLIT_TOKENS
    chunker.SPLIT_TOKENS = tokens
    try:
        yield
    finally:
        chunker.SPLIT_TOKENS = before


def guard(shipped: list[float], other: list[float]) -> dict:
    """`other` against the shipped setting on the same questions. Dropped only when lower with an interval clear of zero."""
    mean, low, high = bench.paired_bootstrap(other, shipped)
    return {"difference": round(mean, 3), "low": round(low, 3), "high": round(high, 3), "dropped": high < 0}


def one_setting(workdir: Path, rows: list[dict], tokens: int, scratch: Path, cool, say) -> dict:
    """Every question asked of its commit chunked at `tokens`. One scratch index a repository: a later commit of it
    adds only what changed, as in an index that is used."""
    by_repo: dict[str, list[dict]] = {}
    for row in rows:
        by_repo.setdefault(row["repo"], []).append(row)
    scored: dict[str, dict] = {}
    chunks = 0
    with split_at(tokens):
        for number, (repo, some) in enumerate(sorted(by_repo.items()), 1):
            cool()
            started = time.monotonic()
            home = Path(tempfile.mkdtemp(prefix=f"split-{tokens}-", dir=scratch))
            index = Index(open_db(home / "index.db"))
            try:
                # All of the repository's commits first, then the questions: the text index's statistics are over
                # everything it holds, and the prepared indexes held every commit when the benchmark was scored.
                views = {}
                for row in some:
                    try:
                        views[row["instance_id"]] = bench.view_at(bench.clone_of(workdir, repo), row["base_commit"])
                    except gitio.GitError:
                        continue
                    while index.sync(views[row["instance_id"]], budget_seconds=600).pending:
                        pass
                for row in some:
                    view = views.get(row["instance_id"])
                    if view is None:
                        continue
                    while index.sync(view, budget_seconds=600).pending:
                        pass
                    forms = bench.question_forms(index, row["problem_statement"])
                    entry: dict = {"chunks": index.conn.execute("SELECT count(*) FROM temp.view_chunks").fetchall()[0][0]}
                    for form in FORMS:
                        files = bench.first_files([hit.path for hit in search(index, view, forms[form], k=10)])
                        entry[form] = {**bench.score(row["gold"], files), "files": len(files)}
                    scored[row["instance_id"]] = entry
                    chunks += entry["chunks"]
            finally:
                index.conn.close()
                shutil.rmtree(home, ignore_errors=True)
            say(f"{tokens:4} tokens  {number:3}/{len(by_repo)} {repo:40} {len(some):3} questions {time.monotonic() - started:6.1f} s")
    return {"questions": scored, "chunks": chunks}


def summarise(by_setting: dict[int, dict]) -> dict:
    """Per setting: the means over the questions every setting scored, the chunks made, and the guard against the shipped setting."""
    shared = sorted(set.intersection(*(set(entry["questions"]) for entry in by_setting.values())))
    out: dict = {"questions": len(shared), "shipped": SHIPPED, "settings": {}}
    if not shared:
        return out
    base = by_setting.get(SHIPPED)
    for tokens, entry in sorted(by_setting.items()):
        block: dict = {"chunks": sum(entry["questions"][name]["chunks"] for name in shared)}
        if base is not None:
            block["chunks_against_shipped"] = round(block["chunks"] / max(sum(base["questions"][name]["chunks"] for name in shared), 1), 3)
        for form in FORMS:
            block[form] = {measure: round(statistics.fmean(entry["questions"][name][form][measure] for name in shared), 3) for measure in MEASURES}
            block[form]["files"] = round(statistics.fmean(entry["questions"][name][form]["files"] for name in shared), 1)
            if base is not None and tokens != SHIPPED:
                block[form]["against_shipped"] = guard([base["questions"][name][form]["recall@10"] for name in shared],
                                                       [entry["questions"][name][form]["recall@10"] for name in shared])
        out["settings"][tokens] = block
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, help="a Benchmark C work directory whose clones are fetched")
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    parser.add_argument("--per-repo", type=int)
    parser.add_argument("--settings", default=",".join(str(tokens) for tokens in SETTINGS))
    parser.add_argument("--only", type=Path, help="a benchmark result file: only the questions it scored are asked")
    parser.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()), help="where the scratch indexes go")
    parser.add_argument("--temp-limit", type=float, default=95.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    say = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731
    rows = bench.instances(None, args.set, args.language, args.per_repo)
    if args.only:
        wanted = {entry["instance"] for entry in json.loads(args.only.read_text())["instances"]}
        rows = [row for row in rows if row["instance_id"] in wanted]
    cool = Guard(limit=args.temp_limit, resume=args.temp_limit - 15, say=say)
    args.scratch.mkdir(parents=True, exist_ok=True)
    by_setting: dict[int, dict] = {}
    try:
        for tokens in (int(part) for part in args.settings.split(",")):
            by_setting[tokens] = one_setting(args.workdir, rows, tokens, args.scratch, cool, say)
            if args.out:    # kept after every setting: an hour's work is not lost to the next one
                args.out.write_text(json.dumps({"summary": summarise(by_setting), "by_setting": by_setting}, indent=1) + "\n")
    except TooHot as hot:
        raise SystemExit(f"given up for heat: {hot}. The settings that were finished are in the output file.") from hot
    print(json.dumps(summarise(by_setting), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
