"""The vector's width and the document's title, read on localisation: Benchmark C with vectors of another kind.

  uv run --group bench python bench/eval/widths.py --title none prepare bench/data/benchc-go --set others --language go --max-chunks 20000
  uv run --group bench python bench/eval/widths.py --title none --read 512 run bench/data/benchc-go --set others --language go --out ...
  uv run --group bench python bench/eval/widths.py compare BASE.json OTHER.json --languages-from bench/data/results/benchmark_c_go.json

The engine keeps a chunk's vector cut to 256 of the model's 768 numbers and embeds it with `title: none`
(ADR-001: chosen on a set too easy to tell 256 from 512, "revisited on localisation tasks", which it never was).
The model's card gives 512 and 768 as better on code and `title: {title or filename}` as the document's form.
This runs the benchmark's own `prepare` and `run` with vectors of a kind of their own, kept beside the engine's
and never read by it: all 768 numbers, under one of three titles. `--read` then scores as if only the first 256,
512 or 768 had been kept (cut and brought back to length one, as the engine does). No engine code is changed;
`--read 256 --title none` must give the engine's own results again, which is the check of this script.

Titles: `none` (as the engine embeds: the breadcrumb is the text's first line); `breadcrumb` (the chunk's own
breadcrumb as the title, its signature and text as the text: still content, shared by every file that holds
the chunk); `path` (the first path of the view that holds the chunk when it is embedded: no longer content, a
chunk in two files or in a file that moved keeps the title it got first; read to know what it would be worth).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

WIDTHS = (256, 512, 768)
FULL = 768
TITLES = {"none": "p1", "breadcrumb": "crumb1", "path": "path1"}      # the title -> its part of the vectors' kind


def _arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], add_help=False)
    parser.add_argument("--title", choices=TITLES, required=True)
    parser.add_argument("--read", type=int, choices=WIDTHS, default=FULL, help="run: score with the first this many numbers of each vector")
    return parser.parse_known_args(argv)


def kind_of(title: str) -> str:
    from aleph_zero.embed import client

    return f"{client.MODEL}/{TITLES[title]}/{FULL}"


def setup(title: str, read: int) -> None:
    """Point the engine's modules, in this process only, at vectors of the experiment's kind. Before anything
    that binds these names is loaded."""
    assert "aleph_zero.vectors" not in sys.modules and "benchmark_c" not in sys.modules, "set up before the engine's vectors are loaded"
    from aleph_zero import vectorfile
    from aleph_zero.embed import client

    client.WIDTH = FULL
    client.VECTOR_KIND = kind_of(title)
    vectorfile.WIDTH = FULL
    vectorfile.ROW_BYTES = FULL * 2
    from aleph_zero import embedder, vectors

    assert vectors.WIDTH == FULL and vectors.ROW_BYTES == FULL * 2
    if read != FULL:
        vectors.scores_of = cut_scores(vectors.scores_of, read)
    if title != "none":
        client.DOCUMENT_PREFIX = ""             # the title is written with each text
        embedder.embed_pending = titled(embedder, title)


def cut(matrix, width: int):
    """The first `width` numbers of each row, each row brought back to length one."""
    import numpy as np

    part = np.asarray(matrix, dtype=np.float32)[..., :width]
    return part / np.maximum(np.linalg.norm(part, axis=-1, keepdims=True), 1e-12)


def cut_scores(scores_of, width: int):
    """`vectors.scores_of`, as if rows and query had been kept at `width`."""
    import numpy as np

    def scores(mapped, rows, query):
        class Cut:
            """The mapped file, read through the cut."""
            def __getitem__(self, key):
                return cut(mapped[key], width)

            def __len__(self):
                return len(mapped)

        padded = np.zeros(width, dtype=np.float32)
        padded[:] = cut(query, width)
        return scores_of(Cut(), rows, padded)

    return scores


def document(title: str, breadcrumb: str, signature: str, text: str, path: str | None, chunk_text) -> str:
    """What is sent to the worker for a chunk under a title other than `none`."""
    if title == "breadcrumb":
        return f"title: {breadcrumb or 'none'} | text: {chunk_text('', signature if signature != breadcrumb else '', text)}"
    return f"title: {path or 'none'} | text: {chunk_text(breadcrumb, signature, text)}"


def titled(embedder, title: str):
    """`embedder.embed_pending`, with a title of the chunk's own in the document's prompt."""
    import time

    from aleph_zero.embed.client import Unavailable

    def embed_pending(index, store, client, budget_seconds=float("inf"), batch=embedder.BATCH, stop=None, progress=None):
        started = time.monotonic()
        embedded, reason = 0, ""
        with embedder.embedding_lock(store.path.parent) as held:
            if not held:
                reason = "another session is embedding this repository"
            while held and not (stop is not None and stop()):
                wanted = store.missing(index, limit=batch)
                if not wanted:
                    break
                marks = ",".join("?" * len(wanted))
                rows = index.conn.execute(
                    f"SELECT chunk_id, breadcrumb, signature, text, chunk_hash FROM chunks WHERE chunk_id IN ({marks}) ORDER BY chunk_id", wanted).fetchall()
                if not rows:
                    break
                paths: dict[int, str] = {}
                for chunk_id, oid, family in index.conn.execute(
                        "SELECT bc.chunk_id, bc.blob_oid, bc.family FROM blob_chunks bc JOIN temp.view_blobs v "
                        f"ON v.blob_oid = bc.blob_oid AND v.family = bc.family WHERE bc.chunk_id IN ({marks}) ORDER BY bc.chunk_id", wanted):
                    held_at = index._paths.get((oid, family))
                    if held_at and chunk_id not in paths:
                        paths[chunk_id] = min(held_at)
                texts = [document(title, row[1], row[2], row[3], paths.get(row[0]), embedder.chunk_text) for row in rows]
                try:
                    matrix = client.embed(texts, kind="document")
                except Unavailable as exc:
                    reason = str(exc)
                    break
                stored = store.add([row[0] for row in rows], matrix, hashes=[row[4] for row in rows])
                embedded += stored
                if progress is not None:
                    progress(embedded, len(wanted))
                if stored == 0 and len(rows) < len(wanted):
                    break
                if stored == 0 and store.replaced():
                    reason = "the index was rebuilt by another session; this one reopens it on its next call"
                    break
                if time.monotonic() - started >= budget_seconds:
                    break
        have, total = store.coverage(index)
        return embedder.EmbedReport(embedded, total - have, time.monotonic() - started, reason)

    return embed_pending


def compare(base: list[dict], other: list[dict], languages: dict[str, str], shipped) -> dict:
    """`other` against `base` on the questions both scored: per main language and all together, with vectors,
    first line and whole text. `shipped(language)` says whether the language ships with vectors: all questions
    together are read once over every language and once over those only."""
    import statistics

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import benchmark_c

    theirs = {row["instance"]: row for row in other}
    pairs = [(row, theirs[row["instance"]]) for row in base if row["instance"] in theirs]
    groups: dict[str, list] = {}
    for pair in pairs:
        groups.setdefault(languages.get(pair[0]["instance"], "unknown"), []).append(pair)
    groups["all languages"] = pairs
    groups["languages that ship with vectors"] = [pair for pair in pairs if shipped(languages.get(pair[0]["instance"], ""))]
    out: dict = {}
    for name, some in groups.items():
        if not some:
            continue
        block: dict = {"questions": len(some), "repositories": len({a["repo"] for a, _ in some})}
        for label, form in (("first line", "title"), ("whole text", "full")):
            for measure in ("recall@10", "recall@1"):
                old = [a["scores"][form]["hybrid"][measure] for a, _ in some]
                new = [b["scores"][form]["hybrid"][measure] for _, b in some]
                gain, low, high = benchmark_c.paired_bootstrap(new, old)
                block[f"{label}, {measure}"] = {"base": round(statistics.fmean(old), 4), "other": round(statistics.fmean(new), 4), "gain": round(gain, 4),
                                                "low": round(low, 4), "high": round(high, 4), "better": sum(x > y for x, y in zip(new, old)),
                                                "worse": sum(x < y for x, y in zip(new, old))}
        out[name] = block
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["compare"]:
        import json

        from aleph_zero import dense_policy

        parser = argparse.ArgumentParser(description="one result of this script against another, on the questions both scored")
        parser.add_argument("base", type=Path)
        parser.add_argument("other", type=Path)
        parser.add_argument("--languages-from", type=Path, nargs="+", default=[], help="results that hold each question's main language (population.py)")
        args = parser.parse_args(argv[1:])
        languages = {row["instance"]: row.get("main_language") or "python" for path in args.languages_from for row in json.loads(path.read_text())["instances"]}
        shipped = lambda language: bool(dense_policy.VERDICTS.get(language) and dense_policy.VERDICTS[language].helps)  # noqa: E731
        print(json.dumps(compare(json.loads(args.base.read_text())["instances"], json.loads(args.other.read_text())["instances"], languages, shipped), indent=1))
        return 0
    ours, rest = _arguments(argv)
    if rest and rest[0] == "run" and "--no-cache" not in rest:
        rest.append("--no-cache")       # the benchmark's kept answers are the engine's kind's, whatever this process is set to
    setup(ours.title, ours.read)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import benchmark_c

    return benchmark_c.main(rest)


if __name__ == "__main__":
    sys.exit(main())
