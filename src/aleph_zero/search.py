"""Search over the current view: candidate generators, fusion, and hits mapped back to paths."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from . import lexical
from .indexer import Index
from .view import View

RRF_K = 60
PATH_CANDIDATES = 5


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


def rrf(rankings: dict[str, list[int]]) -> list[tuple[int, float, tuple[str, ...]]]:
    """Reciprocal rank fusion: (chunk id, score, generators), best first."""
    scores: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    for name, ranked in rankings.items():
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(chunk_id, []).append(name)
    return sorted(((cid, score, tuple(sources[cid])) for cid, score in scores.items()),
                  key=lambda item: (-item[1], item[0]))


def path_ranking(index: Index, view: View, terms: list[str]) -> list[int]:
    """First chunk of the files whose path matches the query's words, best first.
    A multi-word query must match at least two words, so one common word does not flood it."""
    need = min(2, len(terms))
    scored: list[tuple[int, str]] = []
    for path, (words, name_words) in index.path_tokens(view).items():
        matched = sum(term in words for term in terms)
        if matched >= need:
            scored.append((-(matched + sum(term in name_words for term in terms)), path))
    chunk_ids: list[int] = []
    for _, path in sorted(scored)[:PATH_CANDIDATES]:
        rows = index.conn.execute(
            "SELECT chunk_id FROM blob_chunks WHERE blob_oid = ? AND ord = 0", (view.files[path],)).fetchall()
        if rows:
            chunk_ids.append(rows[0][0])
    return chunk_ids


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
    terms = lexical.query_terms(query)
    if not terms:
        return []
    pool = max(50, k * 6)
    fused = rrf({
        "lexical": [chunk_id for chunk_id, _ in lexical.search(index.conn, query, pool)],
        "path": path_ranking(index, view, terms),
    })[:pool]
    if not fused:
        return []
    marks = ",".join("?" * len(fused))
    occurrences: dict[int, list[tuple]] = {}
    for row in index.conn.execute(
        "SELECT bc.chunk_id, bc.blob_oid, bc.start_line, bc.end_line, c.kind, c.symbol, c.signature, "
        "c.breadcrumb, c.text FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id "
        "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid "
        f"WHERE bc.chunk_id IN ({marks}) ORDER BY bc.blob_oid, bc.ord",
        [chunk_id for chunk_id, _, _ in fused],
    ):
        occurrences.setdefault(row[0], []).append(row[1:])
    hits: list[Hit] = []
    for chunk_id, score, sources in fused:
        for oid, start, end, chunk_kind, symbol, signature, breadcrumb, text in occurrences.get(chunk_id, ()):
            if kind == "docs" and chunk_kind != "doc":
                continue
            if kind == "code" and chunk_kind == "doc":
                continue
            for path in view.paths_for(oid):
                if path_glob is None or fnmatch(path, path_glob):
                    hits.append(Hit(path, start, end, chunk_kind, symbol, signature, breadcrumb, text, score, sources))
    return arrange(hits, k, kind)
