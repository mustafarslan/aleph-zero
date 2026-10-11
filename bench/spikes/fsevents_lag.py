"""Can FSEvents tell us that nothing changed? How long after a change does it report it?

Usage: uv run python bench/spikes/fsevents_lag.py        (macOS only)

ADR-008 proposed a watcher used only as a flag: if no event arrived since the last
view, reuse it. That is sound only if an event for every change has been delivered by
the time we ask. This spike changes a file, then asks at once and keeps asking, and
records how long the change took to show. The watcher flushes synchronously
(FSEventStreamFlushSync) before each answer.

Result on macOS 27, M4 Max, 2026-10-12: nothing was ever missed, but 1-6% of changes
were reported about 1,010 ms late, for plain writes and for new files, with and
without per-file events. So a clear flag does not mean "nothing changed", and the
watcher was not adopted.
"""
import ctypes
import ctypes.util
import os
import statistics
import tempfile
import threading
import time
from ctypes import CFUNCTYPE, POINTER, c_char_p, c_double, c_size_t, c_uint32, c_uint64, c_void_p
from pathlib import Path

_CALLBACK = CFUNCTYPE(None, c_void_p, c_void_p, c_size_t, c_void_p, POINTER(c_uint32), POINTER(c_uint64))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
_cs = ctypes.CDLL(ctypes.util.find_library("CoreServices"))
_sys = ctypes.CDLL(ctypes.util.find_library("System"))
_cf.CFStringCreateWithCString.restype = c_void_p
_cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
_cf.CFArrayCreate.restype = c_void_p
_cf.CFArrayCreate.argtypes = [c_void_p, POINTER(c_void_p), ctypes.c_long, c_void_p]
_cf.CFRelease.argtypes = [c_void_p]
_cs.FSEventStreamCreate.restype = c_void_p
_cs.FSEventStreamCreate.argtypes = [c_void_p, _CALLBACK, c_void_p, c_void_p, c_uint64, c_double, c_uint32]
_cs.FSEventStreamSetDispatchQueue.argtypes = [c_void_p, c_void_p]
_cs.FSEventStreamStart.argtypes = [c_void_p]
for _name in ("FSEventStreamFlushSync", "FSEventStreamStop", "FSEventStreamInvalidate", "FSEventStreamRelease"):
    getattr(_cs, _name).argtypes = [c_void_p]
_sys.dispatch_queue_create.restype = c_void_p
_sys.dispatch_queue_create.argtypes = [c_char_p, c_void_p]


class Watcher:
    """FSEvents used only to set a flag."""

    def __init__(self, root: Path, flags: int) -> None:
        self._lock = threading.Lock()
        self._dirty = False
        self._callback = _CALLBACK(self._on_events)
        string = _cf.CFStringCreateWithCString(None, os.path.realpath(root).encode(), 0x08000100)
        array = _cf.CFArrayCreate(None, (c_void_p * 1)(string), 1,
                                  ctypes.addressof(c_void_p.in_dll(_cf, "kCFTypeArrayCallBacks")))
        self._stream = _cs.FSEventStreamCreate(None, self._callback, None, array, 0xFFFFFFFFFFFFFFFF, 0.0, flags)
        _cs.FSEventStreamSetDispatchQueue(self._stream, _sys.dispatch_queue_create(b"aleph-zero.lag", None))
        _cs.FSEventStreamStart(self._stream)

    def _on_events(self, stream, info, count, paths, flags, ids) -> None:
        with self._lock:
            self._dirty = True

    def take(self) -> bool:
        _cs.FSEventStreamFlushSync(self._stream)
        with self._lock:
            dirty, self._dirty = self._dirty, False
        return dirty

    def close(self) -> None:
        _cs.FSEventStreamStop(self._stream)
        _cs.FSEventStreamInvalidate(self._stream)
        _cs.FSEventStreamRelease(self._stream)


def trial(flags: int, change, rounds: int = 80, gap: float = 0.03) -> str:
    root = Path(tempfile.mkdtemp()) / "tree"
    (root / "src").mkdir(parents=True)
    for i in range(20):
        (root / f"src/f{i}.py").write_text("x\n")
    time.sleep(0.6)
    watcher = Watcher(root, flags)
    watcher.take()
    time.sleep(0.2)
    watcher.take()
    delays, missed = [], 0
    for i in range(rounds):
        change(root, i)
        started = time.perf_counter()
        seen = False
        while time.perf_counter() - started < 3.0:
            if watcher.take():
                seen = True
                break
        if seen:
            delays.append((time.perf_counter() - started) * 1000)
        else:
            missed += 1
        time.sleep(gap)
    watcher.close()
    late = sum(delay > 20 for delay in delays)
    return f"missed {missed}  late (>20 ms) {late}/{rounds}  p50 {statistics.median(delays):.2f} ms  max {max(delays):.0f} ms"


def same_file(root, i):
    (root / "src/f0.py").write_text(f"a = {i}\n")


def different_files(root, i):
    (root / f"src/f{i % 20}.py").write_text(f"a = {i}\n")


def atomic_replace(root, i):
    (root / "src/.tmp").write_text(f"a = {i}\n")
    os.replace(root / "src/.tmp", root / "src/f0.py")


def new_files(root, i):
    (root / f"src/new_{i}.py").write_text("n\n")


if __name__ == "__main__":
    for label, flags in (("per directory", 0x2), ("per file", 0x2 | 0x10)):
        for name, change in (("same file rewritten", same_file), ("different files", different_files),
                             ("atomic replace", atomic_replace), ("new files", new_files)):
            print(f"{label:14} {name:22} {trial(flags, change)}")
