"""The code graph: which definition a call reaches, and how sure that is.

A call site says only what is written (`lang/calls.py`). This module answers it for a set
of files, from certain to guessed, and every answer carries how it was found (ADR-003):

- level L0, provenance `syntactic`: the callee is in the same file: a name of the file, a
  method of the class the call sits in.
- level L1, provenance `import-resolved`: the file's imports, its package or the class's
  bases lead to a file of the view that declares it.
- level L1, provenance `name`: nothing better is known; every definition of that name is a
  candidate and the confidence is one over their number.

An answer with one target is "confident". Three things get no answer at all, because any
answer would be a guess that looks like knowledge: a call on a name the function binds
itself (a parameter called `filter` is not the project's `filter`), a call through an
import that leads outside the view (`os.path.join` is not this repository's `join`), and a
call on the method's own object that neither its class nor its bases declare.

Nothing here reads files or the index: it works on facts per file handed to it, so it
serves a view built from the index and a corpus read from disk alike.

The order of the steps and the rules about qualified calls follow oxn's resolver
(`resolve/symbols.py`, v0.1.2, MIT, same author), where each was measured.
"""
from __future__ import annotations

import json
import re
import tomllib
from collections import OrderedDict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import NamedTuple

from .lang.bindings import Binding, named_type
from .lang.calls import Call
from .lang.definitions import Definition

CANDIDATE_LIMIT = 20
HOPS = 6
FILES_KEPT = 500            # records of files a graph keeps loaded (some 30 KB each); the least recently used go first
# The three extensions over oxn's resolver, switchable so each can be measured apart (ADR-003).
FEATURES = frozenset({"reexports", "packages", "receivers"})
MANIFESTS = ("go.mod", "Cargo.toml", "package.json", "tsconfig.json", "tsconfig.base.json")

_TYPES = frozenset({"class", "interface", "enum", "type"})
_MEMBERS = frozenset({"method", "field"})
_UNNAMED = frozenset({"namespace"})
_VALUES = frozenset({"constant", "variable"})
_ECMA = frozenset({"typescript", "tsx", "javascript"})
_IMPLICIT_THIS = frozenset({"java", "cpp"})
_OVERLOADING = frozenset({"java", "cpp"})         # one name, several methods, told apart by their arguments
_TOLD_BY_COUNT = frozenset({"java"})              # ... where the number of arguments says which: C++ has defaults
_PACKAGE_WIDE_TYPES = frozenset({"go", "rust"})   # a type's methods may be declared in another file
_ECMA_SUFFIXES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_RUST_ROOTS = ("lib.rs", "main.rs")
_SEGMENT = re.compile(r"^[A-Za-z_$][\w$]*$")
_UNSTATED = "unstated"      # the source does not say what type a value has: the name is all there is to go on
_PAST_OUTSIDE = "inherited past a base that is not among these files, which may declare it too"
_OUTSIDE = ""               # the file of a base that could not be placed: a stand-in kept in a lookup order


class Defined(NamedTuple):
    """A definition as the graph needs it: what `lang.definitions.Definition` holds of it, or a row of
    the index's symbols (which does not record where a definition ends: `end_line` is then its line)."""
    kind: str
    name: str
    qualified: str
    signature: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class FileFacts:
    family: str                         # the language profile's name
    definitions: Sequence[Definition | Defined]   # in source order: a call names its caller by position
    bindings: list[Binding] = field(default_factory=list)
    bases: list[tuple[str, str]] = field(default_factory=list)
    fields: list[tuple[str, str, str]] = field(default_factory=list)   # (type, field, the field's type as written)


class Target(NamedTuple):
    path: str
    name: str
    qualified: str
    kind: str
    start_line: int
    end_line: int


class Source:
    """Where a graph gets its facts. A graph asks for one file at a time, as a question reaches it, so a
    view of a hundred thousand files costs what the question touches and not what the view holds."""

    def paths(self) -> Collection[str]:
        """Every code file there is: asked whether a path exists, and listed by directory."""
        raise NotImplementedError

    def facts(self, path: str) -> FileFacts | None:
        raise NotImplementedError

    def named(self, group: str, name: str) -> list[Target]:
        """Every definition of that exact name in the languages of `group`."""
        raise NotImplementedError

    def type_files(self, group: str, simple: str) -> list[str]:
        """The files that may declare a part of a type of that name (a base, a field): a superset is fine."""
        raise NotImplementedError

    def signature(self, target: Target) -> str:
        raise NotImplementedError


class InMemory(Source):
    """Facts handed over whole: the tests, and the grader's corpora read from disk."""

    def __init__(self, files: dict[str, FileFacts]) -> None:
        self._files = files
        self._named: dict[tuple[str, str], list[Target]] = {}
        self._types: dict[tuple[str, str], dict[str, None]] = {}
        for path, facts in files.items():
            group = _group(facts.family)
            for definition in facts.definitions:
                if definition.kind not in _UNNAMED:
                    self._named.setdefault((group, definition.name), []).append(Target(
                        path, definition.name, definition.qualified, definition.kind, definition.start_line, definition.end_line))
            for owner in [kind for kind, _ in facts.bases] + [kind for kind, _, _ in facts.fields]:
                self._types.setdefault((group, owner.rsplit(".", 1)[-1]), {})[path] = None

    def paths(self) -> Collection[str]:
        return self._files.keys()

    def facts(self, path: str) -> FileFacts | None:
        return self._files.get(path)

    def named(self, group: str, name: str) -> list[Target]:
        return self._named.get((group, name), [])

    def type_files(self, group: str, simple: str) -> list[str]:
        return list(self._types.get((group, simple), ()))

    def signature(self, target: Target) -> str:
        return next((definition.signature for definition in self._files[target.path].definitions
                     if definition.qualified == target.qualified and definition.start_line == target.start_line), "")


class _File:
    """One file's facts, indexed for lookup. Built when a question first reaches the file."""
    __slots__ = ("family", "definitions", "bindings", "top", "members", "kinds", "bound", "wild", "published", "package",
                 "bases", "fields", "imported")

    def __init__(self, path: str, facts: FileFacts) -> None:
        self.family, self.bindings = facts.family, facts.bindings
        # Every definition, in the file's order (a call names its caller by position), as the one record kept of it.
        self.definitions = [Target(path, d.name, d.qualified, d.kind, d.start_line, d.end_line) for d in facts.definitions]
        self.top: dict[str, list[Target]] = {}          # name -> the file's own names
        self.members: dict[str, list[Target]] = {}      # 'Owner.name' -> what an owner holds
        self.kinds: dict[str, str] = {}                 # qualified -> kind
        self.bound: dict[str, list[Binding]] = {}       # local name -> its import bindings
        self.wild: list[Binding] = []                   # imports of everything a module has
        self.published: dict[str, list[Binding]] = {}   # a name it publishes -> its own name for it
        self.package = ""                               # the package it says it belongs to
        self.bases: dict[str, list[str]] = {}           # type -> what it is built on, as written
        self.fields: dict[str, dict[str, str]] = {}     # type -> field -> its type as written
        self.imported = frozenset(binding.local for binding in facts.bindings if binding.kind == "import")
        required = self.imported if facts.family in _ECMA else frozenset()
        for target in self.definitions:
            if target.kind in _VALUES and target.name in required and "." not in target.qualified:
                continue    # `const Thing = require('./thing')` declares nothing: the name is the import
            self.kinds[target.qualified] = target.kind
            if target.kind in _UNNAMED:
                continue
            holder, key = (self.members, target.qualified) if "." in target.qualified else (self.top, target.name)
            holder.setdefault(key, []).append(target)
        for binding in facts.bindings:
            if binding.kind == "package":
                self.package = binding.local
            elif binding.kind == "export":
                self.published.setdefault(binding.local, []).append(binding)
            elif binding.local == "*":
                self.wild.append(binding)
            else:
                self.bound.setdefault(binding.local, []).append(binding)
        for kind, base in facts.bases:
            self.bases.setdefault(kind, []).append(base)
        for kind, name, stated in facts.fields:
            self.fields.setdefault(kind, {}).setdefault(name, stated)


class _PerFile:
    """`graph._top[path]` and its like: one attribute of each file's record, read as a table by path."""

    def __init__(self, graph: Graph, attribute: str, empty) -> None:
        self._graph, self._attribute, self._empty = graph, attribute, empty

    def get(self, path: str, default=None):
        file = self._graph._of(path)
        return getattr(file, self._attribute) if file is not None else default

    def __getitem__(self, path: str):
        file = self._graph._of(path)
        return getattr(file, self._attribute) if file is not None else self._empty()

    def __contains__(self, path: str) -> bool:
        return self._graph._of(path) is not None


class _Files:
    """`graph.files`: the files a graph knows, each loaded when asked for."""

    def __init__(self, graph: Graph, paths: Collection[str]) -> None:
        self._graph, self._paths = graph, paths

    def __contains__(self, path: object) -> bool:
        return path in self._paths

    def __iter__(self):
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)

    def get(self, path: str, default=None):
        file = self._graph._of(path)
        return file if file is not None else default

    def __getitem__(self, path: str) -> _File:
        file = self._graph._of(path)
        if file is None:
            raise KeyError(path)
        return file


class _Lookup:
    """A table the graph reads with `.get(key, default)`, answered by a function."""

    def __init__(self, answer) -> None:
        self._answer = answer

    def get(self, key, default=None):
        found = self._answer(key)
        return found if found else default


@dataclass(frozen=True)
class Edge:
    targets: tuple[Target, ...]
    level: str          # 'L0' | 'L1'
    provenance: str     # 'syntactic' | 'import-resolved' | 'name'
    rule: str = ""      # which step answered: for reports, decides nothing
    doubt: str = ""     # why a lone target is still not certain, when the reason is not its name: see `certain`

    @property
    def confidence(self) -> float:
        return 1.0 / len(self.targets) if self.targets else 0.0

    @property
    def certain(self) -> bool:
        """One target, reached by what the code says and not by its name alone. A lone candidate found by
        name is not certain: the receiver's type is unknown, and a library's method of that name would not
        be among the candidates at all (on ripgrep, three of four such answers at library calls were wrong).
        Nor is a method inherited past a base that is not among these files: that base comes first, and may
        declare the method itself."""
        return len(self.targets) == 1 and self.provenance != "name" and not self.doubt


class Graph:
    def __init__(self, source: Source | dict[str, FileFacts], manifests: dict[str, bytes] | None = None,
                 features: frozenset[str] | set[str] = FEATURES) -> None:
        self.source = InMemory(source) if isinstance(source, dict) else source
        self.features = frozenset(features)
        paths = self.source.paths()
        self.files = _Files(self, paths)
        self._loaded: OrderedDict[str, _File | None] = OrderedDict()   # the files questions have reached, most recent last
        self._top = _PerFile(self, "top", dict)             # path -> name -> the file's own names
        self._members = _PerFile(self, "members", dict)     # path -> 'Owner.name' -> what an owner holds
        self._kinds = _PerFile(self, "kinds", dict)         # path -> qualified -> kind
        self._bound = _PerFile(self, "bound", dict)         # path -> local name -> its import bindings
        self._wild = _PerFile(self, "wild", list)           # path -> imports of everything a module has
        self._published = _PerFile(self, "published", dict)  # path -> a name it publishes -> its own name for it
        self._package = _PerFile(self, "package", str)      # path -> the package it says it belongs to
        self._named = _Lookup(lambda key: self.source.named(*key))              # (language group, name) -> every definition
        self._methods = _Lookup(self._methods_named)                            # (language group, 'Type.name') -> wherever declared
        self._packages = _Lookup(self._package_files)                           # (language group, package) -> its files
        self._bases = _Lookup(lambda key: (self._of(key[0]) or _NO_FILE).bases.get(key[1]))        # (path, type) -> its bases
        self._fields = _Lookup(lambda key: (self._of(key[0]) or _NO_FILE).fields.get(key[1]))      # (path, type) -> field -> type
        self._bases_named = _Lookup(self._bases_of_name)    # (group, simple type) -> (path, base), wherever declared
        self._fields_named = _Lookup(self._fields_of_name)  # (group, simple type) -> (path, field, type)
        self._python = _Lookup(self._python_rooted)         # 'a/b.py' -> (file, the directory it is rooted in)
        self._dirs: dict[str, list[str]] = {}               # directory -> its files
        for path in paths:
            self._dirs.setdefault(_parent(path), []).append(path)
        self._basenames: dict[str, list[str]] | None = None  # a file's own name -> the files called so; built when first needed
        self._java_packages: dict[str, list[str]] = {}
        self._rooted: dict[str, list[tuple[str, str]]] = {}  # what `_python_rooted` has answered
        self._orders: dict[tuple[str, str], list[tuple[str, str]]] = {}   # Python classes' method resolution orders
        self._doubt = ""                                    # set while one call is answered: see `Edge.doubt`
        self._go_names: dict[str, str] = {}                 # a Go import path -> the name its package gives itself
        self._read_layout(manifests or {})

    # ---- building -----------------------------------------------------------------------------

    def facts(self, path: str) -> _File | None:
        """What the graph knows of one file: its family, definitions and bindings."""
        return self._of(path)

    def _of(self, path: str) -> _File | None:
        """A file's record, loaded when first asked for and kept while recently used."""
        loaded = self._loaded
        if path in loaded:
            loaded.move_to_end(path)
            return loaded[path]
        facts = self.source.facts(path) if path in self.files else None
        loaded[path] = _File(path, facts) if facts is not None else None
        if len(loaded) > FILES_KEPT:
            loaded.popitem(last=False)
        return loaded[path]

    def _by_basename(self, name: str) -> list[str]:
        if self._basenames is None:
            self._basenames = {}
            for path in self.files:
                self._basenames.setdefault(path.rsplit("/", 1)[-1], []).append(path)
        return self._basenames.get(name, [])

    def _methods_named(self, key: tuple[str, str]) -> list[Target]:
        group, dotted = key
        return [target for target in self.source.named(group, dotted.rsplit(".", 1)[-1])
                if target.qualified == dotted or target.qualified.endswith("." + dotted)]

    def _package_files(self, key: tuple[str, str]) -> list[str]:
        """The files of a Java package: those in a directory spelled like it that declare it."""
        family, package = key
        if family != "java":
            return []
        if package not in self._java_packages:
            tail = package.replace(".", "/")
            self._java_packages[package] = [
                path for directory, held in self._dirs.items() if directory == tail or directory.endswith("/" + tail)
                for path in held if path.endswith(".java") and self._package.get(path, "") == package]
        return self._java_packages[package]

    def _bases_of_name(self, key: tuple[str, str]) -> list[tuple[str, str]]:
        group, simple = key
        return [(path, base) for path in self.source.type_files(group, simple)
                for owner, built_on in (self._of(path) or _NO_FILE).bases.items() if owner.rsplit(".", 1)[-1] == simple
                for base in built_on]

    def _fields_of_name(self, key: tuple[str, str]) -> list[tuple[str, str, str]]:
        group, simple = key
        return [(path, name, stated) for path in self.source.type_files(group, simple)
                for owner, held in (self._of(path) or _NO_FILE).fields.items() if owner.rsplit(".", 1)[-1] == simple
                for name, stated in held.items()]

    def _python_rooted(self, suffix: str) -> list[tuple[str, str]]:
        """The Python files whose path ends in `suffix` (`a/b.py`), each with the directory that leaves."""
        if suffix not in self._rooted:
            if len(self._rooted) > 4000:
                self._rooted.clear()
            found = []
            for path in self._by_basename(suffix.rsplit("/", 1)[-1]):   # hundreds of files are called __init__.py
                if path == suffix:
                    found.append((path, ""))
                elif path.endswith("/" + suffix):
                    found.append((path, path[:-len(suffix) - 1]))
            self._rooted[suffix] = found
        return self._rooted[suffix]

    def _go_bound(self, path: str, name: str) -> list[Binding]:
        """A Go import binds the package's own name, which is the last segment of its path only by convention:
        the unaliased imports of `path` whose package calls itself `name`."""
        file = self._of(path)
        if file is None or file.family != "go":
            return []
        found = []
        for local, bindings in file.bound.items():
            for binding in bindings:
                if binding.name or local != _conventional(binding.source):
                    continue    # not a package import, or one the file named itself
                if binding.source not in self._go_names:
                    placed = self._place_go(binding.source)
                    self._go_names[binding.source] = self._package.get(placed[0], "") if placed else ""
                if self._go_names[binding.source] == name:
                    found.append(binding)
        return found

    def _read_layout(self, manifests: dict[str, bytes]) -> None:
        """What the repository's manifests say about where packages live."""
        self._go_modules: list[tuple[str, str]] = []            # (module path, its directory), longest path first
        self._crates: dict[str, str] = {}                       # crate name as code writes it -> its root file
        self._crate_dirs: list[str] = []                        # directories holding a Cargo.toml, deepest first
        self._node_packages: dict[str, tuple[str, list[str]]] = {}  # package name -> (directory, entry files named by it)
        self._aliases: list[tuple[str, str, str, list[str]]] = []  # (config dir, before '*', after '*', targets)
        for path, raw in sorted(manifests.items()):
            here, name, text = _parent(path), path.rsplit("/", 1)[-1], raw.decode("utf-8", "replace")
            try:
                if name == "go.mod":
                    module = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
                    if module:
                        self._go_modules.append((module.group(1).strip('"'), here))
                elif name == "Cargo.toml":
                    self._crate_dirs.append(here)
                    manifest = tomllib.loads(text)
                    package, library = manifest.get("package") or {}, manifest.get("lib") or {}
                    crate = str(library.get("name") or package.get("name") or "").replace("-", "_")
                    root = _join(here, str(library.get("path") or "src/lib.rs"))
                    if crate and root in self.files:
                        self._crates[crate] = root
                elif name == "package.json":
                    manifest = json.loads(text)
                    if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
                        entries = [str(manifest[key]) for key in ("source", "module", "main", "types", "typings")
                                   if isinstance(manifest.get(key), str)]
                        self._node_packages[manifest["name"]] = (here, entries)
                elif name.startswith("tsconfig"):
                    options = (json.loads(_without_comments(text)) or {}).get("compilerOptions") or {}
                    base = _join(here, str(options.get("baseUrl") or "."))
                    for pattern, targets in (options.get("paths") or {}).items():
                        before, star, after = pattern.partition("*")
                        self._aliases.append((here, before, after if star else "\0", [_join(base, str(t)) for t in targets]))
            except (ValueError, TypeError, AttributeError):
                continue    # a manifest that cannot be read tells nothing; the imports it would place stay unplaced
        self._go_modules.sort(key=lambda item: -len(item[0]))
        self._crate_dirs.sort(key=lambda item: -len(item))
        if "packages" not in self.features:     # one package at the root, as if nothing else were declared
            self._go_modules = [item for item in self._go_modules if item[1] == ""]
            self._crates, self._node_packages, self._aliases = {}, {}, []

    # ---- the answer ---------------------------------------------------------------------------

    def resolve(self, path: str, call: Call) -> Edge | None:
        """The definition(s) `call`, written in `path`, may reach; None when nothing in these files fits."""
        facts = self.files.get(path)
        if facts is None:
            return None
        outer, self._doubt = self._doubt, ""     # answering one call may ask about another (what built a value)
        try:
            if not call.via:
                found = self._plain(path, facts, call)
            elif call.head in ("self", "super"):
                found = self._on_self(path, facts, call)
            else:
                found = self._qualified(path, facts, call)
            if found is not None and self._doubt and found.provenance != "name":
                found = replace(found, doubt=self._doubt)
            return found
        finally:
            self._doubt = outer

    def _plain(self, path: str, facts: FileFacts, call: Call) -> Edge | None:
        """`helper()`: the file's own scope, then its imports, then its package, then the name alone."""
        name = call.name
        if call.head in ("inner", "inner-var"):
            # A function defined in a function around the call: the reader found it where the name is bound.
            # It is nobody's but that function's, so it is a target here and a definition nowhere else.
            holder = facts.definitions[call.caller].qualified if call.caller is not None else ""
            targets = tuple(Target(path, name, f"{holder}.{name}" if holder else name, "function", first, last)
                            for first, last in call.inner)
            if call.head == "inner-var":
                self._doubt = self._doubt or "the name is assigned again somewhere in the file"
            return Edge(targets, "L0", "syntactic", "inner function") if targets else None
        if call.head == "local":
            return None     # a parameter or a local variable: nothing recorded has that name
        here = self._lexical(path, facts, call)
        if here:
            return self._edge(path, here, "scope")
        bound = self._bound[path].get(name)
        if bound:
            reached, imported = False, []
            for binding in bound:       # usually one; a name imported twice as two things is both of them
                targets, modules, placed = self._stands_for(path, binding)
                reached = reached or placed
                if not targets and modules and facts.family in _ECMA:   # `const Linter = require('./linter'); new Linter()`
                    targets = [found for module in modules for found in self._exported(module, "default")[0]]
                imported += targets
            if imported:
                return self._edge(path, imported, "import")
            if not reached:
                return None     # imported from outside these files: whatever shares its name here is a coincidence
            return self._by_name(path, facts, call, members=False, rule="name, import not followed")
        nearby = self._nearby(path, facts, name)
        if nearby:
            return self._edge(path, nearby, "package")
        return self._by_name(path, facts, call, members=False, rule="name, plain")

    def _on_self(self, path: str, facts: FileFacts, call: Call) -> Edge | None:
        """`self.step()`, `this.step()`, `super().step()`: the class the call sits in, then what it is built on."""
        owner = self._owner(facts, call)
        if owner is None:
            return self._by_name(path, facts, call, members=True, rule="name, on self outside a type")
        segments = _segments(call.receiver)
        if len(segments or ("", "")) != 1 and not call.receiver.startswith("super("):      # `self.store.save()`
            if segments is not None and "receivers" in self.features:
                typed = self._through(path, [(path, owner)], list(segments[1:]), call.name, call.arity)
                if typed is not _UNSTATED:
                    return typed
            return self._by_name(path, facts, call, members=True, rule="name, on a field")
        found = self._method((path, owner), call.name, above=call.head == "super", arity=call.arity)
        return self._edge(path, found, "own type") if found else None

    def _qualified(self, path: str, facts: FileFacts, call: Call) -> Edge | None:
        """`a.f()` and `a::f()` where `a` is not the method's own object."""
        segments = _segments(call.receiver)
        if call.head == "local" and call.receiver_type and segments is not None and "receivers" in self.features:
            kinds = self._value_type(path, call.receiver_type, call.caller)
            typed = (_UNSTATED if kinds is None else
                     self._through(path, kinds, list(segments[1:]), call.name, call.arity) if kinds else None)
            if typed is not _UNSTATED:
                return typed
        if call.head in ("local", "value") or segments is None:
            return self._by_name(path, facts, call, members=True, rule="name, on a value")
        head, rest, name = segments[0], list(segments[1:]), call.name
        dotted = ".".join(segments)
        local_type = [qualified for qualified, kind in self._kinds[path].items()
                      if kind not in _MEMBERS and (qualified == dotted or qualified.endswith("." + dotted))]
        if local_type:      # `Type.method()`, or an object of this file: `handlers.run()`
            found = [target for qualified in local_type for target in self._method((path, qualified), name, arity=call.arity)]
            if found:
                return self._edge(path, found, "type in file")
        if facts.family == "rust" and call.via == "path" and (head in ("crate", "self", "super") or head not in self._bound[path]):
            return self._rust_path(path, facts, call, list(segments))
        bound = self._bound[path].get(head) or self._go_bound(path, head)
        if bound:
            reached, found = False, []
            for binding in bound:
                targets, modules, placed = self._stands_for(path, binding)
                reached = reached or placed
                found += self._member_of(targets, modules, rest, name)
            if found:
                return self._edge(path, found, "import")
            if not reached:
                return None     # `np.array()`: the receiver is an import from outside these files
            return self._by_name(path, facts, call, members=True, rule="name, import not followed")
        if local_type:
            return self._by_name(path, facts, call, members=True, rule="name, type in file lacks it")
        owner = self._owner(facts, call)
        if owner and facts.family in _IMPLICIT_THIS and "receivers" in self.features and self._field((path, owner), head):
            typed = self._through(path, [(path, owner)], list(segments), name, call.arity)   # `owners.findAll()` is `this.owners.findAll()`
            if typed is not _UNSTATED:
                return typed
        nearby = self._nearby(path, facts, head)    # a type of the same package, written without an import
        if nearby and not rest:
            found = [target for kind in nearby for target in self._method((kind.path, kind.qualified), name, arity=call.arity)]
            if found:
                return self._edge(path, found, "package")
        return self._by_name(path, facts, call, members=True, rule="name, on an unbound name")

    def _rust_path(self, path: str, facts: FileFacts, call: Call, segments: list[str]) -> Edge | None:
        """`crate::flags::parse()`, `super::check()`, `Self::new()`, `grep_matcher::find()`."""
        if segments[0] == "Self":
            owner = self._owner(facts, call)
            found = self._method((path, owner), call.name) if owner and len(segments) == 1 else []
            return self._edge(path, found, "own type") if found else None
        module, rest = self._rust_module(path, segments, self._inline_depth(path, facts, call))
        if module is None:
            return None     # `Vec::new()`, `std::fs::read()`: a path that leaves these files
        if not rest and module == path and segments[-1] != "crate":    # `self::f()`, `super::f()` from an inline module
            here = self._top[path].get(call.name)
            if here:
                return self._edge(path, here, "scope")
        found = self._member_of([], [module], rest, call.name)
        return self._edge(path, found, "module path") if found else None

    # ---- inside the file ------------------------------------------------------------------------

    def _containers(self, path: str, facts: FileFacts, call: Call) -> list[tuple[str, str]]:
        """(qualified name, kind) of what encloses the call, innermost first."""
        if call.caller is None:
            return []
        parts = facts.definitions[call.caller].qualified.split(".")
        kinds = self._kinds[path]
        return [(".".join(parts[:cut]), kinds.get(".".join(parts[:cut]), "")) for cut in range(len(parts) - 1, 0, -1)]

    def _inline_depth(self, path: str, facts: FileFacts, call: Call) -> int:
        """How many inline Rust modules the call is written inside."""
        return sum(1 for _, kind in self._containers(path, facts, call) if kind == "namespace")

    def _lexical(self, path: str, facts: FileFacts, call: Call) -> list[Target]:
        """A plain name looked up outwards from where the call stands, as the language would."""
        for qualified, kind in self._containers(path, facts, call):
            if kind == "namespace":
                found = self._members[path].get(f"{qualified}.{call.name}")
                if found:
                    return found
            elif kind in _TYPES and facts.family in _IMPLICIT_THIS:     # `helper()` inside a class is `this.helper()`
                found = self._method((path, qualified), call.name, arity=call.arity)
                if found:
                    return found
        return self._top[path].get(call.name, [])

    def _owner(self, facts: FileFacts, call: Call) -> str | None:
        """The type the method around the call belongs to, as the file qualifies it."""
        if call.caller is None:
            return None
        qualified = facts.definitions[call.caller].qualified
        return qualified.rsplit(".", 1)[0] if "." in qualified else None

    # ---- types and their members ----------------------------------------------------------------

    def _method(self, kind: tuple[str, str], name: str, above: bool = False,
                seen: set[tuple[str, str]] | None = None, arity: int = -1) -> list[Target]:
        """`name` as the type `kind` (file, qualified name) has it: its own, else an inherited one, looked
        for in the order the type's language would: Python's method resolution order, Go's shallowest
        embedding, elsewhere the first base that has it. `above` skips the type itself (`super()`).
        Where a language tells methods of one name apart by their arguments, `arity` narrows them, and
        what it cannot tell apart is returned together."""
        file = self._of(kind[0])
        family = file.family if file is not None else ""
        if family == "python":
            passed_outside = False
            order = self._linearized(kind)
            for each in (order[1:] if above else order):
                if each[0] == _OUTSIDE:
                    passed_outside = True
                    continue
                own = self._own_members(*each, name)
                if own:
                    self._doubt = _PAST_OUTSIDE if passed_outside else self._doubt
                    return own
            return []
        if family == "go":
            return self._shallowest(kind, name, above)
        seen = seen if seen is not None else set()
        if kind in seen or len(seen) > 64:
            return []
        seen.add(kind)
        path, qualified = kind
        own = [] if above else self._own_members(path, qualified, name)
        if own and family not in _OVERLOADING:
            return own
        fitting = [target for target in own if family not in _TOLD_BY_COUNT or self._fits(target, arity)]
        inherited: list[Target] = []
        passed_outside = False
        for declared_in, base in self._bases_of(path, qualified):
            placed = self._type(declared_in, base)
            passed_outside = passed_outside or not placed
            for found in placed:
                inherited = self._method(found, name, False, seen, arity)
                if inherited:
                    break
            if inherited:
                break
        if not fitting:
            if inherited and passed_outside:
                self._doubt = _PAST_OUTSIDE
            return inherited        # nothing here takes that many arguments (or nothing here has the name at all)
        # An inherited method of the same name that this type does not override takes part in the choice.
        # A signature too long to have been kept whole says nothing: its method may or may not be the override.
        shapes = {self._parameters(target) for target in own}
        if None in shapes:
            return fitting + inherited
        return fitting + [target for target in inherited if self._parameters(target) not in shapes]

    def _linearized(self, kind: tuple[str, str], active: frozenset[tuple[str, str]] = frozenset()) -> list[tuple[str, str]]:
        """Python's method resolution order of a class (C3), over the bases that are classes of these files."""
        if kind in self._orders:
            return self._orders[kind]
        if kind in active or len(active) > 64:
            return [kind]
        bases: list[tuple[str, str]] = []
        for declared_in, base in self._bases_of(*kind):
            found = self._type(declared_in, base)
            placed = found[0] if found else (_OUTSIDE, base)    # kept in the order: what comes after it is inherited past it
            if placed not in bases and placed != kind:
                bases.append(placed)
        sequences = [[base] if base[0] == _OUTSIDE else list(self._linearized(base, active | {kind})) for base in bases] + [list(bases)]
        order = [kind]
        while any(sequences):
            head = next((sequence[0] for sequence in sequences
                         if sequence and not any(sequence[0] in other[1:] for other in sequences)), None)
            if head is None:        # no consistent order exists (the classes would not load): keep what is left as written
                order += [each for sequence in sequences for each in sequence if each not in order]
                break
            order.append(head)
            for sequence in sequences:
                if sequence and sequence[0] == head:
                    del sequence[0]
        if len(self._orders) > 4000:
            self._orders.clear()
        self._orders[kind] = order
        return order

    def _shallowest(self, kind: tuple[str, str], name: str, above: bool) -> list[Target]:
        """Go promotes the method of the embedded type nearest the top; two at one depth are both returned."""
        level, seen = [kind], {kind}
        skip = above
        passed_outside = False      # an embedded type from outside, shallower than where the method is found
        while level and len(seen) <= 64:
            found = [] if skip else [target for each in level for target in self._own_members(*each, name)]
            if found:
                self._doubt = _PAST_OUTSIDE if passed_outside else self._doubt
                return found
            skip = False
            below: list[tuple[str, str]] = []
            outside_here = False
            for each in level:
                for declared_in, base in self._bases_of(*each):
                    placed = self._type(declared_in, base)
                    outside_here = outside_here or not placed
                    for embedded in placed:
                        if embedded not in seen:
                            seen.add(embedded)
                            below.append(embedded)
            # What is found one level further down is deeper than an outside type embedded at this one.
            found_next = [target for each in below for target in self._own_members(*each, name)]
            if found_next:
                self._doubt = _PAST_OUTSIDE if passed_outside else self._doubt
                return found_next
            passed_outside = passed_outside or outside_here
            level, skip = below, True
        return []

    def _parameters(self, target: Target) -> tuple[str, ...] | None:
        """The parameter types a method's signature writes, in order: what tells an override from an overload.
        None when the signature does not show them whole (it is kept only up to a length)."""
        signature = self.source.signature(target)
        named = re.search(rf"(?<![\w$]){re.escape(target.name)}\s*\(", signature)     # not the brackets of an annotation before it
        if named is None:
            return None
        opening = named.end() - 1
        depth, start, parts, closed = 0, opening + 1, [], False
        for index in range(opening, len(signature)):
            char = signature[index]
            if char in "(<[":
                depth += 1
            elif char in ")>]":
                depth -= 1
                if depth == 0:
                    parts.append(signature[start:index])
                    closed = True
                    break
            elif char == "," and depth == 1:
                parts.append(signature[start:index])
                start = index + 1
        if not closed:
            return None
        kinds = []
        for part in parts:
            words = [word for word in re.sub(r"@[\w.]+(\([^)]*\))?", " ", part).split() if word != "final"]
            if words:
                kinds.append(" ".join(words[:-1]) if len(words) > 1 else words[0])
        return tuple(kinds)

    def _fits(self, target: Target, arity: int) -> bool:
        """Whether a call passing `arity` arguments can be a call of this method. Anything that is not a
        method, any call whose arguments are not known, and any method whose parameters are not, fits."""
        if arity < 0 or target.kind != "method":
            return True
        kinds = self._parameters(target)
        if kinds is None:
            return True
        return arity >= len(kinds) - 1 if kinds and kinds[-1].endswith("...") else arity == len(kinds)

    def _own_members(self, path: str, qualified: str, name: str) -> list[Target]:
        own = self._members.get(path, {}).get(f"{qualified}.{name}")
        facts = self.files.get(path)
        if own or facts is None or facts.family not in _PACKAGE_WIDE_TYPES:
            return own or []
        simple = qualified.rsplit(".", 1)[-1]
        spread = self._methods.get((_group(facts.family), f"{simple}.{name}"), [])
        return [target for target in spread if self._same_type(path, qualified, target.path)]

    def _bases_of(self, path: str, qualified: str) -> list[tuple[str, str]]:
        found = [(path, base) for base in self._bases.get((path, qualified), [])]
        facts = self.files.get(path)
        if facts is not None and facts.family in _PACKAGE_WIDE_TYPES:    # `impl Trait for S` in another file of the crate
            simple = qualified.rsplit(".", 1)[-1]
            found += [(other, base) for other, base in self._bases_named.get((_group(facts.family), simple), [])
                      if other != path and self._same_type(path, qualified, other)]
        return found

    def _same_type(self, path: str, qualified: str, other: str) -> bool:
        """Whether a type of this simple name in the file `other` is the type `qualified` of `path`, declared
        in parts. In a Go package a name is one type. A Rust crate may hold several types of one name
        (`Error`, `Builder`), so the other file must be shown to mean this one: by its imports."""
        if not self._same_unit(path, other):
            return False
        if self.files[path].family != "rust":
            return True
        simple = qualified.rsplit(".", 1)[-1]
        mine, theirs = self._type(path, qualified), self._type(other, simple)
        return len(mine) == 1 and theirs == mine

    def _same_unit(self, path: str, other: str) -> bool:
        """Whether two files can declare parts of one type: a Go package, a Rust crate."""
        family = self.files[path].family
        if family == "go":
            return _parent(path) == _parent(other) and self._package.get(path, "") == self._package.get(other, "")
        return self._crate_dir(path) == self._crate_dir(other)

    def _crate_dir(self, path: str) -> str:
        return next((crate for crate in self._crate_dirs if not crate or path.startswith(crate + "/")), "")

    def _type(self, path: str, written: str) -> list[tuple[str, str]]:
        """The types a name written in `path` can stand for, as (file, qualified name)."""
        segments = _segments(written)
        if segments is None or path not in self.files:
            return []
        dotted, head = ".".join(segments), segments[0]
        here = [qualified for qualified, kind in self._kinds[path].items()
                if kind in _TYPES and (qualified == dotted or qualified.endswith("." + dotted))]
        if here:
            return [(path, qualified) for qualified in here]
        facts = self.files[path]
        if self._kinds[path].get(head) in _VALUES:
            return []       # `Store = Cache` at file level: whatever `Store` was imported as, it is something else now
        bound = self._bound[path].get(head) or self._go_bound(path, head)
        if bound:
            found: list[tuple[str, str]] = []
            for binding in bound:
                targets, modules, _ = self._stands_for(path, binding)
                targets, modules = self._walk(targets, modules, list(segments[1:]))
                found += [(target.path, target.qualified) for target in targets if target.kind in _TYPES]
            return list(dict.fromkeys(found))
        if facts.family == "rust" and len(segments) > 1:
            module, rest = self._rust_module(path, list(segments), 0)
            targets, _ = self._walk([], [module], rest) if module is not None else ([], [])
            return [(target.path, target.qualified) for target in targets if target.kind in _TYPES]
        return [(target.path, target.qualified) for target in self._nearby(path, facts, head)
                if target.kind in _TYPES and len(segments) == 1]

    # ---- values of a stated type -----------------------------------------------------------------

    def _through(self, path: str, kinds: list[tuple[str, str]], fields: list[str], name: str,
                 arity: int = -1) -> Edge | None | str:
        """`name` called on a value of one of `kinds`, after going through `fields`: the edge, None when the
        source states a type that is none of these files' (or one that lacks the callee), `_UNSTATED` when a
        field on the way has no stated type."""
        for step in fields:
            below: list[tuple[str, str]] = []
            for kind in kinds:
                stated = self._field(kind, step)
                found = self._value_type(*stated) if stated is not None else None
                if found is None:
                    return _UNSTATED
                below += found
            kinds = below
            if not kinds:
                return None
        return self._edge(path, [target for kind in kinds for target in self._method(kind, name, arity=arity)], "stated type")

    def _field(self, kind: tuple[str, str], name: str, seen: set[tuple[str, str]] | None = None) -> tuple[str, str] | None:
        """(the file that declares it, its type as written) for a field of `kind`, its own or a base's."""
        seen = seen if seen is not None else set()
        if kind in seen or len(seen) > 64:
            return None
        seen.add(kind)
        path, qualified = kind
        own = self._fields.get(kind, {}).get(name)
        if own:
            return path, own
        facts = self.files.get(path)
        if facts is not None and facts.family in _PACKAGE_WIDE_TYPES:
            for other, field_name, stated in self._fields_named.get((_group(facts.family), qualified.rsplit(".", 1)[-1]), []):
                if field_name == name and stated and self._same_type(path, qualified, other):
                    return other, stated
        for declared_in, base in self._bases_of(path, qualified):
            for above in self._type(declared_in, base):
                inherited = self._field(above, name, seen)
                if inherited is not None:
                    return inherited
        return None

    def _value_type(self, path: str, written: str, caller: int | None = None) -> list[tuple[str, str]] | None:
        """The types of these files that a type written in `path` stands for; [] when it is none of them;
        None when it cannot be told (the result of a call that is not certainly a constructor of these files)."""
        if not written.endswith("()"):
            return self._type(path, written)
        segments = _segments(written[:-2])
        if segments is None:
            return None
        joined = "::" if "::" in written else "."
        built = self.resolve(path, Call(segments[-1], joined.join(segments[:-1]), 0, 0, caller,
                                        "" if len(segments) == 1 else "path" if joined == "::" else "member"))
        if built is None:
            head = self._bound.get(path, {}).get(segments[0])
            return [] if head else None     # built by something imported from outside: typed, and not one of ours
        if not built.certain:
            return None
        target = built.targets[0]
        if target.kind in _TYPES:
            return [(target.path, target.qualified)]
        returned = self._returned(target)
        if not returned:
            return None
        if returned == "Self" and "." in target.qualified:
            return [(target.path, target.qualified.rsplit(".", 1)[0])]
        return self._type(target.path, returned)

    def _returned(self, target: Target) -> str:
        """The type a function's own signature says it returns, as a name; '' when it does not say."""
        facts = self.files[target.path]
        signature = self.source.signature(target)
        signature = signature.rstrip(" {:")
        if facts.family in ("python", "rust"):
            _, arrow, stated = signature.rpartition("->")
            stated = stated.split(" where ")[0] if arrow else ""
        elif facts.family in _ECMA:
            closing = signature.rfind(")")
            stated = signature[closing + 1:].lstrip(" :") if closing >= 0 and signature[closing + 1:].lstrip().startswith(":") else ""
        elif facts.family == "go":
            if signature.endswith(")"):                 # (T, error), or no result at all
                opening = _matching_open(signature)
                inside = signature[opening + 1:-1] if opening > 0 and signature[:opening].rstrip().endswith(")") else ""
                stated = inside.split(",")[0].split()[-1] if inside.strip() else ""
            else:
                stated = signature[signature.rfind(")") + 1:]
        elif facts.family == "java":
            before = signature.split("(", 1)[0].split()
            stated = before[-2] if len(before) >= 2 else ""
        else:
            stated = ""
        return named_type(stated) if stated.strip() else ""

    # ---- through imports ------------------------------------------------------------------------

    def _stands_for(self, path: str, binding: Binding, seen: set[tuple[str, str]] | None = None
                    ) -> tuple[list[Target], list[str], bool]:
        """What an import binding of `path` stands for: definitions, or modules (files), and whether its
        source was found among these files at all."""
        family = self.files[path].family
        seen = seen if seen is not None else set()     # one trail per question: imports may lead in circles
        if family == "rust":
            return self._rust_binding(path, binding, seen)
        modules = self._place(path, binding)
        if not binding.name or binding.name == "*":
            return [], modules, bool(modules)
        for module in modules:
            targets, inner = self._exported(module, binding.name, seen)
            if targets or inner:
                return targets, inner, True
        if family == "java" and not modules:        # import static a.b.Type.member
            package, _, owner = binding.source.rpartition(".")
            for file in self._packages.get(("java", package), []):
                found = self._method((file, owner), binding.name) if owner in self._top[file] else []
                if found:
                    return found, [], True
        return [], [], bool(modules)

    def _exported(self, module: str, name: str, seen: set[tuple[str, str]] | None = None) -> tuple[list[Target], list[str]]:
        """What `name` means when taken from the file `module`: definitions, or modules of that name."""
        seen = seen if seen is not None else set()
        if (module, name) in seen or len(seen) > HOPS * 8:
            return [], []
        seen.add((module, name))
        targets, modules, explicit = self._offered(module, name, seen)
        file = self._of(module)
        if "reexports" not in self.features or file is None:
            return targets, modules
        if (targets or modules) and (explicit or file.family not in _ECMA or not file.wild):
            return targets, modules
        # Not named by the module in so many words. What its stars bring counts too: an ECMAScript module
        # that imports `run` for its own use and says `export * from './public'` offers the public one.
        starred, inside = self._starred(module, name, seen)
        if targets and starred and set(starred) != set(targets):
            return targets + starred, modules
        return (targets, modules) if targets or modules else (starred, inside)

    def _offered(self, module: str, name: str, seen: set[tuple[str, str]]) -> tuple[list[Target], list[str], bool]:
        """`name` as the module itself declares, publishes or passes on, and whether it says so explicitly
        (an export of that name, a re-export of that name) rather than merely holding it."""
        found = self._top.get(module, {}).get(name)
        if found:
            return found, [], bool(self._published.get(module, {}).get(name))
        for published in self._published.get(module, {}).get(name, []):     # exports = { name: other }
            targets, modules = self._exported(module, published.name, seen)
            if targets or modules:
                return targets, modules, True
        inside = self._submodule(module, name)
        if inside:
            return [], inside, True
        if "reexports" not in self.features or module not in self.files:
            return [], [], False
        rust = self.files[module].family == "rust"
        for binding in self._bound[module].get(name, []):                   # the module imported it and passes it on
            if binding.level and rust:
                continue    # a `use` inside an inline module is not the file's own
            targets, modules, _ = self._stands_for(module, binding, seen)
            if targets or modules:
                return targets, modules, binding.kind == "reexport"
        return [], [], False

    def _starred(self, module: str, name: str, seen: set[tuple[str, str]]) -> tuple[list[Target], list[str]]:
        """`name` as the modules that `module` takes everything from offer it: export * from, from x import *,
        pub use x::*. Every one that has it, since which one wins is not always the first."""
        file = self._of(module)
        if file is None or file.family == "python" and name.startswith("_"):
            return [], []       # a star import does not bring a name that starts with an underscore
        targets: list[Target] = []
        modules: list[str] = []
        for binding in file.wild:
            if binding.level and file.family == "rust":
                continue
            _, sources, _ = self._stands_for(module, binding, seen)
            for inner in sources:
                found, deeper = self._exported(inner, name, seen)
                targets += [target for target in found if target not in targets]
                modules += [each for each in deeper if each not in modules]
        return targets, modules

    def _walk(self, targets: list[Target], modules: list[str], segments: list[str],
              seen: set[tuple[str, str]] | None = None) -> tuple[list[Target], list[str]]:
        """Follow `a.b.c` down from what `a` stands for."""
        for segment in segments:
            next_targets: list[Target] = []
            next_modules: list[str] = []
            for module in modules:
                found, inner = self._exported(module, segment, seen)
                next_targets += found
                next_modules += inner
            for target in targets:
                next_targets += self._members.get(target.path, {}).get(f"{target.qualified}.{segment}", [])
            targets, modules = next_targets, next_modules
            if not targets and not modules:
                break
        return targets, modules

    def _member_of(self, targets: list[Target], modules: list[str], rest: list[str], name: str) -> list[Target]:
        """`name` as a member of what `targets` and `modules` stand for, after walking `rest`."""
        targets, modules = self._walk(targets, modules, rest)
        found: list[Target] = []
        for module in modules:
            found += self._exported(module, name)[0]
        for target in targets:
            if target.kind in _TYPES:
                found += self._method((target.path, target.qualified), name)
            else:
                found += self._members.get(target.path, {}).get(f"{target.qualified}.{name}", [])
        return found

    def _nearby(self, path: str, facts: FileFacts, name: str) -> list[Target]:
        """A name that needs no import: the same package, or a module imported whole."""
        found: list[Target] = []
        package = self._package.get(path, "")
        if facts.family in ("go", "java") and package:
            beside = self._dirs.get(_parent(path), []) if facts.family == "go" else self._packages.get(("java", package), [])
            for other in beside:
                if other != path and other.endswith((".go", ".java")) and self._package.get(other, "") == package:
                    found += self._top[other].get(name, [])
        if found:
            return found
        found, _ = self._starred(path, name, set())     # everything a star import may have brought
        if not found and facts.family == "java":
            for binding in self._wild[path]:            # import static a.b.Type.*
                package, _, owner = binding.source.rpartition(".")
                for file in self._packages.get(("java", package), []):
                    found += self._method((file, owner), name) if owner in self._top[file] else []
        return found

    # ---- where a module lives -------------------------------------------------------------------

    def _place(self, path: str, binding: Binding) -> list[str]:
        """The files an import's source names; [] when it leaves these files."""
        family = self.files[path].family
        if family == "python":
            return self._place_python(path, binding)
        if family in _ECMA:
            return self._place_ecma(path, binding.source)
        if family == "go":
            return self._place_go(binding.source)
        if family == "java":
            return list(self._packages.get(("java", binding.source), []))
        if family in ("c", "cpp"):
            beside = _join(_parent(path), binding.source)
            if beside in self.files:
                return [beside]
            matching = [file for file in self._by_basename(binding.source.rsplit("/", 1)[-1])
                        if file.endswith("/" + binding.source) or file == binding.source]
            return matching if len(matching) == 1 else []
        return []

    def _python_module(self, base: str) -> list[str]:
        for candidate in (base + ".py", base + "/__init__.py"):
            if candidate in self.files:
                return [candidate]
        return [base + "/"] if base in self._dirs else []     # a package without an __init__: only its modules

    def _place_python(self, path: str, binding: Binding) -> list[str]:
        parts = [part for part in binding.source.split(".") if part]
        if binding.level:
            base = _parent(path).split("/") if _parent(path) else []
            if binding.level - 1 > len(base):
                return []
            base = base[:len(base) - (binding.level - 1)]
            return self._python_module("/".join(base + parts))
        if not parts:
            return []
        tail = "/".join(parts)
        rooted: list[tuple[int, str]] = []
        for suffix in (tail + ".py", tail + "/__init__.py"):
            for file, root in self._python.get(suffix, []):
                # An absolute import starts at a directory that is not itself a package. Inside the package
                # `flask.json`, `import json` is the standard library's, never `flask/json`.
                if root and (root + "/__init__.py" in self.files or "packages" not in self.features):
                    continue
                above = not root or path.startswith(root + "/")
                rooted.append((len(root) if above else -1, file))
        if not rooted:
            return []
        best = max(rank for rank, _ in rooted)
        return [file for rank, file in rooted if rank == best]

    def _ecma_file(self, base: str) -> list[str]:
        if base in self.files:
            return [base]
        stem = next((base[:-len(suffix)] for suffix in (".js", ".mjs", ".cjs", ".jsx") if base.endswith(suffix)), base)
        for candidate in [stem + suffix for suffix in _ECMA_SUFFIXES] + [f"{stem}/index{suffix}" for suffix in _ECMA_SUFFIXES]:
            if candidate in self.files:
                return [candidate]
        return []

    def _place_ecma(self, path: str, source: str) -> list[str]:
        if source.startswith("."):
            return self._ecma_file(_join(_parent(path), source))
        for config, before, after, targets in self._aliases:
            if config and not path.startswith(config + "/"):
                continue
            if after == "\0":
                middle = "" if source == before else None
            else:
                fits = source.startswith(before) and source.endswith(after) and len(source) >= len(before) + len(after)
                middle = source[len(before):len(source) - len(after)] if fits else None
            if middle is None:
                continue
            for target in targets:
                found = self._ecma_file(target.replace("*", middle))
                if found:
                    return found
        owner = max((name for name in self._node_packages if source == name or source.startswith(name + "/")), key=len, default="")
        if not owner:
            return []
        here, entries = self._node_packages[owner]
        inside = source[len(owner) + 1:]
        candidates = [_join(here, inside), _join(here, "src/" + inside)] if inside else \
            [_join(here, entry) for entry in entries] + [_join(here, "index"), _join(here, "src/index")]
        for candidate in candidates:
            found = self._ecma_file(candidate)
            if found:
                return found
        return []

    def _place_go(self, source: str) -> list[str]:
        for module, here in self._go_modules:
            if source == module or source.startswith(module + "/"):
                directory = _join(here, source[len(module):].lstrip("/"))
                return sorted(file for file in self._dirs.get(directory, [])
                              if file.endswith(".go") and not file.endswith("_test.go"))
        return []

    def _submodule(self, module: str, name: str) -> list[str]:
        """A module of that name under `module`: Python's packages, Rust's module tree."""
        if module.endswith(("/__init__.py", "/")) or module == "__init__.py":
            return self._python_module(_join(module.rstrip("/") if module.endswith("/") else _parent(module), name))
        if module.endswith(".rs"):
            child = self._rust_child(module, name)
            return [child] if child else []
        return []

    # ---- Rust's module tree ---------------------------------------------------------------------

    def _rust_child(self, path: str, name: str) -> str | None:
        here, base = _parent(path), path.rsplit("/", 1)[-1]
        own = here if base in _RUST_ROOTS or base == "mod.rs" else _join(here, base[:-3])
        declared = any(binding.kind == "module" for binding in self._bound.get(path, {}).get(name, []))
        for directory in [own] + ([here] if declared and own != here else []):   # a crate root keeps its modules beside it
            for candidate in (_join(directory, name + ".rs"), _join(directory, name + "/mod.rs")):
                if candidate in self.files:
                    return candidate
        return None

    def _rust_parent(self, path: str) -> str | None:
        here, base = _parent(path), path.rsplit("/", 1)[-1]
        if base in _RUST_ROOTS:
            return None
        stem, owned = (here.rsplit("/", 1)[-1], _parent(here)) if base == "mod.rs" else (base[:-3], here)
        for sibling in self._dirs.get(owned, []):        # whoever says `mod stem;` beside it
            if sibling != path and any(binding.kind == "module" for binding in self._bound.get(sibling, {}).get(stem, [])):
                return sibling
        for candidate in (_join(owned, "mod.rs"), _join(owned, "lib.rs"), _join(owned, "main.rs"), owned + ".rs"):
            if candidate in self.files and candidate != path:
                return candidate
        return None

    def _rust_root(self, path: str) -> str:
        current = path
        for _ in range(32):
            above = self._rust_parent(current)
            if above is None:
                break
            current = above
        return current

    def _rust_module(self, path: str, segments: list[str], depth: int,
                     seen: set[tuple[str, str]] | None = None) -> tuple[str | None, list[str]]:
        """The file a Rust path leads to and the segments left over, which name things inside it.
        (None, …) when the path leaves these files."""
        current: str | None = path
        index = 0
        first = segments[0]
        if first == "crate":
            current, index = self._rust_root(path), 1
        elif first == "self":
            index = 1
        elif first == "super":
            while index < len(segments) and segments[index] == "super":
                if depth > 0:
                    depth -= 1          # out of an inline module: still this file
                else:
                    current = self._rust_parent(current) if current else None
                    if current is None:
                        return None, []
                index += 1
        else:
            bound = [binding for binding in self._bound[path].get(first, []) if binding.level <= depth] if path in self._bound else []
            child = self._rust_child(path, first)
            seen = seen if seen is not None else set()
            if bound and ("\0" + path, first) not in seen:
                seen.add(("\0" + path, first))      # `use a::a;`, or two files that name each other
                _, modules, _ = self._rust_binding(path, bound[0], seen)
                if not modules:
                    return None, []
                current, index = modules[0], 1
            elif child:
                current, index = child, 1
            elif first in self._crates:
                current, index = self._crates[first], 1
            else:
                return None, []
        while index < len(segments) and current is not None:
            below = self._rust_child(current, segments[index])
            if below is None:
                break
            current, index = below, index + 1
        return current, segments[index:]

    def _rust_binding(self, path: str, binding: Binding, seen: set[tuple[str, str]]
                      ) -> tuple[list[Target], list[str], bool]:
        if binding.kind == "module":
            child = self._rust_child(path, binding.name)
            return [], [child] if child else [], bool(child)
        if not binding.source:                                      # `extern crate x;`, `use x;`
            root = self._crates.get(binding.name)
            return [], [root] if root else [], bool(root)
        module, rest = self._rust_module(path, binding.source.split("::"), binding.level, seen)
        if module is None:
            return [], [], False
        if binding.name == "*":
            targets, modules = self._walk([], [module], rest, seen)
            return [], modules, True
        targets, modules = [], [module]
        for segment in rest + [binding.name]:
            next_targets: list[Target] = []
            next_modules: list[str] = []
            for inner in modules:
                found, deeper = self._exported(inner, segment, seen)
                next_targets += found
                next_modules += deeper
            for target in targets:
                next_targets += self._members.get(target.path, {}).get(f"{target.qualified}.{segment}", [])
            targets, modules = next_targets, next_modules
        return targets, modules, True

    # ---- by name alone --------------------------------------------------------------------------

    def _by_name(self, path: str, facts: FileFacts, call: Call, members: bool, rule: str) -> Edge | None:
        """Every definition of the name: the last resort, and marked as such."""
        pool = [target for target in self._named.get((_group(facts.family), call.name), ())
                if (target.kind in _MEMBERS or "." in target.qualified) == members and self._visible(target, path, facts.family)
                and not (target.kind in _VALUES and self._is_import(target))]
        if not pool or len(pool) > CANDIDATE_LIMIT:
            return None
        here = _parent(path)
        pool.sort(key=lambda target: (target.path != path, _parent(target.path) != here, target.path, target.start_line))
        return Edge(tuple(pool), "L1", "name", rule)

    def _is_import(self, target: Target) -> bool:
        """`const Thing = require('./thing')`: a definition by its form, an import by what it does."""
        file = self._of(target.path)
        return file is not None and file.family in _ECMA and target.name in file.imported

    @staticmethod
    def _visible(target: Target, path: str, family: str) -> bool:
        """Whether a definition can be named from another file at all, where the name itself says so."""
        if target.path == path:
            return True
        if family == "python":
            return target.kind in _MEMBERS or not target.name.startswith("_") or target.name.startswith("__")
        if family == "go":
            return target.name[:1].isupper() or _parent(target.path) == _parent(path)
        return True

    def _edge(self, path: str, targets: list[Target], rule: str) -> Edge | None:
        unique = list(dict.fromkeys(targets))
        if not unique or len(unique) > CANDIDATE_LIMIT:
            return None
        same_file = all(target.path == path for target in unique)
        return Edge(tuple(unique), "L0" if same_file else "L1", "syntactic" if same_file else "import-resolved", rule)


def _conventional(source: str) -> str:
    """The name an unaliased Go import binds by convention: as `lang.bindings` worked it out."""
    segments = [segment for segment in source.split("/") if segment]
    if len(segments) > 1 and re.match(r"^v[0-9]+$", segments[-1]):
        segments.pop()
    tail = segments[-1] if segments else ""
    dotted = re.match(r"^(?P<name>.+)\.v[0-9]+$", tail)
    return dotted.group("name") if dotted else tail


def _group(family: str) -> str:
    """Languages whose definitions can call each other."""
    return {"tsx": "typescript", "javascript": "typescript", "c": "cpp"}.get(family, family)


def _segments(receiver: str) -> tuple[str, ...] | None:
    """`a.b.c` or `a::b::c` as its names; None when the receiver is anything else (`f().x`, `a[0]`)."""
    text = receiver.strip()
    if text.endswith("()") and text[:-2] == "super":
        return ("super",)
    parts = tuple(part for part in re.split(r"\.|::|->", text))
    return parts if parts and all(_SEGMENT.match(part) for part in parts) else None


def _matching_open(text: str) -> int:
    """Where the bracket that closes at the end of `text` opens; -1 when it does not."""
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index] == ")":
            depth += 1
        elif text[index] == "(":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _join(directory: str, relative: str) -> str:
    """A path under `directory`, with `.` and `..` worked out."""
    parts: list[str] = []
    for part in PurePosixPath(directory, relative).parts if directory else PurePosixPath(relative).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", "/"):
            parts.append(part)
    return "/".join(parts)


def _without_comments(text: str) -> str:
    """JSON as tsconfig files write it: comments and trailing commas removed, strings left alone."""
    out: list[str] = []
    index, in_string = 0, False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index < 0:
                break
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


_NO_FILE = _File("", FileFacts("", []))
