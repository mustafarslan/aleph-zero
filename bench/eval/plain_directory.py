"""What does searching a directory that is not a git repository cost?

Usage:
  uv run python bench/eval/plain_directory.py REPOSITORY SCRATCH_DIR [--files 5000] [--out FILE]

The protocol is fixed in the M10b plan before any figure. Files of REPOSITORY (the first FILES
that git tracks and the rule would search) are copied into SCRATCH_DIR with no `.git`; the
cache directory is set inside SCRATCH_DIR, so the machine's own is not touched. Timed, median
of 9 where it repeats:
- a first search from a new process (everything is hashed and indexed);
- a search from a new process with nothing changed, and after one file was edited;
- the same two in an engine that is already open, and how many files each had to hash;
- the walk alone over a directory of 20,000 small files (the limit), with its memory warm.
Accepted if an unchanged directory is answered from an open engine within 150 ms, a new process
hashes no file whose size and times are unchanged, and the walk at the limit takes under 100 ms.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


def timed(run) -> float:
    started = time.perf_counter()
    run()
    return (time.perf_counter() - started) * 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repository", type=Path)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    scratch = args.scratch.resolve()
    shutil.rmtree(scratch, ignore_errors=True)
    project, limit = scratch / "project", scratch / "at-the-limit"
    os.environ["XDG_CACHE_HOME"] = str(scratch / "cache")
    from aleph_zero import filters, gitio, plain
    from aleph_zero.engine import Engine

    tracked = subprocess.run(["git", "-C", str(args.repository), "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
    copied = 0
    for path in tracked:
        source = args.repository / path
        hidden = any(part.startswith(".") or part in plain.LEFT_OUT for part in path.split("/"))
        if not path or hidden or source.is_symlink() or not source.is_file() or source.stat().st_size > filters.MAX_BYTES:
            continue
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, project / path)
        copied += 1
        if copied == args.files:
            break
    program = [str(Path(sys.executable).parent / "aleph"), "--cwd", str(project), "search"]
    cold = lambda query: timed(lambda: subprocess.run([*program, query], capture_output=True, check=True))   # noqa: E731
    target = next(path for path in sorted(project.rglob("*.py")) if path.stat().st_size > 2000)
    original = target.read_bytes()
    report: dict = {"files": copied, "bytes": sum(path.stat().st_size for path in project.rglob("*") if path.is_file())}
    report["first_search_cold_ms"] = round(cold("scheduler heartbeat"))

    def edit(number: int) -> str:
        name = f"zq_plain_probe_{number}"
        target.write_bytes(original + f"\n\ndef {name}():\n    return {number}\n".encode())
        return name

    report["unchanged_cold_ms"] = round(statistics.median(cold("scheduler heartbeat") for _ in range(9)), 1)
    report["after_an_edit_cold_ms"] = round(statistics.median(cold(edit(number)) for number in range(9)), 1)

    hashed: list[int] = []
    real = gitio.hash_blob
    gitio.hash_blob = lambda data, fmt="sha1": hashed.append(len(data)) or real(data, fmt)
    engine = Engine(project)
    engine.search("warm up")
    hashed.clear()
    plain.forget()                      # as a new process would find it: only the file beside the index remembers
    engine.search("scheduler heartbeat")
    report["files_hashed_by_a_new_process_with_nothing_changed"] = len(hashed)
    report["unchanged_warm_ms"] = round(statistics.median(timed(lambda: engine.search("scheduler heartbeat")) for _ in range(9)), 1)
    hashed.clear()
    took = []
    for number in range(9, 18):
        name = edit(number)
        took.append(timed(lambda: engine.search(name)))
        assert any(hit.path == str(target.relative_to(project)) for hit in engine.search(name).hits)
    report["after_an_edit_warm_ms"] = round(statistics.median(took), 1)
    report["files_hashed_per_edit"] = round(len(hashed) / 9, 1)
    engine.close()
    target.write_bytes(original)

    for number in range(plain.FILE_LIMIT):
        directory = limit / f"d{number // 200:03d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"f{number}.py").write_text(f"x = {number}\n")
    plain.read(limit)
    report["walk_at_the_limit_ms"] = round(statistics.median(timed(lambda: plain.read(limit)) for _ in range(9)), 1)
    report["walk_of_the_project_ms"] = round(statistics.median(timed(lambda: plain.read(project)) for _ in range(9)), 1)
    shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps(report, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
