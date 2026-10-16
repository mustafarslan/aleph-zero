"""What is installed is what the source tree has: a file read at run time must be in the wheel."""
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_everything_tracked_in_the_package_is_in_the_wheel(tmp_path):
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("needs uv and git")
    # The tests run with a cache directory of their own; the build backend is in the real one.
    outside = {name: value for name, value in os.environ.items() if name != "XDG_CACHE_HOME"}
    built = subprocess.run(["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path)], cwd=ROOT, capture_output=True, text=True, env=outside)
    if built.returncode != 0 and "network was disabled" in built.stderr:
        pytest.skip("the build backend is not in the local cache, and the tests do not use the network")
    assert built.returncode == 0, built.stderr[-600:]         # any other failure is the package's
    (wheel,) = tmp_path.glob("*.whl")
    inside = set(zipfile.ZipFile(wheel).namelist())
    tracked = subprocess.run(["git", "ls-files", "src/aleph_zero"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert tracked and [path for path in tracked if path.removeprefix("src/") not in inside] == []
    assert {"aleph_zero/store/schema.sql"} <= inside     # read at run time, and not Python
    entry = next(name for name in inside if name.endswith("entry_points.txt"))
    assert "aleph = aleph_zero.cli:main" in zipfile.ZipFile(wheel).read(entry).decode()
