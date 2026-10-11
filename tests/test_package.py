import apsw

import aleph_zero


def test_package_has_version():
    assert aleph_zero.__version__ == "0.0.1"


def test_bundled_sqlite_is_new_enough_for_wal_and_fts5():
    # Aleph Zero uses APSW's bundled SQLite, not the interpreter's: uv's managed
    # CPython 3.13.12 ships SQLite 3.50.4, below the 3.51.3 WAL-reset fix.
    version = tuple(int(part) for part in apsw.sqlite_lib_version().split("."))
    assert version >= (3, 51, 3)
    assert apsw.using_amalgamation
    assert "ENABLE_FTS5" in apsw.compile_options
