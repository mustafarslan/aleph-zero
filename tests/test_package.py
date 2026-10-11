import sqlite3

import aleph_zero


def test_package_has_version():
    assert aleph_zero.__version__ == "0.0.1"


def test_sqlite_is_new_enough_for_wal_and_fts5():
    assert sqlite3.sqlite_version_info >= (3, 51, 3)
    options = {row[0] for row in sqlite3.connect(":memory:").execute("PRAGMA compile_options")}
    assert "ENABLE_FTS5" in options
