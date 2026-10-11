"""M0 environment gate: prints a JSON report, exits 1 if a hard requirement is unmet."""
import json
import platform
import sqlite3
import sys

import apsw

MIN_SQLITE = (3, 51, 3)


def contentless_delete_works(conn: apsw.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE probe USING fts5("
            "a, content='', contentless_delete=1, tokenize=\"unicode61 tokenchars '_'\")"
        )
        conn.execute("INSERT INTO probe(rowid, a) VALUES (1, 'get_user_name')")
        conn.execute("DELETE FROM probe WHERE rowid = 1")
        return conn.execute("SELECT count(*) FROM probe WHERE probe MATCH 'get_user_name'").fetchall()[0][0] == 0
    except apsw.Error:
        return False


def main() -> int:
    conn = apsw.Connection(":memory:")
    options = set(apsw.compile_options)
    bundled = tuple(int(part) for part in apsw.sqlite_lib_version().split("."))
    report = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "interpreter_sqlite": sqlite3.sqlite_version,
        "apsw": apsw.apsw_version(),
        "sqlite": apsw.sqlite_lib_version(),
        "amalgamation": apsw.using_amalgamation,
        "fts5": "ENABLE_FTS5" in options,
        "contentless_delete": "ENABLE_FTS5" in options and contentless_delete_works(conn),
    }
    failures = []
    if sys.version_info[:2] != (3, 13):
        failures.append("python must be 3.13")
    if bundled < MIN_SQLITE:
        failures.append("APSW's SQLite is older than 3.51.3")
    if not apsw.using_amalgamation:
        failures.append("APSW is linked to a system SQLite instead of its bundled one")
    if not report["fts5"]:
        failures.append("sqlite built without FTS5")
    if not report["contentless_delete"]:
        failures.append("FTS5 contentless_delete unavailable")
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
