"""Documentation ranked by whole documents (DEFERRED, "Documentation answers, the next hypothesis: whole documents").

Search ranks prose by its sections. A question about a whole page ("how do I configure the mail server") can match a
document's words spread over many sections and no one section strongly; whole-file BM25 found more of the answering
documents (96 to 98% against 81 to 83%). A document's words are all its sections' words, with the tokenizer full-text
search uses, kept in this process by content (as `areas` keeps its bags), so an edit re-reads only the changed file.
"""
from __future__ import annotations

import collections
import threading

from . import lexical
from .areas import _Ranking, _index_key
from .indexer import Index
from .view import View

_lock = threading.Lock()
_bags: dict[str, dict[tuple[str, str], collections.Counter]] = {}     # by index file: (blob, family) -> its words
_views: collections.OrderedDict = collections.OrderedDict()            # (index file, view) -> (ranking, path -> content)
_KEPT_VIEWS = 4
_BATCH = 500


def words(text: str) -> collections.Counter:
    """Every term full-text search would index for this text, counted, stop words left out."""
    counts: collections.Counter = collections.Counter()
    for match in lexical._WORD.finditer(text):
        for term in lexical._forms(match.group()):
            if term not in lexical.STOPWORDS:
                counts[term] += 1
    return counts


def _documents(index: Index) -> dict[str, tuple[str, str]]:
    """The view's documentation files: path -> (blob, family), for the contents chunked as prose."""
    rows = index.conn.execute(
        "SELECT DISTINCT bc.blob_oid, bc.family FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id "
        "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family WHERE c.kind = 'doc'")
    out: dict[str, tuple[str, str]] = {}
    for oid, family in rows:
        for path in index.paths_for(oid, family):
            out[path] = (oid, family)
    return out


def _read(index: Index, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], collections.Counter]:
    bags = {key: collections.Counter() for key in wanted}
    for start in range(0, len(wanted), _BATCH):
        part = wanted[start:start + _BATCH]
        oids = sorted({oid for oid, _ in part})
        marks = ",".join("?" * len(oids))
        for oid, family, text in index.conn.execute(
                "SELECT bc.blob_oid, bc.family, c.text FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id "
                f"WHERE c.kind = 'doc' AND bc.blob_oid IN ({marks})", oids):
            if (oid, family) in bags:
                bags[(oid, family)].update(words(text or ""))
    return bags


def ranking(index: Index, view: View) -> tuple[_Ranking, dict[str, tuple[str, str]]]:
    """BM25 over the view's documents, and each document's content."""
    key = (_index_key(index), view.fingerprint)
    with _lock:
        if key in _views:
            _views.move_to_end(key)
            return _views[key]
        kept = _bags.setdefault(key[0], {})
    documents = _documents(index)
    missing = sorted({content for content in documents.values() if content not in kept})
    if missing:
        read = _read(index, missing)
        with _lock:
            kept.update(read)
    made = (_Ranking({path: kept[content] for path, content in documents.items() if kept.get(content)}), documents)
    with _lock:
        _views[key] = made
        while len(_views) > _KEPT_VIEWS:
            _views.popitem(last=False)
    return made


def ranked(index: Index, view: View, question: str, accept=None, limit: int = 50) -> list[str]:
    """The view's documents, best first by whole-document BM25 against the question."""
    made, _ = ranking(index, view)
    asked = set(lexical.query_terms(question))
    return [path for path, _ in made.rank(asked) if accept is None or accept(path)][:limit]


def first_sections(index: Index, view: View, paths: list[str]) -> dict[str, int]:
    """Each document's first section, as a chunk id: what stands for a document search did not find."""
    _, documents = ranking(index, view)
    out: dict[str, int] = {}
    for path in paths:
        oid, family = documents[path]
        row = index.conn.execute(
            "SELECT bc.chunk_id FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id "
            "WHERE bc.blob_oid = ? AND bc.family = ? AND c.kind = 'doc' ORDER BY bc.ord LIMIT 1", (oid, family)).fetchone()
        if row:
            out[path] = row[0]
    return out
