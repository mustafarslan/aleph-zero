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
# Candidates each generator may propose before fusion. 50 was the first value; on the 235
# Loc-Bench questions asked by title, vectors added +3.2 points of recall@10 at 50, +4.1 at
# 100 and +5.5 at 300, with no change for words and names alone (ADR-002).
MIN_POOL = 300
PATH_TIE_LIMIT = 25
COPIES_SHOWN = 3  # places listed for one chunk that many files share
SYMBOL_CANDIDATES = 10
# How much one generator's vote counts in the fusion. All equal: in a longer query, a word
# that happens to be a definition's name is one vote among others. A query that is a single
# name is handled apart (see `search`). Weighing names 1.5 or 2 times was tried on 235 questions
# (ADR-002, 2026-10-13): the order inside the ten results moves, the files among them do not.
GENERATOR_WEIGHT: dict[str, float] = {}
# One result a file, the file's other places on its line: read three times, then switched off again: it cost the agent the
# code of a file's second and third results (ADR-002, 2026-10-14). `search(..., more_files=)` names the same files instead.
ONE_A_FILE = False
MORE_PLACES = 3
# Two trials of 2026-10-14 (docs/DEFERRED.md, entry 1d), off. Read twice and not adopted on first lines (ADR-002);
# back for a third reading on whole issue text, by a rule written before it, on questions never asked:
DOTTED_FALLBACK = False     # `self.rotate`, `pkg.Func`: looked up without what stands before the dot when the whole finds nothing
PATH_VOTE = "first"         # "found": a file's path votes for the best chunk found in it, not for its first lines
# Module summaries (areas.py; DEFERRED 0-graph part 17): "off", "M2" (the two best modules' files first, as part 14
# measured) or "M2floor" (only a module sharing two of the question's words). Set by part 17's reading.
AREAS = "off"
SYMBOL_TIE_LIMIT = 25
# What makes a word of a longer query look like an identifier: an underscore, a digit, a
# capital after the first letter, or a qualifier.
_SHAPED = re.compile(r"_|\d|\.|::|(?<=.)[A-Z]")
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
    # With one result a file: the file's other places among the results, best first, as (start, end, label).
    places: tuple[tuple[int, int, str], ...] = ()


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
    anywhere, in_name = index.path_words(view, terms)
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


def symbol_ranking(index: Index, query: str, scoped: bool) -> tuple[list[int], bool]:
    """Chunks that define a name written in the query, best first, and whether the query
    is that one name and nothing else. A word of a longer query that names more than
    SYMBOL_TIE_LIMIT definitions (`get`, `main`) says nothing about which one is meant,
    and proposes none."""
    written = _IDENTIFIER.findall(query)
    alone = len(written) == 1
    chunk_ids: list[int] = []
    for name in written:
        if not alone and not _SHAPED.search(name):
            continue  # in a sentence, a plain word is a word; `refresh_token` and `TokenStore` are names
        found = symbols.rows(index, name, scoped)
        if not found and DOTTED_FALLBACK and not alone:
            # What stands before the dot is often a variable or a module (`self.rotate`, `store.rotate`,
            # `auth.TokenStore.rotate`), which a definition's own qualified name does not hold.
            parts = [part for part in symbols.SEPARATOR.split(name) if part]
            for shorter in range(len(parts) - 1, 0, -1):
                found = symbols.rows(index, ".".join(parts[-shorter:]), scoped)
                if found:
                    break
        if not alone and len(found) > SYMBOL_TIE_LIMIT:
            continue
        for row in found[:SYMBOL_CANDIDATES]:
            if row[7] not in chunk_ids:
                chunk_ids.append(row[7])
    return chunk_ids, alone


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


def _path_filter(path_glob: str | None, families: frozenset[str] | None) -> Callable[[str], bool] | None:
    """Which paths a query may return: those matching the glob, in one of the families."""
    matches = re.compile(fnmatch.translate(path_glob)).match if path_glob is not None else None
    if families is None:
        return matches
    if matches is None:
        return lambda path: filters.family(path) in families
    return lambda path: bool(matches(path)) and filters.family(path) in families


def one_a_file(hits: list[Hit]) -> list[Hit]:
    """Each file's best result, in order, with the file's further results as its places (at most MORE_PLACES)."""
    first: dict[str, int] = {}
    out: list[Hit] = []
    places: dict[str, list[tuple[int, int, str]]] = {}
    for hit in hits:
        if hit.path not in first:
            first[hit.path] = len(out)
            out.append(hit)
            places[hit.path] = []
        elif len(places[hit.path]) < MORE_PLACES:
            places[hit.path].append((hit.start_line, hit.end_line, hit.breadcrumb or hit.symbol))
    return [replace(hit, places=tuple(places[hit.path])) if places[hit.path] else hit for hit in out]


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


def _areas_first(index: Index, view: View, query: str, merged: list[Hit], accept, columns: str, mode: str) -> list[Hit]:
    """The files of the modules whose summaries match the question best, before search's others (code only). A file
    search found keeps its own results; one it did not enters by its first chunk."""
    from . import areas

    searched = list(dict.fromkeys(hit.path for hit in merged if hit.kind != "doc"))
    chosen = areas.first_files(areas.summary(index, view), query, searched, mode == "M2floor", accept)
    if not chosen:
        return merged
    top = max((hit.score for hit in merged), default=0.0)
    first: list[Hit] = []
    moved: set[int] = set()
    for place, path in enumerate(chosen):
        boost = top + (len(chosen) - place)               # kept first, in this order, by any later sort on score
        own = next((n for n, hit in enumerate(merged) if hit.path == path), None)
        if own is not None:                                # its best result goes first; its others stay where they were
            moved.add(own)
            first.append(replace(merged[own], score=boost, sources=(*merged[own].sources, "module")))
            continue
        oid, family = view.files[path], filters.family(path)
        for (chunk_id, _, _, _, start, end, scope_start, scope_end, chunk_kind, symbol, signature, breadcrumb, text) in \
                index.conn.execute(columns + "WHERE bc.blob_oid = ? AND bc.family = ? AND bc.ord = 0", (oid, family)):
            first.append(Hit(path, start, end, chunk_kind, symbol, signature, breadcrumb, text, boost, ("module",),
                             scope_start or start, scope_end or end))
    return first + [hit for n, hit in enumerate(merged) if n not in moved]


def search(index: Index, view: View, query: str, k: int = 8, kind: str = "any",
           path_glob: str | None = None, families: frozenset[str] | None = None,
           dense: Callable[[int, bool], list[int]] | None = None, more_files: list[Hit] | None = None,
           areas: bool | None = None) -> list[Hit]:
    """Hits for `query` in `view`. Call `index.sync(view)` first: only blobs of the
    synced view can be returned. `dense(limit, scoped)` gives the chunks whose vectors
    are nearest the query's, best first; without it the search is by words and names."""
    if kind not in ("any", "code", "docs"):
        raise ValueError(f"kind must be 'any', 'code' or 'docs', not {kind!r}")
    if not index.serves(view):
        raise ValueError("this view is not the one the index was last synced to; call index.sync(view) first")
    terms = lexical.query_terms(query)
    if not terms:
        return []  # nothing to look for: with vectors too, an empty query has no nearest neighbours worth showing
    # Filters narrow the candidates before ranking: applied afterwards, a filter that
    # few chunks pass would find nothing among the best fifty of everything.
    accept = _path_filter(path_glob, families)
    scoped = kind != "any" or accept is not None
    if scoped:
        index.narrow(view, kind, accept, (path_glob, families))
    pool = max(MIN_POOL, k * 6)
    named, bare_name = symbol_ranking(index, query, scoped)
    rankings = {
        "lexical": [chunk_id for chunk_id, _ in lexical.search(index.conn, query, pool, scoped=scoped)],
        "symbol": named,
    }
    if dense is not None:
        rankings["dense"] = dense(pool, scoped)
    fused = rrf(rankings)[:pool]
    if bare_name and named:
        # The query is one name: its definitions come first, in the order the lookup gives
        # them (exact spelling, kind, code before tests). Rank fusion cannot keep that order,
        # since first and second place differ by 1/61 against 1/62.
        order = {chunk_id: position for position, chunk_id in enumerate(named)}
        fused.sort(key=lambda item: (order.get(item[0], len(order)),))
        top = fused[0][1] if fused else 0.0
        fused = [(chunk_id, max(score, top) + (len(order) - order[chunk_id]) if chunk_id in order else score, sources)
                 for chunk_id, score, sources in fused]
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
    voted: dict[tuple, int] = {}        # with PATH_VOTE "found": the file -> the chunk found in it that its path votes for
    if PATH_VOTE == "found":
        for row in rows:
            key = (row[1], row[2])
            if key in by_file and (key not in voted or by_chunk[row[0]][0] > by_chunk[voted[key]][0]):
                voted[key] = row[0]
    for oid, family in by_file:
        if (oid, family) in voted:
            continue                    # the file is among the results already: no need of its first lines
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
        if (oid, family) in by_file and (voted.get((oid, family)) == chunk_id if (oid, family) in voted else ordinal == 0):
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
        files = list(dict.fromkeys(hit.path for hit in same))
        listed = set(files[:COPIES_SHOWN])
        shown = [hit for hit in same if hit.path in listed]
        if len(files) > len(listed):
            shown[0] = replace(shown[0], duplicates=len(files) - len(listed))
        hits += shown
    hits.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
    merged = merge_pieces(hits)
    # The module step: the engine says whether by the project's main language (areas.decide); without a word from it the
    # module's switch, which the benchmarks set. Not for a query that is one name: its definitions come first, as above.
    mode = AREAS if areas is None else ("M2floor" if areas else "off")
    if mode != "off" and kind != "docs" and not (bare_name and named):
        merged = _areas_first(index, view, query, merged, accept, columns, mode)
    found = arrange(one_a_file(merged) if ONE_A_FILE else merged, k, kind)
    if more_files is not None:
        # Ten results named about seven files: the next files of the same ranking follow them, as places only, until
        # k files are named. Files named among ten: +3.4 on Python's kept questions, the results' code untouched.
        named = set(hit.path for hit in found)
        for hit in merged:
            if len(named) >= k:
                break
            if hit.path not in named:
                named.add(hit.path)
                more_files.append(hit)
    return found
