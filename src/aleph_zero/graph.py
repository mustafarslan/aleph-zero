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
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .lang.bindings import Binding
from .lang.calls import Call
from .lang.definitions import Definition

CANDIDATE_LIMIT = 20
HOPS = 6
# The three extensions over oxn's resolver, switchable so each can be measured apart (ADR-003).
FEATURES = frozenset({"reexports", "packages", "receivers"})
MANIFESTS = ("go.mod", "Cargo.toml", "package.json", "tsconfig.json", "tsconfig.base.json")

_TYPES = frozenset({"class", "interface", "enum", "type"})
_MEMBERS = frozenset({"method", "field"})
_UNNAMED = frozenset({"namespace"})
_VALUES = frozenset({"constant", "variable"})
_ECMA = frozenset({"typescript", "tsx", "javascript"})
_IMPLICIT_THIS = frozenset({"java", "cpp"})
_PACKAGE_WIDE_TYPES = frozenset({"go", "rust"})   # a type's methods may be declared in another file
_ECMA_SUFFIXES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_RUST_ROOTS = ("lib.rs", "main.rs")
_SEGMENT = re.compile(r"^[A-Za-z_$][\w$]*$")


@dataclass(frozen=True)
class FileFacts:
    family: str                         # the language profile's name
    definitions: list[Definition]
    bindings: list[Binding] = field(default_factory=list)
    bases: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Target:
    path: str
    name: str
    qualified: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Edge:
    targets: tuple[Target, ...]
    level: str          # 'L0' | 'L1'
    provenance: str     # 'syntactic' | 'import-resolved' | 'name'
    rule: str = ""      # which step answered: for reports, decides nothing

    @property
    def confidence(self) -> float:
        return 1.0 / len(self.targets) if self.targets else 0.0

    @property
    def certain(self) -> bool:
        """One target, reached by what the code says and not by its name alone. A lone candidate found by
        name is not certain: the receiver's type is unknown, and a library's method of that name would not
        be among the candidates at all (on ripgrep, three of four such answers at library calls were wrong)."""
        return len(self.targets) == 1 and self.provenance != "name"


class Graph:
    def __init__(self, files: dict[str, FileFacts], manifests: dict[str, bytes] | None = None,
                 features: frozenset[str] | set[str] = FEATURES) -> None:
        self.files = files
        self.features = frozenset(features)
        self._top: dict[str, dict[str, list[Target]]] = {}        # path -> name -> the file's own names
        self._members: dict[str, dict[str, list[Target]]] = {}    # path -> 'Owner.name' -> what an owner holds
        self._kinds: dict[str, dict[str, str]] = {}               # path -> qualified -> kind
        self._named: dict[tuple[str, str], list[Target]] = {}     # (language group, name) -> every definition
        self._methods: dict[tuple[str, str], list[Target]] = {}   # (language group, 'Type.name') -> wherever declared
        self._bound: dict[str, dict[str, list[Binding]]] = {}     # path -> local name -> its import bindings
        self._wild: dict[str, list[Binding]] = {}                 # path -> imports of everything a module has
        self._published: dict[str, dict[str, list[Binding]]] = {}  # path -> a name it publishes -> its own name for it
        self._package: dict[str, str] = {}                        # path -> the package it says it belongs to
        self._packages: dict[tuple[str, str], list[str]] = {}     # (language group, package) -> its files
        self._bases: dict[tuple[str, str], list[str]] = {}        # (path, type) -> what it is built on, as written
        self._bases_named: dict[tuple[str, str], list[tuple[str, str]]] = {}  # (group, simple type) -> (path, base)
        self._dirs: dict[str, list[str]] = {}                     # directory -> its files
        self._python: dict[str, list[tuple[str, str]]] = {}       # 'a/b.py' -> (file, the directory it is rooted in)
        for path, facts in files.items():
            self._index(path, facts)
        self._read_layout(manifests or {})
        self._name_go_packages()

    # ---- building -----------------------------------------------------------------------------

    def _index(self, path: str, facts: FileFacts) -> None:
        group = _group(facts.family)
        top, members, kinds = self._top.setdefault(path, {}), self._members.setdefault(path, {}), self._kinds.setdefault(path, {})
        imported = {binding.local for binding in facts.bindings if binding.kind == "import"}
        for definition in facts.definitions:
            if definition.kind in _VALUES and definition.name in imported and "." not in definition.qualified:
                continue    # `const Thing = require('./thing')` declares nothing: the name is the import
            kinds[definition.qualified] = definition.kind
            if definition.kind in _UNNAMED:
                continue
            target = Target(path, definition.name, definition.qualified, definition.kind, definition.start_line, definition.end_line)
            if "." in definition.qualified:
                members.setdefault(definition.qualified, []).append(target)
                self._methods.setdefault((group, ".".join(definition.qualified.split(".")[-2:])), []).append(target)
            else:
                top.setdefault(definition.name, []).append(target)
            self._named.setdefault((group, definition.name), []).append(target)
        bound, wild, published = self._bound.setdefault(path, {}), self._wild.setdefault(path, []), self._published.setdefault(path, {})
        for binding in facts.bindings:
            if binding.kind == "package":
                self._package[path] = binding.local
                self._packages.setdefault((group, binding.local), []).append(path)
            elif binding.kind == "export":
                published.setdefault(binding.local, []).append(binding)
            elif binding.local == "*":
                wild.append(binding)
            else:
                bound.setdefault(binding.local, []).append(binding)
        for kind, base in facts.bases:
            self._bases.setdefault((path, kind), []).append(base)
            self._bases_named.setdefault((group, kind.rsplit(".", 1)[-1]), []).append((path, base))
        self._dirs.setdefault(_parent(path), []).append(path)
        if facts.family == "python":
            parts = path.split("/")
            for cut in range(len(parts)):
                self._python.setdefault("/".join(parts[cut:]), []).append((path, "/".join(parts[:cut])))

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
                        entries = [str(manifest[key]) for key in ("source", "types", "typings", "module", "main")
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

    def _name_go_packages(self) -> None:
        """A Go import binds the package's own name, which is the last segment of its path only by convention."""
        for path, facts in self.files.items():
            if facts.family != "go":
                continue
            for local, found in list(self._bound[path].items()):
                for binding in found:
                    if binding.name or local != binding.source.rsplit("/", 1)[-1] and not binding.source.endswith(local):
                        continue
                    declared = {self._package.get(file, "") for file in self._place(path, binding)} - {"", local}
                    for name in declared:
                        self._bound[path].setdefault(name, []).append(binding)

    # ---- the answer ---------------------------------------------------------------------------

    def resolve(self, path: str, call: Call) -> Edge | None:
        """The definition(s) `call`, written in `path`, may reach; None when nothing in these files fits."""
        facts = self.files.get(path)
        if facts is None:
            return None
        if not call.via:
            return self._plain(path, facts, call)
        if call.head in ("self", "super"):
            return self._on_self(path, facts, call)
        return self._qualified(path, facts, call)

    def _plain(self, path: str, facts: FileFacts, call: Call) -> Edge | None:
        """`helper()`: the file's own scope, then its imports, then its package, then the name alone."""
        name = call.name
        if call.head == "local":
            return None     # a parameter, a local variable or a nested function: nothing recorded has that name
        here = self._lexical(path, facts, call)
        if here:
            return self._edge(path, here, "scope")
        bound = self._bound[path].get(name)
        if bound:
            reached = False
            for binding in bound:
                targets, modules, placed = self._stands_for(path, binding)
                reached = reached or placed
                if not targets and modules and facts.family in _ECMA:   # `const Linter = require('./linter'); new Linter()`
                    targets = [found for module in modules for found in self._exported(module, "default")[0]]
                if targets:
                    return self._edge(path, targets, "import")
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
        if len(_segments(call.receiver) or ("", "")) != 1 and not call.receiver.startswith("super("):
            return self._by_name(path, facts, call, members=True, rule="name, on a field")     # `self.store.save()`
        found = self._method((path, owner), call.name, above=call.head == "super")
        return self._edge(path, found, "own type") if found else None

    def _qualified(self, path: str, facts: FileFacts, call: Call) -> Edge | None:
        """`a.f()` and `a::f()` where `a` is not the method's own object."""
        segments = _segments(call.receiver)
        if call.head in ("local", "value") or segments is None:
            return self._by_name(path, facts, call, members=True, rule="name, on a value")
        head, rest, name = segments[0], list(segments[1:]), call.name
        dotted = ".".join(segments)
        local_type = [qualified for qualified, kind in self._kinds[path].items()
                      if kind not in _MEMBERS and (qualified == dotted or qualified.endswith("." + dotted))]
        if local_type:      # `Type.method()`, or an object of this file: `handlers.run()`
            found = [target for qualified in local_type for target in self._method((path, qualified), name)]
            if found:
                return self._edge(path, found, "type in file")
        if facts.family == "rust" and call.via == "path" and (head in ("crate", "self", "super") or head not in self._bound[path]):
            return self._rust_path(path, facts, call, list(segments))
        bound = self._bound[path].get(head)
        if bound:
            reached = False
            for binding in bound:
                targets, modules, placed = self._stands_for(path, binding)
                reached = reached or placed
                found = self._member_of(targets, modules, rest, name)
                if found:
                    return self._edge(path, found, "import")
            if not reached:
                return None     # `np.array()`: the receiver is an import from outside these files
            return self._by_name(path, facts, call, members=True, rule="name, import not followed")
        if local_type:
            return self._by_name(path, facts, call, members=True, rule="name, type in file lacks it")
        nearby = self._nearby(path, facts, head)    # a type of the same package, written without an import
        if nearby and not rest:
            found = [target for kind in nearby for target in self._method((kind.path, kind.qualified), name)]
            if found:
                return self._edge(path, found, "package")
        return self._by_name(path, facts, call, members=True, rule="name, on an unbound name")

    def _rust_path(self, path: str, facts: FileFacts, call: Call, segments: list[str]) -> Edge | None:
        """`crate::flags::parse()`, `super::check()`, `Self::new()`, `grep_matcher::find()`."""
        if segments[0] == "Self":
            owner = self._owner(facts, call)
            found = self._method((path, owner), call.name) if owner and len(segments) == 1 else []
            return self._edge(path, found, "own type") if found else None
        module, rest = self._rust_module(path, segments, self._inline_depth(facts, call))
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

    def _inline_depth(self, facts: FileFacts, call: Call) -> int:
        """How many inline Rust modules the call is written inside."""
        if call.caller is None:
            return 0
        return sum(1 for index in self._ancestors(facts, call.caller) if facts.definitions[index].kind == "namespace")

    @staticmethod
    def _ancestors(facts: FileFacts, index: int) -> list[int]:
        out: list[int] = []
        parent = facts.definitions[index].parent
        while parent is not None and len(out) < 32:
            out.append(parent)
            parent = facts.definitions[parent].parent
        return out

    def _lexical(self, path: str, facts: FileFacts, call: Call) -> list[Target]:
        """A plain name looked up outwards from where the call stands, as the language would."""
        for qualified, kind in self._containers(path, facts, call):
            if kind == "namespace":
                found = self._members[path].get(f"{qualified}.{call.name}")
                if found:
                    return found
            elif kind in _TYPES and facts.family in _IMPLICIT_THIS:     # `helper()` inside a class is `this.helper()`
                found = self._method((path, qualified), call.name)
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
                seen: set[tuple[str, str]] | None = None) -> list[Target]:
        """`name` as the type `kind` (file, qualified name) has it: its own, else the nearest base's."""
        seen = seen if seen is not None else set()
        if kind in seen or len(seen) > 64:
            return []
        seen.add(kind)
        path, qualified = kind
        if not above:
            own = self._own_members(path, qualified, name)
            if own:
                return own
        for declared_in, base in self._bases_of(path, qualified):
            for found in self._type(declared_in, base):
                inherited = self._method(found, name, False, seen)
                if inherited:
                    return inherited
        return []

    def _own_members(self, path: str, qualified: str, name: str) -> list[Target]:
        own = self._members.get(path, {}).get(f"{qualified}.{name}")
        facts = self.files.get(path)
        if own or facts is None or facts.family not in _PACKAGE_WIDE_TYPES:
            return own or []
        simple = qualified.rsplit(".", 1)[-1]
        spread = self._methods.get((_group(facts.family), f"{simple}.{name}"), [])
        return [target for target in spread if self._same_unit(path, target.path)]

    def _bases_of(self, path: str, qualified: str) -> list[tuple[str, str]]:
        found = [(path, base) for base in self._bases.get((path, qualified), [])]
        facts = self.files.get(path)
        if facts is not None and facts.family in _PACKAGE_WIDE_TYPES:    # `impl Trait for S` in another file of the crate
            simple = qualified.rsplit(".", 1)[-1]
            found += [(other, base) for other, base in self._bases_named.get((_group(facts.family), simple), [])
                      if other != path and self._same_unit(path, other)]
        return found

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
        bound = self._bound[path].get(head)
        if bound:
            for binding in bound:
                targets, modules, _ = self._stands_for(path, binding)
                targets, modules = self._walk(targets, modules, list(segments[1:]))
                found = [(target.path, target.qualified) for target in targets if target.kind in _TYPES]
                if found:
                    return found
            return []
        if facts.family == "rust" and len(segments) > 1:
            module, rest = self._rust_module(path, list(segments), 0)
            targets, _ = self._walk([], [module], rest) if module is not None else ([], [])
            return [(target.path, target.qualified) for target in targets if target.kind in _TYPES]
        return [(target.path, target.qualified) for target in self._nearby(path, facts, head)
                if target.kind in _TYPES and len(segments) == 1]

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
        found = self._top.get(module, {}).get(name)
        if found:
            return found, []
        for published in self._published.get(module, {}).get(name, []):     # exports = { name: other }
            targets, modules = self._exported(module, published.name, seen)
            if targets or modules:
                return targets, modules
        inside = self._submodule(module, name)
        if inside:
            return [], inside
        if "reexports" not in self.features or module not in self.files:
            return [], []
        for binding in self._bound[module].get(name, []):                   # the module imported it and passes it on
            if binding.level and self.files[module].family == "rust":
                continue    # a `use` inside an inline module is not the file's own
            targets, modules, _ = self._stands_for(module, binding, seen)
            if targets or modules:
                return targets, modules
        for binding in self._wild[module]:                                   # export * from, from x import *, pub use x::*
            if binding.level and self.files[module].family == "rust":
                continue
            _, modules, _ = self._stands_for(module, binding, seen)
            for inner in modules:
                targets, deeper = self._exported(inner, name, seen)
                if targets or deeper:
                    return targets, deeper
        return [], []

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
        if facts.family in ("go", "java") and path in self._package:
            for other in self._packages.get((facts.family, self._package[path]), []):
                if other != path and (facts.family == "java" or _parent(other) == _parent(path)):
                    found += self._top[other].get(name, [])
        if found:
            return found
        for binding in self._wild[path]:
            _, modules, _ = self._stands_for(path, binding)
            for module in modules:
                found += self._exported(module, name)[0]
            if not modules and facts.family == "java":      # import static a.b.Type.*
                package, _, owner = binding.source.rpartition(".")
                for file in self._packages.get(("java", package), []):
                    found += self._method((file, owner), name) if owner in self._top[file] else []
            if found:
                return found
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
            matching = [file for file in self.files if file.endswith("/" + binding.source) or file == binding.source]
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
                above = not root or path.startswith(root + "/")
                if not root or above or root + "/__init__.py" not in self.files:
                    if root and "packages" not in self.features:
                        continue
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
                if (target.kind in _MEMBERS or "." in target.qualified) == members and self._visible(target, path, facts.family)]
        if not pool or len(pool) > CANDIDATE_LIMIT:
            return None
        here = _parent(path)
        pool.sort(key=lambda target: (target.path != path, _parent(target.path) != here, target.path, target.start_line))
        return Edge(tuple(pool), "L1", "name", rule)

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
