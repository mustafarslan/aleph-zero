"""Which language the product would call each benchmark question's repository, written into the result that scored it.

  uv run --group bench python bench/eval/population.py bench/data/benchc-typescript bench/data/results/benchmark_c_typescript.json --set others --language typescript

A verdict is applied by a repository's main language (`Index.languages()`: the language most of its indexed source
is written in), so that is what a language's questions are chosen by: `verdict.py --main-language`. A question
set's own label says what a fix touched, which is another thing: tailwindcss is JavaScript to the last file and one
set files it under TypeScript. Read at the question's own commit, since a repository changes its language over the
years. Nothing is embedded; a commit that was never indexed is indexed (words only) to be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402

from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402


def main_language(workdir: Path, question: dict) -> tuple[str | None, dict[str, float]]:
    """The main language of the question's repository at its commit, and the shares it was read from."""
    view = bench.view_at(bench.clone_of(workdir, question["repo"]), question["base_commit"])
    index = Index(open_db(index_file(view.common_dir)))
    try:
        while index.sync(view).pending:
            pass
        shares = index.languages()
    finally:
        index.conn.close()
    return next(iter(shares), None), {name: round(share, 3) for name, share in list(shares.items())[:3]}


def write(workdir: Path, result: Path, questions: list[dict], say=lambda text: None) -> dict[str, dict[str, int]]:
    """`main_language` and `language_share` into every scored question of `result`; per repository, how many of each."""
    by_name = {question["instance_id"]: question for question in questions}
    found = json.loads(result.read_text())
    missing = [row["instance"] for row in found["instances"] if row["instance"] not in by_name]
    if missing:
        raise SystemExit(f"{len(missing)} scored questions are not in the set given ({', '.join(missing[:3])}): the wrong --set or --language")
    counted: dict[str, dict[str, int]] = {}
    for number, row in enumerate(found["instances"], 1):
        row["main_language"], row["language_share"] = main_language(workdir, by_name[row["instance"]])
        per = counted.setdefault(row["repo"], {})
        per[str(row["main_language"])] = per.get(str(row["main_language"]), 0) + 1
        if number % 50 == 0:
            say(f"{number} of {len(found['instances'])}")
    scratch = result.with_name(f"{result.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(found, indent=1) + "\n")
    os.replace(scratch, result)
    return counted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    args = parser.parse_args(argv)
    counted = write(args.workdir.resolve(), args.result, bench.instances(None, args.set, args.language, None),
                    lambda text: print(text, file=sys.stderr, flush=True))
    for repo, per in sorted(counted.items()):
        print(f"{repo}: " + ", ".join(f"{count} {language}" for language, count in sorted(per.items(), key=lambda item: -item[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
