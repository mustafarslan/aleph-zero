"""The vector file as a file: where it is, and giving back the disk under rows nobody owns.

Apart from `vectors` so that a session with no vectors, which has not loaded numpy, can still
do this after it has collected garbage.
"""
from __future__ import annotations

import os
import re
import struct
import sys
from pathlib import Path

import apsw

from .store import transaction

WIDTH = 256
ROW_BYTES = WIDTH * 2       # float16


def file_of(directory: Path, kind: str) -> Path:
    return Path(directory) / f"vectors.{re.sub(r'[^A-Za-z0-9._-]+', '_', kind)}.f16"


def _punch(descriptor: int, offset: int, length: int) -> None:
    """Hand the file system the blocks of this stretch of the file; its length stays and the stretch reads as
    zeros. Both ends on block boundaries. OSError where the file system has no such thing."""
    if sys.platform == "darwin":
        import fcntl

        fcntl.fcntl(descriptor, 99, struct.pack("IIqq", 0, 0, offset, length))      # F_PUNCHHOLE, struct fpunchhole
    elif sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.fallocate.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong)
        if libc.fallocate(descriptor, 0x01 | 0x02, offset, length) != 0:            # KEEP_SIZE | PUNCH_HOLE
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    else:
        raise OSError(45, f"not supported on {sys.platform}")


def give_back(conn: apsw.Connection, directory: Path) -> dict:
    """Give the file system back the blocks of the vector files that hold free rows only. Returns how many
    kinds of vector were looked at and how many bytes the file system took back this time, and `unsupported` with the reason
    where the file system has no way to do it (nothing is changed then).

    Under the index's write lock, and only for rows that are on the free list as committed: a hole cannot be
    taken back, so a row whose freeing could still be rolled back is not touched, and no session can be
    giving a free row to a new chunk meanwhile. Rows in use are never in a block that is given back: a
    stretch of free rows is narrowed to the whole blocks inside it."""
    found: dict = {"kinds": 0, "bytes": 0}
    with transaction(conn):
        for kind, end in conn.execute("SELECT kind, next_row FROM vector_files ORDER BY kind").fetchall():
            path = file_of(directory, kind)
            try:
                descriptor = os.open(path, os.O_RDWR)
            except OSError:
                continue
            try:
                status = os.fstat(descriptor)
                block = max(status.st_blksize or 4096, 4096)
                held = min(end, status.st_size // ROW_BYTES)
                found["kinds"] += 1
                punched = False
                start = previous = None
                free = (row[0] for row in conn.execute("SELECT row FROM vector_free WHERE kind = ? AND row < ? ORDER BY row", (kind, held)))
                for row in (*free, None):       # None closes the last stretch
                    if row is not None and previous is not None and row == previous + 1:
                        previous = row
                        continue
                    if start is not None:
                        first = -(-start * ROW_BYTES // block) * block
                        last = (previous + 1) * ROW_BYTES // block * block
                        if last > first:
                            _punch(descriptor, first, last - first)
                            punched = True
                    start = previous = row
                if punched:
                    os.fsync(descriptor)
                    # What the file system says it took back: blocks given back before are not counted again.
                    found["bytes"] += max(0, (status.st_blocks - os.fstat(descriptor).st_blocks) * 512)
            except OSError as exc:
                found["unsupported"] = str(exc)
                break
            finally:
                os.close(descriptor)
    return found
