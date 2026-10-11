"""Search over the current view: candidate generators, fusion, and hits mapped back to paths."""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass

from . import filters, lexical, symbols
from .indexer import Index
from .view import View

RRF_K = 60
PATH_CANDIDATES = 5
PATH_TIE_LIMIT = 25
SYMBOL_CANDIDATES = 10
# A name written in the query that is exactly a definition's name is stronger evidence than
# a text match: its order decides between otherwise equal candidates. A starting value, tuned in M2.
GENERATOR_WEIGHT = {"symbol": 2.0}
SYMBOL_TIE_LIMIT = 25
_IDENTIFIER = re.compile(r"[^\W\d]\w*(?:(?:\.|::)[^\W\d]\w*)*")


@dataclass(frozen=True)
class Hit:
    path: str
    start_line: int
    end_line: int
    kind: str
    symbol: str
    signature: str
    breadcrumb: str
    text: str
    score: float
    sources: tuple[str, ...]  # which generators proposed it
    # The whole definition the chunk belongs to. Equal to its own lines unless the chunk
    # is one piece of a long function or the part of a class its methods do not cover.
    scope_start_line: int = 0
    scope_end_line: int = 0


def rrf(rankings: dict[str, list[int]]) -> list[tuple[int, float, tuple[str, ...]]]:
    """Reciprocal rank fusion: (chunk id, score, generators), best first. A generator's
    vote counts GENERATOR_WEIGHT times; unlisted generators count once."""
    scores: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    for name, ranked in rankings.items():
        weight = GENERATOR_WEIGHT.get(name, 1.0)
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank)
            sources.setdefault(chunk_id, []).append(name)
    return sorted(((cid, score, tuple(sources[cid])) for cid, score in scores.items()),
                  key=lambda item: (-item[1], item[0]))


def path_ranking(index: Index, view: View, terms: list[str],
                 accept: Callable[[str], bool] | None = None) -> list[int]:
    """First chunk of the files whose path matches the query's words, best first.

    A multi-word query must match at least two words and a one-word query must match
    the file's own name, so a common word or an extension does not match everything.
    When too many files tie for best, the path says nothing and no file is proposed."""
    need = min(2, len(terms))
    scored: list[tuple[int, str]] = []
    for path, (words, name_words) in index.path_tokens(view).items():
        if accept is not None and not accept(path):
            continue
        matched = sum(term in words for term in terms)
        in_name = sum(term in name_words for term in terms)
        if matched >= need and (len(terms) > 1 or in_name):
            scored.append((-(matched + in_name), path))
    scored.sort()
    if not scored or sum(score == scored[0][0] for score, _ in scored) > PATH_TIE_LIMIT:
        return []
    chunk_ids: list[int] = []
    for _, path in scored[:PATH_CANDIDATES]:
        rows = index.conn.execute(
            "SELECT chunk_id FROM blob_chunks WHERE blob_oid = ? AND family = ? AND ord = 0",
            (view.files[path], filters.family(path))).fetchall()
        if rows:
            chunk_ids.append(rows[0][0])
    return chunk_ids


def symbol_ranking(index: Index, query: str, scoped: bool) -> list[int]:
    """Chunks that define a name written in the query, best first. A word of a longer
    query that names more than SYMBOL_TIE_LIMIT definitions (`get`, `main`) says nothing
    about which one is meant, and proposes none."""
    written = _IDENTIFIER.findall(query)
    alone = len(written) == 1
    chunk_ids: list[int] = []
    for name in written:
        if not alone and name.lower() in lexical.STOPWORDS:
            continue
        found = symbols.rows(index, name, scoped)
        if not alone and len(found) > SYMBOL_TIE_LIMIT:
            continue
        for row in found[:SYMBOL_CANDIDATES]:
            if row[7] not in chunk_ids:
                chunk_ids.append(row[7])
    return chunk_ids


def merge_pieces(hits: list[Hit]) -> list[Hit]:
    """One result per definition: of the pieces of a long function, the best-ranked stays."""
    seen: set[tuple] = set()
    kept: list[Hit] = []
    for hit in hits:
        key = (hit.path, hit.symbol, hit.scope_start_line, hit.scope_end_line)
        if hit.symbol and key in seen:
            continue
        seen.add(key)
        kept.append(hit)
    return kept


def arrange(hits: list[Hit], k: int, kind: str) -> list[Hit]:
    """Top k. With kind 'any', code comes first and documentation is capped, so prose
    cannot crowd code out of a natural-language query."""
    if kind != "any":
        return hits[:k]
    code = [hit for hit in hits if hit.kind != "doc"]
    docs = [hit for hit in hits if hit.kind == "doc"]
    if not code or not docs:
        return hits[:k]
    reserved = min(max(2, k // 4), k // 2)  # documentation never takes more than half
    n_docs = min(len(docs), reserved)
    n_code = min(len(code), k - n_docs)
    n_docs = min(len(docs), k - n_code)  # unused code slots go to documentation
    return code[:n_code] + docs[:n_docs]


def search(index: Index, view: View, query: str, k: int = 8, kind: str = "any",
           path_glob: str | None = None) -> list[Hit]:
    """Hits for `query` in `view`. Call `index.sync(view)` first: only blobs of the
    synced view can be returned."""
    if kind not in ("any", "code", "docs"):
        raise ValueError(f"kind must be 'any', 'code' or 'docs', not {kind!r}")
    if not index.serves(view):
        raise ValueError("this view is not the one the index was last synced to; call index.sync(view) first")
    terms = lexical.query_terms(query)
    if not terms:
        return []
    # Filters narrow the candidates before ranking: applied afterwards, a filter that
    # few chunks pass would find nothing among the best fifty of everything.
    accept = re.compile(fnmatch.translate(path_glob)).match if path_glob is not None else None
    scoped = kind != "any" or accept is not None
    if scoped:
        index.narrow(view, kind, accept, path_glob)
    pool = max(50, k * 6)
    fused = rrf({
        "lexical": [chunk_id for chunk_id, _ in lexical.search(index.conn, query, pool, scoped=scoped)],
        "symbol": symbol_ranking(index, query, scoped),
        "path": path_ranking(index, view, terms, accept),
    })[:pool]
    if not fused:
        return []
    marks = ",".join("?" * len(fused))
    occurrences: dict[int, list[tuple]] = {}
    for row in index.conn.execute(
        "SELECT bc.chunk_id, bc.blob_oid, bc.family, bc.start_line, bc.end_line, bc.scope_start, bc.scope_end, "
        "c.kind, c.symbol, c.signature, "
        "c.breadcrumb, c.text FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id "
        "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family "
        f"WHERE bc.chunk_id IN ({marks}) ORDER BY bc.blob_oid, bc.family, bc.ord",
        [chunk_id for chunk_id, _, _ in fused],
    ):
        occurrences.setdefault(row[0], []).append(row[1:])
    hits: list[Hit] = []
    for chunk_id, score, sources in fused:
        for (oid, family, start, end, scope_start, scope_end, chunk_kind, symbol, signature, breadcrumb,
             text) in occurrences.get(chunk_id, ()):
            if kind == "docs" and chunk_kind != "doc":
                continue
            if kind == "code" and chunk_kind == "doc":
                continue
            for path in index.paths_for(oid, family):
                if accept is None or accept(path):
                    hits.append(Hit(path, start, end, chunk_kind, symbol, signature, breadcrumb, text, score, sources,
                                    scope_start or start, scope_end or end))
    return arrange(merge_pieces(hits), k, kind)
