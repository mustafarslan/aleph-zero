"""Compiler-grade indexes (SCIP) kept beside the index, and when one may be believed.

A SCIP index is computed for a whole tree. What a call in one file resolves to can depend
on another file's exports or on a type declared in a third, so an answer taken from the
index is exact only while the file and everything it depends on are what the index was
made from (ADR-004). Each document is therefore kept with the blob it was made from and
the documents it depends on; a question checks those blobs against the view before it
believes a line of it. When the check fails, the resolver answers and the answer says why.

One compressed row per document: every reference to a definition inside the tree, where
it refers to something outside (a library: the compiler knows, the resolver can only
guess), and its dependencies. For eslint (a 54 MB index) that is 0.6 MB.
"""
from __future__ import annotations

import json
import time
import zlib
from pathlib import Path

import apsw

from . import scip
from .store import transaction

DEPENDENCY_LIMIT = 2000         # files looked at to trust one document; a file that depends on more is not trusted
# An index is read whole, and reading one takes about five times its size in memory (measured: 266 MB for
# eslint's 54 MB). 200 MB of file is about 1 GB for the seconds an import takes; a larger one is refused.
MAX_INDEX_BYTES = 200 * 1024 * 1024


class NotAnIndex(ValueError):
    """The file given is not a SCIP index this build can read."""


class Unstamped(ValueError):
    """The import was not told what the index was made from, and the worktree does not say."""


def read(path: Path) -> tuple[list[scip.Reduced], int, str]:
    """(documents reduced, documents left out for their path, the tool that made the index)."""
    try:
        size = path.stat().st_size
        if size > MAX_INDEX_BYTES:
            raise NotAnIndex(f"{path} is {size / 1e6:.0f} MB; the largest index read is {MAX_INDEX_BYTES / 1e6:.0f} MB")
        data = path.read_bytes()
        kept, skipped = scip.reduce(scip.parse_index(data))
    except (OSError, scip.WireFormatError) as exc:
        raise NotAnIndex(f"{path} is not a readable SCIP index: {exc}") from exc
    if not kept:
        raise NotAnIndex(f"{path} is not a readable SCIP index: it holds no document of this tree")
    return kept, skipped, scip.tool_name(data)


def _pack(value) -> bytes:
    return zlib.compress(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogateescape"), 6)


def _unpack(blob: bytes):
    return json.loads(zlib.decompress(blob).decode("utf-8", "surrogateescape"))


def store(conn: apsw.Connection, kept: list[scip.Reduced], skipped: int, tool: str, stamps: dict[str, str], stamped: str) -> dict:
    """Keep the documents whose path `stamps` knows, each with the blob it is taken to have been made
    from. A document of an earlier import at the same path is replaced; an import left with no
    document is forgotten."""
    known = [document for document in kept if document.path in stamps]
    with transaction(conn):
        conn.execute("INSERT INTO scip_imports(tool, stamped, imported_at, skipped) VALUES (?, ?, ?, ?)",
                     (tool, stamped, int(time.time()), skipped + len(kept) - len(known)))
        import_id = conn.last_insert_rowid()
        for document in known:
            paths = sorted({target for _, _, target, _, _ in document.references})
            number = {path: at for at, path in enumerate(paths)}
            refs = {"files": paths, "to": [[line, column, number[target], at, name] for line, column, target, at, name in document.references],
                    "outside": [list(site) for site in document.outside], "encoding": document.encoding}
            # Each dependency with the blob it had when this document was made: what a later import says of
            # that path is another index's business, and must not vouch for this one.
            depends = [[each, stamps.get(each, "")] for each in document.depends_on]
            conn.execute(
                "INSERT INTO scip_documents(path, import_id, blob_oid, depends, refs) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET import_id = excluded.import_id, blob_oid = excluded.blob_oid, "
                "depends = excluded.depends, refs = excluded.refs",
                (document.path, import_id, stamps[document.path], _pack(depends), _pack(refs)))
        conn.execute("DELETE FROM scip_imports WHERE import_id NOT IN (SELECT DISTINCT import_id FROM scip_documents)")
    return {"import_id": import_id, "tool": tool, "stamped": stamped, "documents": len(known),
            "references": sum(len(document.references) for document in known),
            "skipped": skipped + len(kept) - len(known)}


def imports(conn: apsw.Connection) -> list[dict]:
    return [{"import_id": import_id, "tool": tool, "stamped": stamped, "imported_at": at, "skipped": skipped, "documents": documents}
            for import_id, tool, stamped, at, skipped, documents in conn.execute(
                "SELECT i.import_id, i.tool, i.stamped, i.imported_at, i.skipped, count(d.path) FROM scip_imports i "
                "LEFT JOIN scip_documents d ON d.import_id = i.import_id GROUP BY i.import_id ORDER BY i.import_id")]


def forget(conn: apsw.Connection, import_id: int | None = None) -> int:
    """Forget one import, or all of them. Returns how many documents went."""
    with transaction(conn):
        before = conn.execute("SELECT count(*) FROM scip_documents").fetchall()[0][0]
        if import_id is None:
            conn.execute("DELETE FROM scip_imports")
        else:
            conn.execute("DELETE FROM scip_imports WHERE import_id = ?", (import_id,))
        return before - conn.execute("SELECT count(*) FROM scip_documents").fetchall()[0][0]


class Compiled:
    """What the imported indexes say for one view, and whether each document may be believed there."""

    def __init__(self, conn: apsw.Connection, files: dict[str, str]) -> None:
        self._conn, self._files = conn, files
        self._stamps: dict[str, tuple[str, list[list[str]]] | None] = {}   # path -> (blob it was made from, [dependency, its blob then])
        self._state: dict[str, str] = {}                               # path -> '' believed, else why not
        self._sites: dict[str, tuple[dict, set, int]] = {}
        self.any = bool(conn.execute("SELECT 1 FROM scip_documents LIMIT 1").fetchall())

    def _stamp(self, path: str) -> tuple[str, list[list[str]]] | None:
        if path not in self._stamps:
            rows = self._conn.execute("SELECT blob_oid, depends FROM scip_documents WHERE path = ?", (path,)).fetchall()
            self._stamps[path] = (rows[0][0], _unpack(rows[0][1])) if rows else None
        return self._stamps[path]

    def status(self, path: str) -> str | None:
        """None when no index covers the file; '' when its document may be believed in this view; else
        the reason it may not: the file or something it depends on, directly or through others, is not
        what the index was made from."""
        if not self.any or self._stamp(path) is None:
            return None
        if path in self._state:
            return self._state[path]
        own = self._stamp(path)
        if self._files.get(path) != own[0]:
            why = f"{path} has changed since the compiler index was made"
        else:
            why = ""
            waiting, seen = [tuple(each) for each in own[1]], {path}
            while waiting and not why:
                each, then = waiting.pop()
                if each in seen:
                    continue
                seen.add(each)
                if len(seen) - 1 > DEPENDENCY_LIMIT:
                    why = f"{path} depends on more than {DEPENDENCY_LIMIT} files: too many to check against the compiler index"
                    break
                if not then or self._files.get(each) != then:
                    why = f"{path} depends on {each}, which has changed since the compiler index was made"
                    break
                if self._state.get(each) == "":
                    continue        # already found whole, with all it depends on
                stamp = self._stamp(each)
                if stamp is None or stamp[0] != then:
                    # The same file as then, but what the index knew of *its* dependencies is gone: a later
                    # import replaced that document with one made from other content.
                    why = f"{path} depends on {each}, whose part of the compiler index was replaced by a later import"
                else:
                    waiting.extend(tuple(inner) for inner in stamp[1])
        self._state[path] = why
        return why

    def target(self, path: str, line: int, columns: tuple[int, int, int], name: str) -> tuple[str, int, str] | None | bool:
        """What the document says stands where a call's name does: (the defining file, the line of the
        definition's name, its name); False for something of another package; None when it says nothing
        there. `line` counts from 0; `columns` is the name's column in bytes, UTF-16 units and characters
        (-1: as in bytes). The document's own unit is used; when it names none and the units differ on
        this line, a place counts only if what stands there has the call's name."""
        if path not in self._sites:
            rows = self._conn.execute("SELECT refs FROM scip_documents WHERE path = ?", (path,)).fetchall()
            refs = _unpack(rows[0][0]) if rows else {"files": [], "to": [], "outside": []}
            files = refs["files"]
            self._sites[path] = ({(row[0], row[1]): (files[row[2]], row[3], row[4]) for row in refs["to"]},
                                 {(row[0], row[1]) for row in refs["outside"]}, refs.get("encoding", 0))
            if len(self._sites) > 400:
                self._sites.pop(next(iter(self._sites)))
        inside, outside, encoding = self._sites[path]
        in_bytes, in_utf16, in_characters = columns[0], columns[1] if columns[1] >= 0 else columns[0], columns[2] if columns[2] >= 0 else columns[0]
        if encoding or in_bytes == in_utf16 == in_characters:
            column = {1: in_bytes, 2: in_utf16, 3: in_characters}.get(encoding, in_bytes)
            found = inside.get((line, column))
            return found if found is not None else (False if (line, column) in outside else None)
        for column in dict.fromkeys((in_utf16, in_characters, in_bytes)):      # most indexers count UTF-16 units
            found = inside.get((line, column))
            if found is not None and (found[2] == name or found[2] == name + "#"):
                return found
        return None
