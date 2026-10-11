"""Lexical search: identifier-aware text for FTS5, and the query that reads it."""
from __future__ import annotations

import re

from .chunking import Chunk

WEIGHTS = (5.0, 3.0, 2.0, 1.0)  # symbol, signature, doc, body: a starting grid, tuned in M2
MAX_TERMS = 48
_WORD = re.compile(r"[^\W\d]\w*|\d+")  # a word in any script, or a number
_ASCII_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by do does for from how in is it of on or that the this to "
    "was what when where which who why with".split()
)
# The unary plus stops SQLite from driving the query off the IN list, which would run
# one text match per row of the view: 5.8 s instead of 3 ms at 100k chunks (M0, spike D).
_SEARCH = (
    "SELECT rowid, bm25(chunk_fts, ?, ?, ?, ?) AS score FROM chunk_fts "
    "WHERE chunk_fts MATCH ? AND +rowid IN (SELECT chunk_id FROM temp.{table}) "
    "ORDER BY score LIMIT ?"
)
SEARCH_SQL = _SEARCH.format(table="view_chunks")
SCOPED_SQL = _SEARCH.format(table="scope_chunks")  # the view, narrowed by a kind or path filter


def _parts(piece: str) -> list[str]:
    """Split at case and digit boundaries: getUser -> get, User; HTTPServer -> HTTP, Server."""
    if piece.isascii():
        return _ASCII_PARTS.findall(piece)
    parts, start = [], 0
    for at in range(1, len(piece)):
        before, here = piece[at - 1], piece[at]
        if here.isdigit() != before.isdigit():
            cut = True
        elif here.isupper():
            cut = before.islower() or (before.isupper() and at + 1 < len(piece) and piece[at + 1].islower())
        else:
            cut = False
        if cut:
            parts.append(piece[start:at])
            start = at
    parts.append(piece[start:])
    return parts


def split_identifier(token: str) -> list[str]:
    """Sub-words of an identifier, lower-cased: getUserName -> get, user, name.
    Empty when there is nothing to split, so a plain word is not repeated."""
    parts = [part.lower() for piece in token.split("_") if piece for part in _parts(piece)]
    return parts if len(parts) > 1 else []


def expand(text: str) -> str:
    """Text as indexed: every word, and after each identifier its sub-words. Keeping the
    whole identifier matters: sub-words alone lose most of the gain (Radha and Goktas, 2026)."""
    out: list[str] = []
    for match in _WORD.finditer(text):
        token = match.group()
        out.append(token)
        out.extend(split_identifier(token))
    return " ".join(out)


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _WORD.finditer(query):
        token = match.group()
        for term in [token.lower(), *split_identifier(token)]:
            if term not in terms:
                terms.append(term)
    kept = [term for term in terms if term not in STOPWORDS]
    return (kept or terms)[:MAX_TERMS]


def match_expression(terms: list[str]) -> str:
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def insert(conn, chunk_id: int, chunk: Chunk) -> None:
    conn.execute(
        "INSERT INTO chunk_fts(rowid, symbol, signature, doc, body) VALUES (?, ?, ?, ?, ?)",
        (chunk_id, expand(f"{chunk.symbol} {chunk.breadcrumb}"), expand(chunk.signature),
         expand(chunk.doc), expand(chunk.text)),
    )


def search(conn, query: str, limit: int, scoped: bool = False) -> list[tuple[int, float]]:
    """(chunk id, score) for chunks in the current view, best first. Lower scores are better.
    With `scoped`, only chunks the caller put in temp.scope_chunks."""
    terms = query_terms(query)
    if not terms:
        return []
    return list(conn.execute(SCOPED_SQL if scoped else SEARCH_SQL, (*WEIGHTS, match_expression(terms), limit)))
