"""Spike D: brute-force vector scan, FTS5 query latency with a view filter, concurrent writers."""
import json
import multiprocessing
import time
from pathlib import Path

import apsw
import numpy as np

from aleph_zero import memstat

DATA = Path(__file__).resolve().parents[1] / "data"
LIMIT_MS = 50.0
DIM = 256
BLOCK = 16_384


def percentile(samples: list[float], q: float) -> float:
    return round(float(np.percentile(samples, q)), 3)


def scan_memmap(vectors: np.memmap, mask: np.ndarray, query: np.ndarray, k: int) -> np.ndarray:
    scores = np.empty(len(vectors), dtype=np.float32)
    for start in range(0, len(vectors), BLOCK):
        scores[start:start + BLOCK] = vectors[start:start + BLOCK].astype(np.float32) @ query
    scores[~mask] = -np.inf
    return np.argpartition(-scores, k)[:k]


def vector_section() -> dict:
    out = {}
    rng = np.random.default_rng(0)
    for count in (10_000, 100_000, 300_000):
        dense = rng.standard_normal((count, DIM), dtype=np.float32)
        dense /= np.linalg.norm(dense, axis=1, keepdims=True)
        path = DATA / "cache" / f"vectors_{count}.f16"
        path.parent.mkdir(parents=True, exist_ok=True)
        dense.astype(np.float16).tofile(path)
        mask = rng.random(count) < 0.8
        queries = dense[rng.choice(count, size=50, replace=False)]
        private_before = memstat.private_bytes()
        mapped = np.memmap(path, dtype=np.float16, mode="r", shape=(count, DIM))
        scan_memmap(mapped, mask, queries[0], 10)  # warm-up
        mapped_ms, heap_ms = [], []
        for query in queries:
            started = time.perf_counter()
            scan_memmap(mapped, mask, query, 10)
            mapped_ms.append((time.perf_counter() - started) * 1e3)
            started = time.perf_counter()
            scores = dense @ query
            scores[~mask] = -np.inf
            np.argpartition(-scores, 10)[:10]
            heap_ms.append((time.perf_counter() - started) * 1e3)
        out[str(count)] = {
            "memmap_f16_p50_ms": percentile(mapped_ms, 50),
            "memmap_f16_p95_ms": percentile(mapped_ms, 95),
            "heap_f32_p50_ms": percentile(heap_ms, 50),
            "heap_f32_p95_ms": percentile(heap_ms, 95),
            "file_mb": round(path.stat().st_size / 1e6, 1),
            "private_growth_mb": round((memstat.private_bytes() - private_before) / 1e6, 1),
        }
        del mapped
        path.unlink()
    return out


def open_db(path: Path) -> apsw.Connection:
    # APSW bundles SQLite 3.53.4; the interpreter's own is 3.50.4 (below the 3.51.3 floor).
    conn = apsw.Connection(str(path))
    conn.set_busy_timeout(5000)
    conn.pragma("journal_mode", "wal")
    conn.pragma("synchronous", "normal")
    return conn


def fts_section(rows: int = 100_000) -> dict:
    path = DATA / "cache" / "fts_probe.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = open_db(path)
    conn.execute(
        "CREATE VIRTUAL TABLE chunk_fts USING fts5(symbol, signature, doc, body, "
        "content='', contentless_delete=1, tokenize=\"unicode61 tokenchars '_'\")"
    )
    rng = np.random.default_rng(1)
    vocab = [f"{a}_{b}" for a in ("get", "set", "load", "parse", "build", "send", "read", "open", "sync", "make")
             for b in (f"item{i}" for i in range(500))]
    conn.execute("BEGIN IMMEDIATE")
    for rowid in range(1, rows + 1):
        picks = rng.choice(len(vocab), size=96, replace=True)
        words = [vocab[i] for i in picks]
        conn.execute(
            "INSERT INTO chunk_fts(rowid, symbol, signature, doc, body) VALUES (?, ?, ?, ?, ?)",
            (rowid, words[0], " ".join(words[:6]), " ".join(words[6:16]), " ".join(words[16:])),
        )
    conn.execute("COMMIT")
    conn.execute("CREATE TEMP TABLE view_chunks(chunk_id INTEGER PRIMARY KEY)")
    # generate_series is not compiled into this SQLite build, so fill the view from Python.
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO view_chunks(chunk_id) VALUES (?)",
        ((rowid,) for rowid in range(1, rows + 1) if rowid % 5 != 0),
    )
    conn.execute("COMMIT")
    # The unary plus stops the planner from driving the query off the IN list,
    # which would run one MATCH per view row. Same rows, same order.
    sql = (
        "SELECT rowid, bm25(chunk_fts, 5.0, 3.0, 2.0, 1.0) AS score FROM chunk_fts "
        "WHERE chunk_fts MATCH ? AND +rowid IN (SELECT chunk_id FROM temp.view_chunks) "
        "ORDER BY score LIMIT 50"
    )
    slow_sql = sql.replace("+rowid IN", "rowid IN")

    def terms() -> str:
        return " OR ".join(f'"{vocab[j]}"' for j in rng.choice(len(vocab), size=4, replace=False))

    samples = []
    for i in range(201):
        query = terms()
        started = time.perf_counter()
        conn.execute(sql, (query,)).fetchall()
        if i:  # the first call is a warm-up
            samples.append((time.perf_counter() - started) * 1e3)
    slow_samples = []
    for _ in range(3):  # kept only to record how much the wrong form costs
        query = terms()
        started = time.perf_counter()
        conn.execute(slow_sql, (query,)).fetchall()
        slow_samples.append((time.perf_counter() - started) * 1e3)
    size_mb = round(path.stat().st_size / 1e6, 1)
    conn.close()
    return {
        "rows": rows,
        "p50_ms": percentile(samples, 50),
        "p95_ms": percentile(samples, 95),
        "without_unary_plus_p50_ms": percentile(slow_samples, 50),
        "db_mb": size_mb,
    }


def _writer(path: str, seconds: float, queue) -> None:
    conn = open_db(Path(path))
    errors = writes = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("INSERT INTO t(v) VALUES (?)", [(str(i),) for i in range(50)])
            conn.execute("COMMIT")
            writes += 1
        except (apsw.BusyError, apsw.LockedError):
            errors += 1
    queue.put(("writer", writes, errors))


def _reader(path: str, seconds: float, queue) -> None:
    conn = open_db(Path(path))
    errors = reads = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            conn.execute("SELECT count(*) FROM t").fetchone()
            reads += 1
        except (apsw.BusyError, apsw.LockedError):
            errors += 1
    queue.put(("reader", reads, errors))


def concurrency_section(seconds: float = 10.0) -> dict:
    path = DATA / "cache" / "concurrency_probe.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = open_db(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.close()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_writer, args=(str(path), seconds, queue)) for _ in range(2)]
    workers += [context.Process(target=_reader, args=(str(path), seconds, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=seconds + 30) for _ in workers]
    for worker in workers:
        worker.join()
    return {
        "writers": 2,
        "readers": 2,
        "write_transactions": sum(n for kind, n, _ in results if kind == "writer"),
        "reads": sum(n for kind, n, _ in results if kind == "reader"),
        "lock_errors": sum(e for _, _, e in results),
    }


def main() -> int:
    report = {"vectors": vector_section(), "fts": fts_section(), "concurrency": concurrency_section()}
    report["pass"] = (
        report["vectors"]["100000"]["memmap_f16_p95_ms"] < LIMIT_MS
        and report["fts"]["p95_ms"] < LIMIT_MS
        and report["concurrency"]["lock_errors"] == 0
    )
    out = DATA / "results" / "storage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
