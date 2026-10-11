"""Spike B: how many chunks of a changed file were already seen, AST versus 50-line windows."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import tree_sitter_language_pack as pack

RESULTS = Path(__file__).resolve().parents[1] / "data" / "results"
SPLIT_CLASS_LINES = 80
_PARSER = None


def _parser():
    global _PARSER
    if _PARSER is None:
        _PARSER = pack.get_parser("python")
    return _PARSER


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip("\n")


def ast_chunks(source: bytes) -> list[str]:
    chunks: list[str] = []

    def text(start: int, end: int) -> str:
        return _normalize(source[start:end].decode("utf-8", "replace"))

    def visit(node) -> None:
        loose: list[str] = []

        def flush() -> None:
            if loose:
                chunks.append("\n".join(loose))
                loose.clear()

        for child in node.named_children:
            inner = child
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition") or child
            if inner.type == "function_definition":
                flush()
                chunks.append(text(child.start_byte, child.end_byte))
            elif inner.type == "class_definition":
                flush()
                body = inner.child_by_field_name("body")
                lines = child.end_point.row - child.start_point.row + 1
                if body is None or lines <= SPLIT_CLASS_LINES:
                    chunks.append(text(child.start_byte, child.end_byte))
                else:
                    chunks.append(text(child.start_byte, body.start_byte))
                    visit(body)
            elif child.type != "comment":
                loose.append(text(child.start_byte, child.end_byte))
        flush()

    visit(_parser().parse(source).root_node)
    return [chunk for chunk in chunks if chunk]


def window_chunks(source: bytes, lines: int = 50) -> list[str]:
    rows = source.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    return [c for c in (_normalize("\n".join(rows[i:i + lines])) for i in range(0, len(rows), lines)) if c]


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def replay(repo: Path, commits: int) -> dict:
    revs = _git(repo, "rev-list", "--first-parent", "-n", str(commits), "HEAD").decode().split()[::-1]
    seen_blobs: set[str] = set()
    seen = {"ast": set(), "window": set()}
    totals = {"ast": [0, 0], "window": [0, 0]}  # [chunks in changed files, never seen before]
    for index, rev in enumerate(revs):
        entries = _git(repo, "ls-tree", "-r", "-z", rev).decode("utf-8", "replace").split("\0")
        for entry in entries:
            if not entry or not entry.endswith(".py"):
                continue
            meta, _path = entry.split("\t", 1)
            mode, kind, oid = meta.split()
            if kind != "blob" or oid in seen_blobs:
                continue
            seen_blobs.add(oid)
            source = _git(repo, "cat-file", "blob", oid)
            if len(source) > 300_000:
                continue
            for name, chunker in (("ast", ast_chunks), ("window", window_chunks)):
                hashes = {hashlib.sha256(c.encode()).hexdigest() for c in chunker(source)}
                if index > 0:  # the first commit is a cold index, not an incremental one
                    totals[name][0] += len(hashes)
                    totals[name][1] += len(hashes - seen[name])
                seen[name] |= hashes
    return {
        "repo": str(repo),
        "commits_requested": commits,
        "commits_replayed": len(revs),
        **{
            name: {
                "chunks_in_changed_files": total,
                "new_chunks": new,
                "reuse_rate": round(1 - new / total, 4) if total else None,
            }
            for name, (total, new) in totals.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repos", nargs="+", type=Path)
    parser.add_argument("--commits", type=int, default=50)
    args = parser.parse_args()
    report = [replay(repo, args.commits) for repo in args.repos]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "dedup.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
