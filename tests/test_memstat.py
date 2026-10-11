import mmap
import subprocess
import sys

import numpy as np
import pytest

from aleph_zero import memstat

MB = 1024 * 1024


def _write_file_in_another_process(path, megabytes):
    code = (
        "import os, sys\n"
        "chunk = os.urandom(1024 * 1024)\n"
        "with open(sys.argv[1], 'wb') as fh:\n"
        "    for _ in range(int(sys.argv[2])):\n"
        "        fh.write(chunk)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(path), str(megabytes)], check=True)


def test_readings_are_positive_for_own_process():
    assert memstat.rss_bytes() > 5 * MB
    assert memstat.private_bytes() > 1 * MB


def test_mapped_file_counts_in_rss_but_not_in_private(tmp_path):
    path = tmp_path / "blob.bin"
    _write_file_in_another_process(path, 64)
    rss_before, private_before = memstat.rss_bytes(), memstat.private_bytes()
    with open(path, "rb") as fh:
        mapping = mmap.mmap(fh.fileno(), 0, prot=mmap.PROT_READ)
        view = np.frombuffer(mapping, dtype=np.uint8)
        assert int(view[::4096].sum()) >= 0  # touch every page
        rss_after, private_after = memstat.rss_bytes(), memstat.private_bytes()
        del view
        mapping.close()
    assert rss_after - rss_before > 48 * MB
    assert private_after - private_before < 16 * MB


def test_heap_allocation_counts_as_private():
    private_before = memstat.private_bytes()
    block = np.ones(64 * MB, dtype=np.uint8)
    block[::4096] = 2
    private_after = memstat.private_bytes()
    del block
    assert private_after - private_before > 48 * MB


def test_dead_pid_raises_instead_of_reading_zero():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    with pytest.raises(memstat.MemstatError):
        memstat.private_bytes(proc.pid)
    with pytest.raises(memstat.MemstatError):
        memstat.rss_bytes(proc.pid)
