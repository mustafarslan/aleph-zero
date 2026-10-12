"""How much of what a repository states about its architecture does `aleph facts` read, and how
much of what it reads is so? The protocol is fixed in the M8 plan, before any fact was extracted.

Usage:
  uv run python bench/eval/facts_grade.py CLONE --out FILE [--openapi PATH] [--sample 50]

CLONE is a scratch clone: Aleph's index is written under its `.git`, never under the owner's.
Each kind is graded against what the repository itself supplies:

- routes of the documented API: the operations of an OpenAPI document (`--openapi`, a path inside
  the clone), same method and same path; the share found is the recall. The document covers what
  it covers, so it says nothing about precision;
- every other oracle is a search of the source that does not use the readers: route decorators,
  task decorators, `model` blocks, `create_table` calls, the names of the example environment
  files, `os.Getenv` calls, `service` and `rpc` lines. They can show a miss or an invention, not
  much more, and are reported as counts, by file and line where both sides have one;
- precision: `--sample` facts of a kind drawn at random (seed 20261012) are written out with the
  source lines they point at, to be read by a person. That reading is not independent of the
  author of the readers, and the result says so.

Names stay in FILE (git-ignored); what is printed is counts.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from aleph_zero.engine import Engine

SEED = 20261012
CODE = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go", ".rs", ".java")
METHODS = ("get", "post", "put", "delete", "patch", "head", "options")
ROUTE_DECORATOR = re.compile(r"^\s*@\w+\.(get|post|put|delete|patch|head|options|route|api_route|websocket)\(")
TASK_DECORATOR = re.compile(r"^\s*@(?:\w+\.)*(?:shared_task|task)\b")
PRISMA_MODEL = re.compile(r"^model\s+(\w+)\s*\{")
CREATE_TABLE = re.compile(r"\bop\.create_table\(")
EXAMPLE_FILE = re.compile(r"(^|\.)env\.(example|sample|template|dist)$")
EXAMPLE_NAME = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=")
GO_ENV = re.compile(r"\bos\.(?:Getenv|LookupEnv)\(\s*\"")
PROTO_SERVICE = re.compile(r"^\s*service\s+(\w+)")
PROTO_RPC = re.compile(r"^\s*rpc\s+(\w+)")


def tracked(root: Path) -> list[str]:
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return sorted(path for path in listed.decode("utf-8", "surrogateescape").split("\0") if path)


def lines_of(root: Path, path: str) -> list[str]:
    try:
        return (root / path).read_text("utf-8", "replace").split("\n")
    except (OSError, IsADirectoryError):
        return []


def extracted(engine: Engine) -> dict[str, list[dict]]:
    """Every fact Aleph reads, by kind, once the whole view is indexed and read."""
    deadline = time.monotonic() + 1800
    while True:
        overview = engine.facts()
        if not overview.pending and not overview.unread:
            break
        if time.monotonic() > deadline:
            raise SystemExit(f"not read within the time allowed: {overview.pending} files to index, {overview.unread} to read")
    out = {}
    for kind, count in overview.counts.items():
        answer = engine.facts(kind, k=count + 1)
        assert not answer.truncated and len(answer.facts) == count
        out[kind] = [{"name": f.name, "path": f.path, "line": f.line, "detail": f.detail} for f in answer.facts]
    return out


def searched(root: Path, files: list[str]) -> dict:
    """What a search of the source finds, without the readers."""
    out = {"route_decorators": [], "task_decorators": [], "prisma_models": [], "create_table": [],
           "example_names": {}, "go_env": [], "proto_services": [], "proto_rpcs": []}
    for path in files:
        name = path.rsplit("/", 1)[-1].lower()
        wanted = name.endswith((".py", ".prisma", ".go", ".proto")) or EXAMPLE_FILE.search(name)
        for number, line in enumerate(lines_of(root, path) if wanted else [], 1):
            if name.endswith(".py"):
                if ROUTE_DECORATOR.match(line):
                    out["route_decorators"].append([path, number])
                if TASK_DECORATOR.match(line):
                    out["task_decorators"].append([path, number])
                if CREATE_TABLE.search(line):
                    out["create_table"].append([path, number])
            elif name.endswith(".prisma"):
                if PRISMA_MODEL.match(line):
                    out["prisma_models"].append([path, number])
            elif name.endswith(".go"):
                out["go_env"] += [[path, number]] * len(GO_ENV.findall(line))
            elif name.endswith(".proto"):
                if PROTO_SERVICE.match(line):
                    out["proto_services"].append([path, number])
                if PROTO_RPC.match(line):
                    out["proto_rpcs"].append([path, number])
            else:
                declared = EXAMPLE_NAME.match(line)
                if declared:
                    out["example_names"].setdefault(declared.group(1), [path, number])
    return out


def by_place(facts: list[dict]) -> Counter:
    return Counter((fact["path"], fact["line"]) for fact in facts)


def against(found: list[dict], oracle: list[list]) -> dict:
    """Facts and search results compared by file and line."""
    ours, theirs = by_place(found), Counter(map(tuple, oracle))
    return {"searched": sum(theirs.values()), "extracted": sum(ours.values()),
            "both": sum((ours & theirs).values()),
            "searched_only": sorted(f"{path}:{line}" for path, line in (theirs - ours).elements()),
            "extracted_only": sorted(f"{path}:{line}" for path, line in (ours - theirs).elements())}


def operations(document: dict) -> list[str]:
    return sorted(f"{method.upper()} {path}" for path, item in document.get("paths", {}).items()
                  for method in item if method in METHODS)


def nameless(route: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", route)


def sample(root: Path, facts: list[dict], size: int) -> list[dict]:
    chosen = random.Random(SEED).sample(facts, min(size, len(facts)))
    out = []
    for fact in sorted(chosen, key=lambda each: (each["path"], each["line"])):
        text = lines_of(root, fact["path"])
        out.append({**fact, "source": [line.strip()[:160] for line in text[fact["line"] - 1:fact["line"] + 2]]})
    return out


def grade(root: Path, openapi: str | None, size: int) -> tuple[dict, dict]:
    files = tracked(root)
    engine = Engine(root)
    try:
        facts = extracted(engine)
    finally:
        engine.close()
    found = searched(root, files)
    routes = facts.get("route", [])
    counts: dict = {"files": len(files), "extracted": {kind: len(listed) for kind, listed in facts.items()},
                    "routes_by_framework": dict(Counter(route["detail"] for route in routes))}
    detail: dict = {"facts": facts, "samples": {}}

    if openapi:
        documented = operations(json.loads((root / openapi).read_text()))
        ours = {route["name"] for route in routes}
        loose = {nameless(name) for name in ours}
        missed = [operation for operation in documented if operation not in ours]
        counts["documented_routes"] = {
            "operations": len(documented), "found": len(documented) - len(missed),
            "recall": round((len(documented) - len(missed)) / max(len(documented), 1), 3),
            "found_when_parameter_names_are_disregarded": sum(nameless(operation) in loose for operation in documented)}
        detail["documented_missed"] = missed

    python_routes = [route for route in routes if route["path"].endswith(".py")]
    # One decorator with several methods is several routes at one line: compared by line, once.
    places = [dict(path=path, line=line) for path, line in sorted({(r["path"], r["line"]) for r in python_routes})]
    for key, ours, theirs in (
            ("python_route_decorators", places, found["route_decorators"]),
            ("task_decorators", facts.get("task", []), found["task_decorators"]),
            ("prisma_models", [f for f in facts.get("model", []) if f["detail"] == "prisma"], found["prisma_models"]),
            ("alembic_tables", [f for f in facts.get("table", []) if f["detail"] == "alembic"], found["create_table"]),
            ("go_environment_reads", [f for f in facts.get("env", []) if f["path"].endswith(".go")], found["go_env"]),
            ("grpc_services", facts.get("service", []), found["proto_services"]),
            ("grpc_methods", facts.get("rpc", []), found["proto_rpcs"])):
        compared = against(ours, theirs)
        detail[key] = {name: compared.pop(name) for name in ("searched_only", "extracted_only")}
        counts[key] = compared

    environment = facts.get("env", [])
    read = {fact["name"] for fact in environment if fact["detail"] != "declared"}
    declared = set(found["example_names"])
    unread = sorted(declared - read)
    source = "\n".join("\n".join(lines_of(root, path)) for path in files
                       if path.lower().endswith(CODE) and not EXAMPLE_FILE.search(path.rsplit("/", 1)[-1].lower()))
    words = set(re.findall(r"[A-Z][A-Z0-9_]{2,}", source))
    in_source = [name for name in unread if name in words]
    counts["environment"] = {
        "names_in_example_files": len(declared), "extracted_as_declared": len({f["name"] for f in environment if f["detail"] == "declared"}),
        "of_them_read_in_code": len(declared & read), "recall": round(len(declared & read) / max(len(declared), 1), 3),
        "not_read_but_the_name_occurs_in_source": len(in_source), "not_read_and_the_name_occurs_nowhere": len(unread) - len(in_source),
        "names_read_in_code": len(read), "read_but_in_no_example_file": len(read - declared)}
    detail["environment"] = {"occurs_in_source": in_source, "occurs_nowhere": [name for name in unread if name not in in_source]}

    for key, some in (("express_routes", [r for r in routes if r["detail"] == "express"]),
                      ("python_routes", python_routes),
                      ("go_routes", [r for r in routes if r["detail"] == "go"]),
                      ("environment_reads", [f for f in environment if f["detail"] != "declared"])):
        detail["samples"][key] = sample(root, some, size)
        counts.setdefault("sampled", {})[key] = len(detail["samples"][key])
    return counts, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("clone", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--openapi", help="an OpenAPI document inside the clone")
    parser.add_argument("--sample", type=int, default=50)
    args = parser.parse_args(argv)
    counts, detail = grade(args.clone.resolve(), args.openapi, args.sample)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"counts": counts, **detail}, indent=1))
    print(json.dumps(counts, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
