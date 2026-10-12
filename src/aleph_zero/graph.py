"""The code graph: which definition a call reaches, and how sure that is.

A call site says only what is written (`lang/calls.py`). This module answers it for a set
of files, in steps that go from certain to guessed, and every answer carries how it was
found (ADR-003):

- level L0, provenance `syntactic`: the callee is defined in the same file (or is a method
  of the class the call sits in, called on `self` or `this`).
- level L1, provenance `import-resolved`: the file imports the name, and the import leads
  to a file of the view that defines it (through re-exports, a few hops).
- level L1, provenance `name`: nothing better is known; every definition of that name in
  the view is a candidate, and the confidence is one over their number.

An answer with one target is "confident" in the sense the grader uses. Nothing here reads
files or the index: it works on facts per file handed to it, so it serves a view built
from the index and a corpus read from disk alike.

The approach follows oxn's resolver (v0.1.2, MIT, same author); this is a first version,
graded by `bench/eval/graph_grade.py`, and not yet a port of all of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .lang.calls import Call
from .lang.definitions import Definition
from .lang.imports import RawImport

CANDIDATE_LIMIT = 20
_SELF = frozenset({"self", "this", "cls", "super()", "super"})
_CALLABLE = frozenset({"function", "method", "class", "interface", "type", "enum"})
_SOURCE_SUFFIXES = {
    "python": (".py",), "typescript": (".ts", ".tsx", ".d.ts", ".js"), "tsx": (".tsx", ".ts", ".js"),
    "javascript": (".js", ".mjs", ".cjs", ".jsx"), "java": (".java",),
}
_INDEX_NAMES = {"python": ("__init__.py",), "typescript": ("index.ts", "index.tsx"), "tsx": ("index.tsx", "index.ts"),
                "javascript": ("index.js", "index.mjs")}


@dataclass(frozen=True)
class FileFacts:
    family: str                         # the language profile's name
    definitions: list[Definition]
    imports: list[RawImport] = field(default_factory=list)


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

    @property
    def confidence(self) -> float:
        return 1.0 / len(self.targets) if self.targets else 0.0


class Graph:
    def __init__(self, files: dict[str, FileFacts]) -> None:
        self.files = files
        self._top: dict[str, dict[str, list[Target]]] = {}      # path -> name -> its top-level definitions
        self._members: dict[str, dict[str, list[Target]]] = {}  # path -> 'Owner.name' -> methods
        self._by_name: dict[tuple[str, str], list[Target]] = {}  # (language group, name) -> every definition, project-wide
        for path, facts in files.items():
            top, members = self._top.setdefault(path, {}), self._members.setdefault(path, {})
            for definition in facts.definitions:
                if definition.kind not in _CALLABLE:
                    continue
                target = Target(path, definition.name, definition.qualified, definition.kind, definition.start_line, definition.end_line)
                if definition.parent is None and "." not in definition.qualified:
                    top.setdefault(definition.name, []).append(target)
                else:
                    members.setdefault(definition.qualified, []).append(target)
                self._by_name.setdefault((_group(facts.family), definition.name), []).append(target)

    def resolve(self, path: str, call: Call) -> Edge | None:
        """The definition(s) `call`, written in `path`, may reach; None when nothing in these files fits."""
        facts = self.files.get(path)
        if facts is None:
            return None
        receiver = call.receiver.strip()
        if not receiver:
            same = self._top[path].get(call.name)
            if same:
                return Edge(tuple(same), "L0", "syntactic")
            imported = self._imported(path, facts, call.name, hops=3)
            if imported:
                return Edge(tuple(imported), "L1", "import-resolved")
            return self._named(facts, path, call.name, methods=False)
        if receiver in _SELF or receiver.startswith(("self.", "this.")) and False:
            owner = _owner_of(facts, call)
            if owner:
                own = self._members[path].get(owner + "." + call.name)
                if own:
                    return Edge(tuple(own), "L0", "syntactic")
            return self._named(facts, path, call.name, methods=True)
        head = receiver.split(".")[0].split("::")[0]
        # `Type.method()` or `module.function()` where the head is something this file defines or imports.
        local_type = self._members[path].get(head + "." + call.name) if "." not in receiver and "::" not in receiver else None
        if local_type:
            return Edge(tuple(local_type), "L0", "syntactic")
        through = self._through_import(path, facts, head, receiver, call.name)
        if through:
            return Edge(tuple(through), "L1", "import-resolved")
        return self._named(facts, path, call.name, methods=True)

    # ---- by import ---------------------------------------------------------------------------

    def _imported(self, path: str, facts: FileFacts, name: str, hops: int) -> list[Target]:
        """Definitions of `name` reached through an import of that name in `path`."""
        for item in facts.imports:
            if name not in item.names and not (item.kind == "wildcard"):
                continue
            for module in self._module_files(path, facts.family, item):
                found = self._top.get(module, {}).get(name)
                if found:
                    return found
                if hops > 0 and module != path:  # a package's __init__ or a barrel file republishing the name
                    onward = self._imported(module, self.files[module], name, hops - 1)
                    if onward:
                        return onward
        return []

    def _through_import(self, path: str, facts: FileFacts, head: str, receiver: str, name: str) -> list[Target]:
        """`mod.f()`, `pkg.mod.f()`, `Type.f()` where the head is imported into `path`."""
        for item in facts.imports:
            last = item.specifier.replace("::", ".").replace("/", ".").split(".")[-1] if item.specifier else ""
            if head in item.names:
                # from pkg import mod  -> mod.f();   from mod import Type -> Type.f()
                for module in self._module_files(path, facts.family, item):
                    for nested in self._submodule(module, head, facts.family):
                        found = self._top.get(nested, {}).get(name)
                        if found:
                            return found
                    method = self._members.get(module, {}).get(head + "." + name)
                    if method:
                        return method
            elif head == last and "." not in receiver and "::" not in receiver:
                for module in self._module_files(path, facts.family, item):   # import pkg.mod -> mod.f() ; Go: pkg.F()
                    found = self._top.get(module, {}).get(name)
                    if found:
                        return found
        return []

    def _submodule(self, module: str, name: str, family: str) -> list[str]:
        base = PurePosixPath(module).parent
        return [str(base / (name + suffix)) for suffix in _SOURCE_SUFFIXES.get(family, ()) if str(base / (name + suffix)) in self.files] + [
            str(base / name / index) for index in _INDEX_NAMES.get(family, ()) if str(base / name / index) in self.files]

    def _module_files(self, path: str, family: str, item: RawImport) -> list[str]:
        """The files of the view an import statement names; [] when it leaves the tree or cannot be told."""
        here = PurePosixPath(path).parent
        specifier = item.specifier
        suffixes, indexes = _SOURCE_SUFFIXES.get(family, ()), _INDEX_NAMES.get(family, ())
        bases: list[PurePosixPath] = []
        if family == "python":
            parts = [part for part in specifier.split(".") if part]
            if item.level:
                start = here
                for _ in range(item.level - 1):
                    start = start.parent
                bases.append(start.joinpath(*parts))
            else:
                # An absolute import is rooted somewhere above: try each directory on the way up.
                roots = [PurePosixPath(".")] + [PurePosixPath(*here.parts[:depth]) for depth in range(1, len(here.parts) + 1)]
                bases.extend(root.joinpath(*parts) for root in roots)
        elif family in ("typescript", "tsx", "javascript"):
            if not specifier.startswith("."):
                return []  # a package, or a path alias this version does not read
            bases.append(here / specifier)
        elif family == "java":
            tail = specifier.replace(".", "/") + ".java"
            return [candidate for candidate in self.files if candidate.endswith("/" + tail) or candidate == tail]
        elif family == "go":
            tail = specifier.strip('"')
            return sorted(candidate for candidate in self.files
                          if candidate.endswith(".go") and not candidate.endswith("_test.go")
                          and (str(PurePosixPath(candidate).parent) + "/").endswith("/" + tail.split("/", 3)[-1] + "/"))
        found = []
        for base in bases:
            text = _normal(base)
            for candidate in [text] + [text + suffix for suffix in suffixes] + [f"{text}/{index}" for index in indexes]:
                if candidate in self.files and candidate not in found:
                    found.append(candidate)
        return found

    # ---- by name ------------------------------------------------------------------------------

    def _named(self, facts: FileFacts, path: str, name: str, methods: bool) -> Edge | None:
        """Every definition of the name in these files: the last resort, and marked as such."""
        candidates = [target for target in self._by_name.get((_group(facts.family), name), ())
                      if (target.kind == "method") == methods or (not methods and target.kind != "method")
                      or (methods and target.kind in ("function",) and "." in target.qualified)]
        if not methods:
            # A name private to another file is not a candidate here.
            candidates = [target for target in candidates if target.path == path or not target.name.startswith("_")]
        if facts.family == "go":
            near = [target for target in candidates if PurePosixPath(target.path).parent == PurePosixPath(path).parent]
            if near and not methods:
                return Edge(tuple(near[:CANDIDATE_LIMIT]), "L0" if all(t.path == path for t in near) else "L1", "syntactic")
        if not candidates or len(candidates) > CANDIDATE_LIMIT:
            return None
        candidates.sort(key=lambda target: (target.path != path, target.path, target.start_line))
        return Edge(tuple(candidates), "L1", "name")


def _group(family: str) -> str:
    """Languages whose definitions can call each other."""
    return {"tsx": "typescript", "javascript": "typescript", "c": "cpp"}.get(family, family)


def _owner_of(facts: FileFacts, call: Call) -> str:
    """The qualified name of the type the call's enclosing method belongs to, or ''."""
    if call.caller is None:
        return ""
    qualified = facts.definitions[call.caller].qualified
    return qualified.rsplit(".", 1)[0] if "." in qualified else ""


def _normal(path: PurePosixPath) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)
