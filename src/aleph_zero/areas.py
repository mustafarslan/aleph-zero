"""Summaries of a repository's modules, made without a model (DEFERRED entry 0-graph, parts 14 and 17).

A module is a directory of source code. Its summary is the words of what its files define (names and the first line of
each signature) and state (routes, models, environment names: the facts), and of the files' paths. A question about a
whole area ("how is a recipe imported from a URL") names the module's words more than any one line's, so search places
the files of the modules that match it best first.

Every word comes from what the index already keeps per file content (its symbols and its facts), so nothing is read
from disk and nothing goes stale: a file's bag of words is made once for its content and kept in this process, and a
changed file is a new content, read when next asked for. A module's summary is the sum of its files' bags in the view.
"""
from __future__ import annotations

import collections
import math
import threading
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import archfacts, filters
from .archfacts import NEVER_AN_ANSWER, words_of
from .indexer import Index
from .view import View

CODE = frozenset({"python", "typescript", "tsx", "javascript", "go", "rust", "java", "c", "cpp"})
MODULES = 2                  # the modules whose files are placed first
FILES_A_MODULE = 3           # files of each
SEARCHED = 50                # a file's place in search counts within search's first fifty files
K1, B = 1.2, 0.75
_BATCH = 500
_KEPT_VIEWS = 4

# By a project's main language, as vectors are decided (DEFERRED 0-graph part 17): on where it was measured to help.
VERDICTS = {
    "python": (True, "on for Python: +9.2 points of files found on 72 questions about whole areas, +2.5 among ten on 595 "
                     "Python issues, the first file right not lowered within the noise"),
}
NOT_SHOWN = "off: not shown to help for this language (DEFERRED 0-graph part 17)"


def decide(language: str | None) -> tuple[bool, str]:
    """Whether search places the best modules' files first in a project of this main language, and why."""
    return VERDICTS.get(language or "", (False, NOT_SHOWN))


_lock = threading.Lock()
_bags: dict[str, dict[tuple[str, str], collections.Counter]] = {}     # by index file: (blob, family) -> its words
_views: collections.OrderedDict = collections.OrderedDict()            # (index file, view) -> Summary
_paths: dict[str, dict[str, tuple]] = {}      # by index file: path -> (its family, its module, its path's words), or None
_last: dict[str, "Summary"] = {}              # by index file: the summary made last, which the next one starts from


@dataclass
class Summary:
    modules: dict[str, collections.Counter]
    files: dict[str, collections.Counter]                   # path -> its words (its bag and its path's)
    members: dict[str, list[str]] = field(default_factory=dict)   # module -> its files
    contents: dict[str, tuple[str, str]] = field(default_factory=dict)   # path -> the content its words were made from

    def __post_init__(self) -> None:
        self._modules = _Ranking(self.modules)
        self._file_ranking: _Ranking | None = None          # made when a question first picks a module

    @property
    def _files(self) -> "_Ranking":
        if self._file_ranking is None:
            self._file_ranking = _Ranking(self.files)
        return self._file_ranking

    def best_modules(self, words: set[str], floor: bool) -> list[str]:
        ranked = [module for module, _ in self._modules.rank(words)]
        if floor:                # part 17's second arm: a module that shares two of the question's words or more
            ranked = [module for module in ranked if len(words & self.modules[module].keys()) >= 2]
        return ranked[:MODULES]

    def file_order(self, words: set[str]) -> dict[str, int]:
        return {path: place for place, (path, _) in enumerate(self._files.rank(words))}


class _Ranking:
    """BM25 over bags of words."""

    def __init__(self, docs: dict[str, collections.Counter]) -> None:
        self.docs = docs
        self.sizes = {key: sum(counts.values()) for key, counts in docs.items()}
        self.average = sum(self.sizes.values()) / max(1, len(docs))
        self.frequency = collections.Counter(word for counts in docs.values() for word in counts)

    def rank(self, words: set[str]) -> list[tuple[str, float]]:
        n = len(self.docs)
        out = []
        for key, counts in self.docs.items():
            score = 0.0
            for word in words & counts.keys():
                idf = math.log(1 + (n - self.frequency[word] + 0.5) / (self.frequency[word] + 0.5))
                tf = counts[word]
                score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.sizes[key] / max(1.0, self.average)))
            if score > 0:
                out.append((key, score))
        return sorted(out, key=lambda pair: (-pair[1], pair[0]))


def _index_key(index: Index) -> str:
    return str(index.conn.filename)


def _read(index: Index, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], collections.Counter]:
    """The bags of these contents, from the symbols and facts the index keeps for them."""
    bags = {key: collections.Counter() for key in wanted}
    for start in range(0, len(wanted), _BATCH):
        part = wanted[start:start + _BATCH]
        oids = sorted({oid for oid, _ in part})
        marks = ",".join("?" * len(oids))
        for oid, family, name, qualified, signature in index.conn.execute(
                f"SELECT blob_oid, family, name, qualified, signature FROM symbols WHERE nested = 0 AND blob_oid IN ({marks})", oids):
            if (oid, family) in bags:
                bags[(oid, family)].update(words_of(" ".join(filter(None, (name, qualified, (signature or "").split("\n")[0])))))
        for oid, family, stored in index.conn.execute(
                f"SELECT blob_oid, family, facts FROM arch_facts WHERE version = ? AND length(facts) > 0 AND blob_oid IN ({marks})",
                (archfacts.FACTS_VERSION, *oids)):
            if (oid, family) in bags:
                for kind, name, _, detail in archfacts.unpack(stored):
                    bags[(oid, family)].update(words_of(f"{name} {detail}"))
    return bags


def _about(index_key: str, path: str) -> tuple | None:
    """What a path is to the summaries, kept by path (paths rarely change): (family, module, its words), or None."""
    known = _paths.setdefault(index_key, {})
    if path not in known:
        family = filters.family(path)
        known[path] = None if NEVER_AN_ANSWER.search(path) or family not in CODE else (
            family, str(PurePosixPath(path).parent), collections.Counter(words_of(path)))
    return known[path]


def summary(index: Index, view: View) -> Summary:
    """The view's module summaries. A file's words are made once for its content; after an edit only the modules of the
    files that changed are summed again, from the summary made last."""
    key = (_index_key(index), view.fingerprint)
    with _lock:
        if key in _views:
            _views.move_to_end(key)
            return _views[key]
        kept = _bags.setdefault(key[0], {})
        last = _last.get(key[0])
    about = {path: found for path in view.files if (found := _about(key[0], path)) is not None}
    contents = {path: (view.files[path], family) for path, (family, _, _) in about.items()}
    missing = sorted({content for content in contents.values() if content not in kept})
    if missing:
        read = _read(index, missing)
        with _lock:
            kept.update(read)
    previous = last.contents if last is not None else {}
    changed = {path for path in contents.keys() | previous.keys() if contents.get(path) != previous.get(path)}
    files: dict[str, collections.Counter] = {}
    members: dict[str, list[str]] = {}
    for path, content in contents.items():
        if last is not None and path not in changed and path in last.files:
            words = last.files[path]
        else:
            bag = kept.get(content)
            words = bag + about[path][2] if bag else None       # a file that defines and states nothing is in no summary
        if words:
            files[path] = words
            members.setdefault(about[path][1], []).append(path)
    touched = {str(PurePosixPath(path).parent) for path in changed}
    modules: dict[str, collections.Counter] = {}
    for module, inside in members.items():
        if last is not None and module not in touched and module in last.modules:
            modules[module] = last.modules[module]
        else:
            total = collections.Counter()
            for path in inside:
                total.update(files[path])
            modules[module] = total
    made = Summary(modules, files, members, contents)
    with _lock:
        _views[key] = made
        _last[key[0]] = made
        while len(_views) > _KEPT_VIEWS:
            _views.popitem(last=False)
    return made


def first_files(made: Summary, question: str, searched: list[str], floor: bool,
                accept=None) -> list[str]:
    """The files placed before search's others: the best modules' files, FILES_A_MODULE each, in search's own order (its
    first SEARCHED files), then by their own words."""
    words = words_of(question)
    best = made.best_modules(words, floor)
    if not best:
        return []
    place = {path: n for n, path in enumerate(searched[:SEARCHED])}
    own = made.file_order(words)
    out: list[str] = []
    for module in best:
        inside = [path for path in made.members.get(module, []) if accept is None or accept(path)]
        inside.sort(key=lambda path: (place.get(path, SEARCHED), own.get(path, 10 ** 6), path))
        out += [path for path in inside[:FILES_A_MODULE] if path not in out]
    return out
