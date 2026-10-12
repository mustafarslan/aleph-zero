"""Process memory readings.

RSS counts every resident page, including file-backed pages that several
processes share. Private memory counts only what the process owns, which is
what the machine-wide budget in ADR-005 is written in.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
_FOOTPRINT = re.compile(r"Footprint:\s*([\d.]+)\s*(B|KB|MB|GB)\b")


class MemstatError(RuntimeError):
    """A reading could not be taken (dead pid, unsupported platform)."""


def _smaps_rollup(pid: int) -> dict[str, int]:
    try:
        lines = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()[1:]
    except OSError as exc:
        raise MemstatError(f"no smaps_rollup for pid {pid}: {exc}") from exc
    fields: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0]) * 1024
    return fields


_libproc = None


def _footprint(pid: int) -> int | None:
    """macOS: the kernel's own figure for a process's footprint, the number `footprint` and
    Activity Monitor show. One system call, where the `footprint` tool takes 30 ms.
    None when the call is refused (another user's process) or missing."""
    global _libproc
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        # struct rusage_info_v0: a 16-byte uuid, then 64-bit fields; ri_phys_footprint is the eighth.
        buffer = (ctypes.c_uint64 * 48)()
        if _libproc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(0), ctypes.byref(buffer)) != 0:
            return None
        return int(buffer[2 + 7])
    except (OSError, AttributeError):
        return None


def private_bytes(pid: int | None = None) -> int:
    """Memory owned by the process alone: clean file-backed pages are excluded."""
    pid = os.getpid() if pid is None else pid
    if sys.platform == "darwin":
        found = _footprint(pid)
        if found:
            return found
        proc = subprocess.run(["/usr/bin/footprint", str(pid)], capture_output=True, text=True)
        match = _FOOTPRINT.search(proc.stdout)
        if match is None:
            raise MemstatError(f"footprint gave no reading for pid {pid}: {proc.stderr.strip()[:200]}")
        return int(float(match.group(1)) * _UNITS[match.group(2)])
    if sys.platform.startswith("linux"):
        fields = _smaps_rollup(pid)
        return fields.get("Pss_Anon", fields.get("Private_Dirty", 0)) + fields.get("SwapPss", 0)
    raise MemstatError(f"unsupported platform {sys.platform}")


def rss_bytes(pid: int | None = None) -> int:
    """Resident set size, shared pages included."""
    pid = os.getpid() if pid is None else pid
    if sys.platform.startswith("linux"):
        fields = _smaps_rollup(pid)
        if "Rss" not in fields:
            raise MemstatError(f"no Rss field for pid {pid}")
        return fields["Rss"]
    proc = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    text = proc.stdout.strip()
    if not text.isdigit():
        raise MemstatError(f"ps gave no rss for pid {pid}")
    return int(text) * 1024
