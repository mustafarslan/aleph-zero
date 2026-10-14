"""Lexical search: identifier-aware text for FTS5, and the query that reads it."""
from __future__ import annotations

import functools
import re

import apsw

from .chunking import Chunk

WEIGHTS = (5.0, 3.0, 2.0, 1.0)  # symbol, signature, doc, body. Five other grids were tried (ADR-002, 2026-10-13): none gains a point
MAX_TERMS = 400      # words read from a query before the rarest are chosen
LONG_QUERY = 16      # a query with more terms than this keeps only its rarest
KEEP_TERMS = 24
TOKENIZER = ("porter", ["unicode61", "tokenchars", "_"])  # must match store/schema.sql
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
    return list(_split(token))


@functools.lru_cache(maxsize=8192)
def _split(token: str) -> tuple[str, ...]:
    """`split_identifier`, remembered: a repository writes the same few ten thousand words hundreds of thousands
    of times. Measured on a cold index of Django: 8.6 s, 7.7 s with this, for 2 MB (65,536 kept were 19 MB for nothing more)."""
    parts = tuple(part.lower() for piece in token.split("_") if piece for part in _parts(piece))
    return parts if len(parts) > 1 else ()


def expand(text: str) -> str:
    """Text as indexed: every word, and after each identifier its sub-words. Keeping the
    whole identifier matters: sub-words alone lose most of the gain (Radha and Goktas, 2026)."""
    out: list[str] = []
    for match in _WORD.finditer(text):
        token = match.group()
        out.append(token)
        out.extend(_split(token))
    return " ".join(out)


@functools.lru_cache(maxsize=8192)
def _forms(token: str) -> tuple[str, ...]:
    """A word as it is searched for: itself, and the sub-words of an identifier. Remembered, because
    the paths of a view are read through this and repeat the same few thousand words."""
    return (token.lower(), *_split(token))


def all_forms(text: str) -> frozenset[str]:
    """Every word of a text as it is searched for, stop words too, uncut and in no order: what `query_terms` starts
    from. For the parts of paths, which are read once for all the files that share them."""
    return frozenset(form for match in _WORD.finditer(text) for form in _forms(match.group()))


def query_terms(query: str) -> list[str]:
    terms: dict[str, None] = {}  # ordered, with constant-time membership
    for match in _WORD.finditer(query):
        token = match.group()
        for term in _forms(token):
            terms.setdefault(term)
        if len(terms) >= 2 * MAX_TERMS:
            break  # a pasted log of thousands of words: the rest would be cut anyway
    kept = [term for term in terms if term not in STOPWORDS]
    return (kept or list(terms))[:MAX_TERMS]


def select_terms(conn, terms: list[str]) -> list[str]:
    """For a long query (a pasted issue, a paragraph), the KEEP_TERMS terms found in the
    fewest chunks, rarest first. Every extra term of an OR query is another posting list
    to read, and a word in half the chunks says little. On the Loc-Bench Django issues
    this answered in 118 ms instead of 172 ms with the same files found, within the noise
    of 35 questions (ADR-002). Terms the index has never seen are dropped. A short query
    is returned as it is."""
    if len(terms) <= LONG_QUERY:
        return terms
    # Line numbers and addresses of a pasted traceback are in few chunks, but by accident.
    terms = [term for term in terms if not term.isdigit()] or terms
    tokenize = conn.fts5_tokenizer(*TOKENIZER)
    stems: dict[str, str] = {}
    for term in terms:
        tokens = tokenize(term.encode("utf-8", "replace"), apsw.FTS5_TOKENIZE_QUERY, None)
        if tokens:
            stems[term] = tokens[0][-1]
    wanted = sorted(set(stems.values()))
    counts = dict(conn.execute(
        f"SELECT term, doc FROM temp.chunk_vocab WHERE term IN ({','.join('?' * len(wanted))})", wanted))
    known = [term for term in terms if counts.get(stems.get(term, ""), 0) > 0]
    known.sort(key=lambda term: counts[stems[term]])  # stable: equally rare terms keep the query's order
    return known[:KEEP_TERMS]


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
    terms = select_terms(conn, query_terms(query))
    if not terms:
        return []
    return list(conn.execute(SCOPED_SQL if scoped else SEARCH_SQL, (*WEIGHTS, match_expression(terms), limit)))
