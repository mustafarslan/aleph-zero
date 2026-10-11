"""Non-gating: can Aeon v4's Trace serve as a lock-guarded, open-per-operation memory store?"""
import argparse
import fcntl
import gc
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from aleph_zero import memstat

DATA = Path(__file__).resolve().parents[1] / "data"
BUILD_LIB = DATA / "aeon-build" / "lib"
STORE = DATA / "cache" / "aeon_probe"
DIM = 256


def load_core():
    if not list(BUILD_LIB.glob("core.cpython-313-*.so")):
        print(f"no Aeon extension under {BUILD_LIB}; run Task 8 Step 1 first", file=sys.stderr)
        raise SystemExit(2)
    sys.path.insert(0, str(BUILD_LIB))
    import core  # the nanobind extension alone, without aeon_py/__init__.py

    return core


def with_trace(core, path: Path, operation):
    """Exclusive lock, open, one operation, close, unlock.

    TraceManager has no close(): its destructor closes the files, so the object
    must be dropped before the lock is released. `operation` must not return
    anything that keeps the TraceManager alive.
    """
    with open(f"{path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            trace = core.TraceManager(str(path))
            try:
                return operation(trace)
            finally:
                del trace
                gc.collect()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def unit(seed: int) -> list[float]:
    vector = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return (vector / np.linalg.norm(vector)).tolist()


def fresh(name: str) -> Path:
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / name
    for leftover in STORE.glob(name + "*"):
        leftover.unlink()
    return path


def timings(core, events: int) -> dict:
    path = fresh(f"timing_{events}.trace")

    def fill(trace):
        started = time.perf_counter()
        for i in range(events):
            trace.append_event(f"s{i % 8}", 1, f"note {i} about module_{i % 97}", embedding=unit(i), event_time=int(time.time()))
        return (time.perf_counter() - started) * 1e3 / events

    append_ms = with_trace(core, path, fill)
    query = unit(events // 2)
    search_ms, write_ms = [], []
    for i in range(20):
        started = time.perf_counter()
        hits = with_trace(core, path, lambda trace: len(trace.semantic_search(query, 10)))
        search_ms.append((time.perf_counter() - started) * 1e3)
        started = time.perf_counter()
        with_trace(core, path, lambda trace: trace.append_event("probe", 1, f"late {i}", embedding=unit(10**6 + i), event_time=int(time.time())))
        write_ms.append((time.perf_counter() - started) * 1e3)
    return {
        "events": events,
        "bulk_append_ms_each": round(append_ms, 3),
        "open_search_close_p50_ms": round(statistics.median(search_ms), 2),
        "open_append_close_p50_ms": round(statistics.median(write_ms), 2),
        "hits_for_top_10": hits,
        "disk_mb": round(sum(f.stat().st_size for f in STORE.glob(path.name + "*")) / 1e6, 1),
    }


def writer(session: str, count: int) -> None:
    core = load_core()
    path = STORE / "interleave.trace"
    for i in range(count):
        with_trace(core, path, lambda trace: trace.append_event(session, 1, f"{session}-{i}", embedding=unit(hash((session, i)) % 2**31), event_time=int(time.time())))


def interleave(core, per_writer: int = 500) -> dict:
    import subprocess

    path = fresh("interleave.trace")
    children = [
        subprocess.Popen([sys.executable, __file__, "--writer", session, "--count", str(per_writer)])
        for session in ("alpha", "bravo")
    ]
    codes = [child.wait() for child in children]

    def audit(trace):
        result = {"size": trace.size()}
        ids: list[int] = []
        for session in ("alpha", "bravo"):
            history = trace.get_history(session, per_writer * 4)
            result[f"{session}_events"] = len(history)
            result[f"{session}_distinct_texts"] = len({event["text"] for event in history})
            ids += [event["id"] for event in history]
        result["ids_unique"] = len(ids) == len(set(ids))
        return result

    report = with_trace(core, path, audit)
    report["writer_exit_codes"] = codes
    report["nothing_lost"] = (
        report["size"] == 2 * per_writer
        and report["alpha_distinct_texts"] == per_writer
        and report["bravo_distinct_texts"] == per_writer
        and report["ids_unique"]
    )
    return report


def overfetch(core) -> dict:
    """Trace search has no filter. Does asking for more candidates recover a crowded-out session?"""
    path = fresh("overfetch.trace")
    shared = unit(7)

    def fill(trace):
        for i in range(60):
            trace.append_event("crowd", 1, f"crowd {i}", embedding=shared, event_time=int(time.time()))
        for i in range(5):
            trace.append_event("target", 1, f"target {i}", embedding=shared, event_time=int(time.time()))

    with_trace(core, path, fill)

    def count_target(top_k: int) -> int:
        return with_trace(core, path, lambda trace: sum(e["session_id"] == "target" for e in trace.semantic_search(shared, top_k)))

    return {"target_hits_top_5": count_target(5), "target_hits_top_100": count_target(100), "target_events": 5}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writer")
    parser.add_argument("--count", type=int, default=500)
    args = parser.parse_args()
    if args.writer:
        writer(args.writer, args.count)
        return 0
    private_before = memstat.private_bytes()
    core = load_core()
    report = {
        "torch_loaded": "torch" in sys.modules,
        "sentence_transformers_loaded": "sentence_transformers" in sys.modules,
        "import_private_mb": round((memstat.private_bytes() - private_before) / 1e6, 1),
        "timings": [timings(core, 5_000), timings(core, 50_000)],
        "interleave": interleave(core),
        "overfetch": overfetch(core),
    }
    report["private_mb_after_all"] = round(memstat.private_bytes() / 1e6, 1)
    out = DATA / "results" / "aeon_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
