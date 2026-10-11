"""Search over the current view: candidate generators, fusion, and hits mapped back to paths."""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from . import filters, lexical, symbols
from .indexer import Index
from .view import View

RRF_K = 60
PATH_CANDIDATES = 5
PATH_TIE_LIMIT = 25
COPIES_SHOWN = 3  # places listed for one chunk that many files share
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
    # How many more files hold exactly this chunk and are not listed.
    duplicates: int = 0


def rrf(rankings: dict[str, list[int]]) -> list[tuple[int, float, tuple[str, ...]]]:
    """Reciprocal rank fusion: (chunk id, score, generators), best first. A generator's
    vote counts GENERATOR_WEIGHT times; unlisted generators count once."""
    scores: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    for name, ranked in rankings.items():
        weight = GENERATOR_WEIGHT.get(name, 1.0)
        for rank, chunk_id in enumerate(dict.fromkeys(ranked), start=1):  # a repeated id votes once
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank)
            sources.setdefault(chunk_id, []).append(name)
    return sorted(((cid, score, tuple(sources[cid])) for cid, score in scores.items()),
                  key=lambda item: (-item[1], item[0]))


def path_ranking(index: Index, view: View, terms: list[str],
                 accept: Callable[[str], bool] | None = None) -> list[tuple[str, str]]:
    """The files whose path matches the query's words, best first, as (blob, family).

    A multi-word query must match at least two words and a one-word query must match
    the file's own name, so a common word or an extension does not match everything.
    When too many files tie for best, the path says nothing and no file is proposed.

    Files are proposed, not chunks: many files open with the same lines, and a chunk
    shared by all of them would stand for every one."""
    need = min(2, len(terms))
    anywhere, in_name = index.path_words(view)
    matched: dict[str, int] = {}
    named: dict[str, int] = {}
    for term in terms:
        for path in anywhere.get(term, ()):
            matched[path] = matched.get(path, 0) + 1
        for path in in_name.get(term, ()):
            named[path] = named.get(path, 0) + 1
    scored = [(-(count + named.get(path, 0)), path) for path, count in matched.items()
              if count >= need and (len(terms) > 1 or path in named) and (accept is None or accept(path))]
    scored.sort()
    if not scored or sum(score == scored[0][0] for score, _ in scored) > PATH_TIE_LIMIT:
        return []
    keys = [(view.files[path], filters.family(path)) for _, path in scored[:PATH_CANDIDATES]]
    return list(dict.fromkeys(keys))


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
    })[:pool]
    by_chunk = {chunk_id: (score, sources) for chunk_id, score, sources in fused}
    # A file proposed for its path votes for its own first chunk, in that file only.
    by_file = {key: 1.0 / (RRF_K + rank)
               for rank, key in enumerate(path_ranking(index, view, terms, accept), start=1)}
    if not by_chunk and not by_file:
        return []
    columns = ("SELECT bc.chunk_id, bc.blob_oid, bc.family, bc.ord, bc.start_line, bc.end_line, bc.scope_start, "
               "bc.scope_end, c.kind, c.symbol, c.signature, c.breadcrumb, c.text "
               "FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id ")
    rows = list(index.conn.execute(
        columns + "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family "
        f"WHERE bc.chunk_id IN ({','.join('?' * len(by_chunk))})", list(by_chunk))) if by_chunk else []
    seen = {(row[1], row[2], row[3]) for row in rows}
    for oid, family in by_file:
        rows += [row for row in index.conn.execute(
            columns + "WHERE bc.blob_oid = ? AND bc.family = ? AND bc.ord = 0", (oid, family))
            if (row[1], row[2], row[3]) not in seen]
    copies: dict[int, list[Hit]] = {}
    for (chunk_id, oid, family, ordinal, start, end, scope_start, scope_end, chunk_kind, symbol, signature,
         breadcrumb, text) in rows:
        if kind == "docs" and chunk_kind != "doc":
            continue
        if kind == "code" and chunk_kind == "doc":
            continue
        score, sources = by_chunk.get(chunk_id, (0.0, ()))
        if ordinal == 0 and (oid, family) in by_file:
            score, sources = score + by_file[(oid, family)], (*sources, "path")
        for path in index.paths_for(oid, family):
            if accept is None or accept(path):
                copies.setdefault(chunk_id, []).append(
                    Hit(path, start, end, chunk_kind, symbol, signature, breadcrumb, text, score, sources,
                        scope_start or start, scope_end or end))
    hits: list[Hit] = []
    for same in copies.values():
        # The same text in many files (a licence header, a common import line) is one
        # finding: show a few places, best first, and count the rest.
        same.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        shown = same[:COPIES_SHOWN]
        if len(same) > len(shown):
            shown[0] = replace(shown[0], duplicates=len(same) - len(shown))
        hits += shown
    hits.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
    return arrange(merge_pieces(hits), k, kind)
