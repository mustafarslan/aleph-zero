"""Process memory readings.

RSS counts every resident page, including file-backed pages that several
processes share. Private memory counts only what the process owns, which is
what the machine-wide budget in ADR-005 is written in.
"""
from __future__ import annotations

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


def private_bytes(pid: int | None = None) -> int:
    """Memory owned by the process alone: clean file-backed pages are excluded."""
    pid = os.getpid() if pid is None else pid
    if sys.platform == "darwin":
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
