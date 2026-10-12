"""Benchmark C: does searching by meaning as well as by words find more of the right files?

    uv run --group bench python bench/eval/benchmark_c.py prepare WORKDIR [--repos a/b,c/d] [--max-chunks N]
    uv run --group bench python bench/eval/benchmark_c.py run WORKDIR [--repos ...] --out FILE

Questions are Loc-Bench instances: the text of an issue, and the files the fix edited.
For every instance the repository is read at the issue's base commit straight from
git's object store (no checkout), so `prepare` can index and embed every commit once
and `run` can then compare configurations without touching a working tree.

`prepare` uses the network (it fetches each base commit by id from GitHub) and the
embedding worker (GPU, about a hundred chunks a second). `run` does not use the network; it embeds each
question with the same worker (one text a question), and has no heat guard of its own.

Both run for a long time and are made to be left alone and looked after from another shell:

    benchmark_c.py status WORKDIR      where it is, how warm the chip got, whether it still runs
    benchmark_c.py pause  WORKDIR      it waits after the batch it is on; `resume` lets it go on
    benchmark_c.py stop   WORKDIR      it ends after the batch it is on

Whatever ends a run (stop, a closed connection, a restart), the same command takes it up
where it was: what is indexed and embedded stays so, and `run` keeps each question's result
(`WORKDIR/run-cache`, per setting and per version of the engine's code), so a second run
with another repository added, or after an interruption, works out only what is new.
`prepare` watches the chip's temperature by itself: it waits when it is at `--temp-limit`,
and gives the job up at `--temp-stop` or when it has had to wait `--max-pauses` times within
half an hour. `--away` sets these for a machine nobody is sitting at (85, 97, 6), and
`--hours 19-7` keeps the work to those hours. It does not start with less than 20 GB of
free disk, on battery, with a pause left over in the work directory, or on a machine whose chip
temperature cannot be read; it waits while the machine is on battery, and with `--away` it gives the job up
when the temperature can no longer be read. While the network is away it waits (up to two hours a repository) and
then fetches the same repository; what it could not fetch is counted, and the run is then not "done". To start it so that it outlives the connection:

    nohup caffeinate -i uv run --group bench python bench/eval/benchmark_c.py prepare WORKDIR --away ... > WORKDIR/prepare.log 2>&1 &

Configurations compared on the same questions:
  bm25     one FTS5 row per file, default tokenizer, every word OR-ed (fixed in advance)
  lexical  Aleph without vectors
  dense    vectors only
  hybrid   Aleph with vectors (rank fusion)
  convex   a weighted sum of normalised text and vector scores, for several weights
           (benchmark only: ADR-002 asks whether it beats rank fusion)

Each is run on three forms of the question, because an issue is hundreds of words and
an agent's query is not:
  full   the whole issue text
  title  its first line
  rare   the 24 rarest words of the issue, the ones Aleph's text search keeps anyway
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import re
import statistics
import socket
import subprocess
import sys
import time
from pathlib import Path

import apsw

from aleph_zero import embedder, filters, gitio, lexical
from aleph_zero.embed import worker
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient, EmbedPool, Unavailable
from aleph_zero.indexer import Index
from aleph_zero.search import search
from aleph_zero.store import index_file, open_db
from aleph_zero.vectors import VectorStore
from aleph_zero.view import View

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import longrun  # noqa: E402  pause, resume, stop and status of a long job
from temps import Guard, TooHot  # noqa: E402  the temperature guard lives beside the other bench tools

DATASET = ("czlll/Loc-Bench_V1", "data/test-00000-of-00001.parquet")
CACHE = Path(__file__).resolve().parents[1] / "data" / "locbench"
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
FORMS = ("full", "title", "rare")
ALPHAS = (0.2, 0.4, 0.6, 0.8)   # weight of the vector score in the convex combination
MODES = ("lexical", "dense", "hybrid", *(f"convex{alpha}" for alpha in ALPHAS))


def instances(repos: set[str] | None) -> list[dict]:
    import pyarrow.parquet as pq                    # only the commands that read the questions need these
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(DATASET[0], DATASET[1], repo_type="dataset", local_dir=CACHE)
    rows = []
    for row in pq.read_table(path).to_pylist():
        if repos is not None and row["repo"] not in repos:
            continue
        edited = row["edit_functions"]
        edited = ast.literal_eval(edited) if isinstance(edited, str) else list(edited)
        row["gold"] = sorted({entry.split(":")[0] for entry in edited})
        if row["gold"]:
            rows.append(row)
    return rows


def clone_of(workdir: Path, repo: str) -> Path:
    return workdir / repo.replace("/", "__")


def fetch(workdir: Path, row: dict) -> Path:
    """Make sure the instance's base commit is in a local repository. Network."""
    clone = clone_of(workdir, row["repo"])
    if not (clone / ".git").exists():
        clone.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(clone), "init", "--quiet"], check=True)
        subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", f"https://github.com/{row['repo']}"], check=True)
    have = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{row['base_commit']}^{{commit}}"], capture_output=True)
    if have.returncode != 0:
        subprocess.run(["git", "-C", str(clone), "fetch", "--quiet", "--depth", "1", "origin", row["base_commit"]],
                       check=True, capture_output=True, timeout=1800)
    return clone


NETWORK_EVERY = 120.0     # seconds between two looks whether the network is back
NETWORK_WAIT = 7200.0     # how long one repository waits for it; then it is counted as not fetched


def reachable() -> bool:
    """Whether GitHub answers at all: tells a network that is away from a repository whose fetch fails."""
    try:
        socket.create_connection(("github.com", 443), timeout=10).close()
    except OSError:
        return False
    return True


def fetched(workdir: Path, row: dict, job: longrun.Control, say) -> Path | None:
    """`fetch`, for a job nobody watches: while the network is away it waits (a stop is heard, the status says so)
    and then fetches the same repository; a fetch that fails with the network there is tried once more. None when
    it is given up: the caller counts it."""
    again, deadline = True, time.monotonic() + NETWORK_WAIT
    while True:
        try:
            return fetch(workdir, row)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            say(f"fetch failed: {str(exc)[:80]}")
            if not reachable():
                left = deadline - time.monotonic()
                if left <= 0 or not job.wait_for("waiting for the network", reachable, every=NETWORK_EVERY, at_most=left):
                    return None
            elif again and isinstance(exc, subprocess.CalledProcessError):
                again = False
            else:
                return None


def view_at(clone: Path, commit: str) -> View:
    """The repository as it was at `commit`, read from the object store."""
    info = gitio.repo_info(clone)
    tree = gitio.git(clone, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{tree}}").decode().strip()
    return View(info.root, info.common_dir, info.object_format, tree, gitio.ls_tree(clone, tree), frozenset())


def code_fingerprint() -> str:
    """A name for the engine's code as it is now: a result worked out by other code is not this code's result."""
    digest = hashlib.sha256()
    source = Path(__file__).resolve().parents[2] / "src" / "aleph_zero"
    for path in [*sorted(source.rglob("*.py")), Path(__file__).resolve()]:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


class ResultCache:
    """What `run` worked out for one question, kept per setting and per version of the code. A run that was
    interrupted, or is repeated with a repository added, reads these and works out only what is missing."""

    def __init__(self, directory: Path, settings: dict, fingerprint: str) -> None:
        self.directory = directory
        self._salt = json.dumps(settings, sort_keys=True) + "\0" + fingerprint

    def _file(self, instance: str, commit: str) -> Path:
        key = hashlib.sha256(f"{self._salt}\0{instance}\0{commit}".encode()).hexdigest()[:24]
        return self.directory / f"{key}.json"

    def get(self, instance: str, commit: str) -> dict | None:
        try:
            return json.loads(self._file(instance, commit).read_text())
        except (OSError, ValueError):
            return None         # not worked out yet, or cut short: worked out again

    def put(self, instance: str, commit: str, entry: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        file = self._file(instance, commit)
        aside = file.with_suffix(f".{os.getpid()}.part")
        aside.write_text(json.dumps(entry))
        os.replace(aside, file)


def prepare(workdir: Path, rows: list[dict], max_chunks: int, temp_limit: float = 95.0, guard=None,
            temp_stop: float | None = None, max_pauses: int | None = None, hours: tuple[int, int] | None = None,
            mains=None) -> str:
    """Index and embed every instance's commit. Returns how it ended: 'done', 'stopped', 'stopped for heat'."""
    done = partial = 0
    say = lambda text: print(text, flush=True)  # noqa: E731
    # Hours of GPU work: after every batch the chip's temperature is looked at (at most every
    # 15 s), and embedding waits while it is at the limit or macOS reports thermal pressure.
    cool = guard or Guard(limit=temp_limit, resume=temp_limit - 15, say=say, stop_at=temp_stop, max_pauses=max_pauses)
    job = longrun.Control(workdir, "prepare", total=len(rows), guard=cool, say=say, hours=hours, mains=mains)
    if isinstance(cool, Guard):
        cool.while_waiting = job.cooling
    try:
        finished = _prepare(workdir, rows, max_chunks, job, counts := {"done": 0, "partial": 0, "unfetched": 0})
    except TooHot as hot:
        say(f"given up for heat: {hot}. Nothing is lost: the same command takes it up where it was")
        return job.finish(why=str(hot))
    done, partial = counts["done"], counts["partial"]
    print(f"prepared {done} of {len(rows)} instances; {partial} left partly embedded (run prepare again); "
          f"{len(rows) - done - partial} skipped, not fetched or not reached", flush=True)
    if hasattr(cool, "highest"):
        print(f"chip temperature: highest {cool.highest:.0f} C, paused {cool.pauses} times to cool", flush=True)
    # Not "done" while a repository is partly embedded or was not fetched (the network was away): `run` would measure
    # the one with vectors missing and leave the other out. One left out for its size is left out on purpose.
    owed = [f"{count} {what}" for count, what in ((partial, "left partly embedded"), (counts["unfetched"], "not fetched")) if count]
    return job.finish(done=finished and not owed,
                      why=f"{', '.join(owed)}: the same command does them again" if finished and owed else "")


def _prepare(workdir: Path, rows: list[dict], max_chunks: int, job: longrun.Control, counts: dict) -> bool:
    for number, row in enumerate(rows, 1):
        job()                       # between two repositories too: heat, a pause, the hours, a stop
        if job.stop_wanted():
            print("stopped on request; the same command takes it up here", flush=True)
            return False
        job.unit(number, row["instance_id"])
        started = time.monotonic()
        clone = fetched(workdir, row, job, lambda text: print(f"{number:4}/{len(rows)} {row['instance_id']:40} {text}", flush=True))
        if clone is None:
            counts["unfetched"] += 1
            job.note(not_fetched=counts["unfetched"])
            continue
        view = view_at(clone, row["base_commit"])
        index = Index(open_db(index_file(view.common_dir)))
        while index.sync(view, budget_seconds=600).pending:
            pass
        store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
        have, total = store.coverage(index)
        if total > max_chunks:
            print(f"{number:4}/{len(rows)} {row['instance_id']:40} skipped: {total} chunks is over the limit", flush=True)
            index.conn.close()
            continue
        try:
            for attempt in (1, 2):
                # Asked for each time: a long fetch outlasts the worker's idle limit, and so does a wait inside the
                # embedding (a pause, the hours, the chip cooling). The worker is then gone at the next batch, which
                # the report says; the same repository is tried once more with a worker started again.
                sock = worker.ensure()
                pool = EmbedPool(sock, capacity=worker.capacity_tokens(), connections=worker.slots())
                try:
                    report = embedder.embed_pending(index, store, pool, batch=embedder.BATCH * worker.slots(), progress=job,
                                                    stop=job.stop_wanted)
                finally:
                    pool.close()
                if not (report.remaining and report.reason) or job.stop_wanted() or attempt == 2:
                    break
                print(f"{number:4}/{len(rows)} {row['instance_id']:40} {report.reason}: trying it once more", flush=True)
        finally:
            index.conn.close()
        counts["done"] += report.remaining == 0
        counts["partial"] += report.remaining > 0
        job.note(prepared=counts["done"], partly=counts["partial"])
        print(f"{number:4}/{len(rows)} {row['instance_id']:40} files {len(view.files):6} chunks {total:7} "
              f"embedded {report.embedded:6} left {report.remaining} {time.monotonic() - started:6.1f} s {report.reason}", flush=True)
    if job.stop_wanted():           # asked for while the last repository was embedded: that one is not finished
        print("stopped on request; the same command takes it up here", flush=True)
        return False
    return True


def first_files(paths: list[str], count: int = 10) -> list[str]:
    return list(dict.fromkeys(paths))[:count]


def bm25_files(view: View, question: str) -> list[str]:
    conn = apsw.Connection(":memory:")
    conn.execute("CREATE VIRTUAL TABLE files USING fts5(path UNINDEXED, body)")
    rows = []
    with gitio.Readers() as readers:
        for path in view.files:
            data = view.read(path, readers, filters.MAX_BYTES)
            if not isinstance(data, bytes):
                continue
            _, text = filters.classify(filters.family(path), data)
            if text is not None:
                rows.append((path, text))
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO files(path, body) VALUES (?, ?)", rows)
    conn.execute("COMMIT")
    words = list(dict.fromkeys(word.lower() for word in _WORD.findall(question)))
    if not words:
        return []
    match = " OR ".join(f'"{word}"' for word in words)
    return [row[0] for row in conn.execute("SELECT path FROM files WHERE files MATCH ? ORDER BY bm25(files) LIMIT 10", (match,))]


def chunk_files(index: Index, chunk_ids: list[int]) -> list[str]:
    """The first ten files that hold the given chunks, in the chunks' order."""
    paths: list[str] = []
    for chunk_id in chunk_ids:
        for oid, family in index.conn.execute(
                "SELECT bc.blob_oid, bc.family FROM blob_chunks bc JOIN temp.view_blobs v "
                "ON v.blob_oid = bc.blob_oid AND v.family = bc.family WHERE bc.chunk_id = ?", (chunk_id,)):
            paths.extend(index.paths_for(oid, family))
        if len(set(paths)) >= 10:
            break
    return first_files(paths)


def convex_chunks(text_scores: list[tuple[int, float]], vector_scores: list[tuple[int, float]], alpha: float) -> list[int]:
    """Chunks by alpha * vector + (1 - alpha) * text, each normalised between its
    theoretical minimum and the best score of this query (Bruch, Gai and Ingber, 2023)."""
    best_text = max((-score for _, score in text_scores), default=0.0) or 1.0   # bm25() is negative: lower is better
    best_vector = max((score for _, score in vector_scores), default=0.0)
    combined: dict[int, float] = {}
    for chunk_id, score in text_scores:
        combined[chunk_id] = (1 - alpha) * (-score / best_text)
    for chunk_id, score in vector_scores:
        normalised = (score + 1.0) / (best_vector + 1.0) if best_vector > -1.0 else 0.0   # cosine's floor is -1
        combined[chunk_id] = combined.get(chunk_id, 0.0) + alpha * normalised
    return [chunk_id for chunk_id, _ in sorted(combined.items(), key=lambda item: (-item[1], item[0]))]


def question_forms(index: Index, statement: str) -> dict[str, str]:
    title = next((line.strip() for line in statement.splitlines() if line.strip()), statement)
    rare = " ".join(lexical.select_terms(index.conn, lexical.query_terms(statement)))
    return {"full": statement, "title": title, "rare": rare or title}


def score(gold: list[str], found: list[str]) -> dict[str, float]:
    out = {}
    for cut in (1, 5, 10):
        top = set(found[:cut])
        out[f"recall@{cut}"] = len(top & set(gold)) / len(gold)
        out[f"all@{cut}"] = float(set(gold) <= top)
    first = next((position for position, path in enumerate(found, 1) if path in gold), 0)
    out["mrr"] = 1 / first if first else 0.0
    return out


def paired_bootstrap(a: list[float], b: list[float], rounds: int = 10_000, seed: int = 7) -> tuple[float, float, float]:
    """Mean of a - b over the same questions, with a 95% interval."""
    rng = random.Random(seed)
    differences = [x - y for x, y in zip(a, b)]
    count = len(differences)
    means = sorted(sum(differences[rng.randrange(count)] for _ in range(count)) / count for _ in range(rounds))
    return statistics.mean(differences), means[int(0.025 * rounds)], means[int(0.975 * rounds)]


def run(workdir: Path, rows: list[dict], out: Path | None, with_bm25: bool, code_and_docs_only: bool = False,
        k: int = 10, cache: ResultCache | None = None) -> dict:
    sock = worker.ensure()
    client = EmbedClient(sock, capacity=worker.capacity_tokens(), timeout=60)
    results = []
    job = longrun.Control(workdir, "run", total=len(rows), say=lambda text: print(text, file=sys.stderr, flush=True))
    stopped = False
    for number, row in enumerate(rows, 1):
        job()
        if job.stop_wanted():
            stopped = True
            break
        job.unit(number, row["instance_id"])
        kept = cache.get(row["instance_id"], row["base_commit"]) if cache is not None else None
        if kept is not None:
            results.append(kept)    # worked out by this code with these settings before: not again
            continue
        clone = clone_of(workdir, row["repo"])
        try:
            view = view_at(clone, row["base_commit"])
        except gitio.GitError:
            continue  # not prepared
        index = Index(open_db(index_file(view.common_dir)))
        if index.sync(view, budget_seconds=0.0).pending:
            index.conn.close()
            continue
        store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
        have, total = store.coverage(index)
        if have < total:
            index.conn.close()
            continue  # not fully embedded: skipped rather than scored on partial vectors
        # What vectors would propose if files with no language profile (JSON, YAML, SQL
        # fixtures: kind 'block') had none. Simulated by leaving their chunks out.
        left_out = {r[0] for r in index.conn.execute(
            "SELECT v.chunk_id FROM temp.view_chunks v JOIN chunks c ON c.chunk_id = v.chunk_id WHERE c.kind = 'block'")
        } if code_and_docs_only else set()

        def vector_search(vector, limit, scoped=False):
            found_chunks = store.search(index, vector, limit * 4 if left_out else limit, scoped)
            return [pair for pair in found_chunks if pair[0] not in left_out][:limit]

        forms = question_forms(index, row["problem_statement"])
        try:
            vectors = dict(zip(forms, client.embed(list(forms.values()), kind="query")))
        except Unavailable as exc:
            raise SystemExit(f"the worker is needed for the query vectors: {exc}") from exc
        found: dict[str, dict[str, list[str]]] = {}
        for form, question in forms.items():
            vector = vectors[form]
            nearest = lambda limit, scoped, vector=vector: [  # noqa: E731
                chunk_id for chunk_id, _ in vector_search(vector, limit, scoped)]
            text_scores = lexical.search(index.conn, question, 200)
            vector_scores = vector_search(vector, 200)
            found[form] = {
                "lexical": first_files([hit.path for hit in search(index, view, question, k=k)]),
                "hybrid": first_files([hit.path for hit in search(index, view, question, k=k, dense=nearest)]),
                "dense": chunk_files(index, [chunk_id for chunk_id, _ in vector_scores]),
                **{f"convex{alpha}": chunk_files(index, convex_chunks(text_scores, vector_scores, alpha)) for alpha in ALPHAS},
            }
        bm25 = bm25_files(view, forms["full"]) if with_bm25 else []
        index.conn.close()
        entry = {"instance": row["instance_id"], "repo": row["repo"], "gold": row["gold"], "chunks": total,
                 "title": forms["title"][:120], "files": found, "bm25_files": bm25,
                 "bm25": score(row["gold"], bm25),
                 "scores": {form: {mode: score(row["gold"], found[form][mode]) for mode in MODES} for form in FORMS}}
        results.append(entry)
        if cache is not None:
            cache.put(row["instance_id"], row["base_commit"], entry)
        print(f"{number:4}/{len(rows)} {row['instance_id']:36} " + "  ".join(
            f"{form}: lex {entry['scores'][form]['lexical']['recall@10']:.2f} dense {entry['scores'][form]['dense']['recall@10']:.2f} "
            f"hyb {entry['scores'][form]['hybrid']['recall@10']:.2f}" for form in FORMS), file=sys.stderr, flush=True)
    job.finish(done=not stopped)
    if stopped:
        raise SystemExit(f"stopped on request after {len(results)} questions; what was worked out is kept: "
                         f"the same command takes it up there")
    if not results:
        raise SystemExit("no prepared instances found; run `prepare` first")
    summary: dict = {"instances": len(results), "repos": sorted({entry["repo"] for entry in results}),
                     "bm25_full": {key: round(statistics.mean(entry["bm25"][key] for entry in results), 3)
                                   for key in results[0]["bm25"]}}
    for form in FORMS:
        block: dict = {}
        for mode in MODES:
            block[mode] = {key: round(statistics.mean(entry["scores"][form][mode][key] for entry in results), 3)
                           for key in ("recall@1", "recall@5", "recall@10", "all@10", "mrr")}
        for name, (first, second) in {"hybrid_minus_lexical": ("hybrid", "lexical"), "dense_minus_lexical": ("dense", "lexical")}.items():
            block[name] = {}
            for key in ("recall@10", "recall@1", "mrr"):
                mean, low, high = paired_bootstrap([entry["scores"][form][first][key] for entry in results],
                                                   [entry["scores"][form][second][key] for entry in results])
                block[name][key] = {"mean": round(mean, 3), "low": round(low, 3), "high": round(high, 3)}
        summary[form] = block
    print(json.dumps(summary, indent=2))
    if out:
        out.write_text(json.dumps({"summary": summary, "instances": results}, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("prepare", "run", "list", "status", "pause", "resume", "stop"))
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--repos", help="comma-separated owner/name; default: every repository of Loc-Bench")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-chunks", type=int, default=250_000, help="prepare: skip a commit with more chunks than this")
    parser.add_argument("--temp-limit", type=float, default=95.0,
                        help="prepare: pause embedding while the chip is at or above this many degrees Celsius")
    parser.add_argument("--temp-stop", type=float, help="prepare: give the job up when the chip is at or above this")
    parser.add_argument("--max-pauses", type=int, help="prepare: give the job up after waiting to cool this often within half an hour")
    parser.add_argument("--away", action="store_true",
                        help="prepare: nobody is at the machine: wait at 85 C, give up at 97 C or after 6 waits in half an hour")
    parser.add_argument("--hours", help="prepare: only work in these hours of the day, as FIRST-LAST (19-7: from 19:00 to 7:00)")
    parser.add_argument("--no-cache", action="store_true", help="run: work every question out again")
    parser.add_argument("--no-preflight", action="store_true", help="prepare: start although the disk is nearly full or the machine is on battery")
    parser.add_argument("--no-bm25", action="store_true")
    parser.add_argument("--k", type=int, default=10,
                        help="run: results asked of Aleph per question. The product's default is 8; 10 is the "
                             "least that can fill recall@10. The first run of this benchmark used 50, which also "
                             "deepened the candidate pool sixfold and flattered the result")
    parser.add_argument("--min-pool", type=int, help="run: override search.MIN_POOL, to measure a deeper candidate pool")
    parser.add_argument("--code-and-docs-only", action="store_true",
                        help="run: as if files without a language profile had no vectors")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        hours = longrun.parse_hours(args.hours)     # before anything is fetched, written or started
    except ValueError as wrong:
        parser.error(str(wrong))
    workdir = args.workdir.resolve()
    if args.action in ("status", "pause", "resume", "stop"):
        return longrun.main([args.action, str(workdir)])
    rows = instances(set(args.repos.split(",")) if args.repos else None)[: args.limit]
    if args.action == "list":
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["repo"]] = counts.get(row["repo"], 0) + 1
        for repo, count in sorted(counts.items(), key=lambda item: -item[1]):
            print(f"{count:4}  {repo}")
        print(f"{len(rows)} instances in {len(counts)} repositories")
    elif args.action == "prepare":
        # Nobody there: the lower of the limit asked for and 85 C, never a higher one than was asked.
        limit, stop_at, pauses = (min(args.temp_limit, 85.0), 97.0, 6) if args.away else (args.temp_limit, None, None)
        problems = [] if args.no_preflight else longrun.preflight(workdir)
        if problems:
            print("not started:\n  " + "\n  ".join(problems) + "\n(--no-preflight starts it anyway)", file=sys.stderr)
            return 2
        ended = prepare(workdir, rows, args.max_chunks, limit, temp_stop=args.temp_stop or stop_at,
                        max_pauses=args.max_pauses or pauses, hours=hours, mains=longrun.on_mains)
        return 0 if ended == "done" else 3
    else:
        if args.min_pool:
            from aleph_zero import search as search_module

            search_module.MIN_POOL = args.min_pool
        settings = {"k": args.k, "min_pool": args.min_pool, "bm25": not args.no_bm25, "code_and_docs_only": args.code_and_docs_only,
                    "alphas": ALPHAS, "forms": FORMS}
        cache = None if args.no_cache else ResultCache(workdir / "run-cache", settings, code_fingerprint())
        run(workdir, rows, args.out, not args.no_bm25, args.code_and_docs_only, args.k, cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
