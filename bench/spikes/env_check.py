"""M0 environment gate: prints a JSON report, exits 1 if a hard requirement is unmet."""
import json
import platform
import sqlite3
import sys

MIN_SQLITE = (3, 51, 3)


def contentless_delete_works(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE probe USING fts5("
            "a, content='', contentless_delete=1, tokenize=\"unicode61 tokenchars '_'\")"
        )
        conn.execute("INSERT INTO probe(rowid, a) VALUES (1, 'get_user_name')")
        conn.execute("DELETE FROM probe WHERE rowid = 1")
        return conn.execute("SELECT count(*) FROM probe WHERE probe MATCH 'get_user_name'").fetchone()[0] == 0
    except sqlite3.OperationalError:
        return False


def main() -> int:
    conn = sqlite3.connect(":memory:")
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    report = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
        "fts5": "ENABLE_FTS5" in options,
        "contentless_delete": "ENABLE_FTS5" in options and contentless_delete_works(conn),
        "autocommit_attr": hasattr(conn, "autocommit"),
    }
    failures = []
    if sys.version_info[:2] != (3, 13):
        failures.append("python must be 3.13")
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        failures.append("sqlite older than 3.51.3: bundle SQLite through APSW instead of stdlib sqlite3")
    if not report["fts5"]:
        failures.append("sqlite built without FTS5")
    if not report["contentless_delete"]:
        failures.append("FTS5 contentless_delete unavailable")
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
