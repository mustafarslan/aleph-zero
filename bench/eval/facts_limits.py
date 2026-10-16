"""The knowledge graph's limits, measured (DEFERRED entry 0-graph, parts 7 to 9; rules written before any run).

coverage: for each OpenAPI document of part 2, the share of its operations with a route fact that agrees with it, as
          `facts(mentioned=…)` matches them (read from their ends, the shorter path whole, one word at least; the method
          too), by framework.
handlers: the share of route facts (outside tests and documentation) whose handler is given by name, and of those, the
          share whose definition is found.
speed:    facts(mentioned=…) warm, p50 and p95 of 20 look-ups, on each repository as it is.

    facts_limits.py coverage|handlers|speed SRC_DIR [--out FILE]
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from facts_questions import METHODS, PROJECTS  # noqa: E402

from aleph_zero import archfacts  # noqa: E402
from aleph_zero.engine import Engine  # noqa: E402


def _facts(engine: Engine, root: Path) -> list[archfacts.Found]:
    engine.facts(cwd=root, prepare_seconds=900)
    index, view, _, _ = engine._ready(root, time.monotonic(), None)
    return archfacts.load(index), index, view


def coverage(src: Path) -> dict:
    out = {}
    for repo, (_, spec, _) in PROJECTS.items():
        if not spec:
            continue
        root = src / repo.replace("/", "_")
        text = (root / spec).read_text()
        document = json.loads(text) if spec.endswith(".json") else yaml.safe_load(text)
        operations = [(method.upper(), path) for path, item in (document.get("paths") or {}).items() for method in (item or {}) if method in METHODS]
        engine = Engine(root, dense=False)
        found, _, _ = _facts(engine, root)
        routes = [each for each in found if each.kind == "route" and not archfacts.NEVER_AN_ANSWER.search(each.path)]
        frameworks = collections.Counter(each.detail.split(" ")[0] for each in routes)
        matched = 0
        for method, path in operations:
            asked = archfacts._segments(path)
            for route in routes:
                stated_method, _, stated_path = route.name.partition(" ")
                stated = archfacts._segments(stated_path)
                if stated_method in (method, "ANY") and archfacts._route_match(asked, stated) > 0:
                    matched += 1
                    break
        out[repo] = {"operations": len(operations), "with_a_route": matched, "share": round(matched / max(1, len(operations)), 3),
                     "route_facts": len(routes), "frameworks": dict(frameworks)}
        engine.close()
    return out


def handlers(src: Path) -> dict:
    out = {}
    for repo in PROJECTS:
        root = src / repo.replace("/", "_")
        engine = Engine(root, dense=False)
        found, index, view = _facts(engine, root)
        routes = [each for each in found if each.kind == "route" and not archfacts.NEVER_AN_ANSWER.search(each.path)]
        named = [each for each in routes if " " in each.detail]
        resolved = {each.name for each in archfacts.handlers(index, view, named)}
        out[repo] = {"routes": len(routes), "named": len(named), "resolved": sum(1 for each in named if each.name in resolved),
                     "decorated": sum(1 for each in routes if each.detail in ("fastapi", "flask", "spring"))}
        engine.close()
    return out


def speed(src: Path) -> dict:
    out = {}
    texts = ["DELETE /client/{id} fails", "GET /api/users/7 returns 500", "DATABASE_URL is ignored", "POST /v1/jobs and REDIS_URL"]
    for repo in PROJECTS:
        root = src / repo.replace("/", "_")
        engine = Engine(root, dense=False)
        engine.facts(cwd=root, prepare_seconds=900)
        times = []
        for n in range(20):
            started = time.monotonic()
            engine.facts(mentioned=texts[n % len(texts)], cwd=root, timeout_ms=2000)
            times.append((time.monotonic() - started) * 1000)
        times.sort()
        out[repo] = {"p50_ms": round(statistics.median(times)), "p95_ms": round(times[int(len(times) * 0.95) - 1])}
        engine.close()
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["coverage", "handlers", "speed"])
    parser.add_argument("src", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = {"coverage": coverage, "handlers": handlers, "speed": speed}[args.action](args.src.resolve())
    text = json.dumps(result, indent=1)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
