"""Does a test notice each rule of the code graph's resolver? Break them one at a time and see.

Usage (from the repository root, with a clean src/aleph_zero/graph.py):
  uv run python bench/tools/graph_rules_check.py

Each entry replaces one piece of graph.py's text with a broken version, runs tests/test_graph.py
and prints how many tests failed; "N passed" with no failure means no test notices that rule, and
"PATTERN NOT FOUND" means the entry has gone stale. The file is restored afterwards, also on error.
The resolver's tests were first written after its code; this is the check that they test it.
"""
import subprocess, sys
from pathlib import Path
target = Path("src/aleph_zero/graph.py")
original = target.read_text()
mutations = {
    "local names answered": ('        if call.head == "local":\n            return None     # a parameter', '        if False:\n            return None     # a parameter'),
    "outside imports guessed (plain)": ("            if not reached:\n                return None     # imported from outside", "            if False:\n                return None     # imported from outside"),
    "outside imports guessed (qualified)": ("            if not reached:\n                return None     # `np.array()`", "            if False:\n                return None     # `np.array()`"),
    "super is self": ('above=call.head == "super", arity=call.arity)', 'above=False, arity=call.arity)'),
    "no bases": ("        for declared_in, base in self._bases_of(path, qualified):", "        for declared_in, base in []:"),
    "self falls back to names": ('        return self._edge(path, found, "own type") if found else None\n\n    def _qualified', '        return self._edge(path, found, "own type") if found else self._by_name(path, facts, call, members=True, rule="x")\n\n    def _qualified'),
    "private names visible": ('            return target.kind in _MEMBERS or not target.name.startswith("_") or target.name.startswith("__")', '            return True'),
    "go private visible": ("            return target.name[:1].isupper() or _parent(target.path) == _parent(path)", "            return True"),
    "no candidate limit": ("        if not pool or len(pool) > CANDIDATE_LIMIT:", "        if not pool:"),
    "no inline scope": ('            if kind == "namespace":\n                found = self._members[path].get(f"{qualified}.{call.name}")', '            if kind == "namespace":\n                found = None'),
    "required const kept": ('            if target.kind in _VALUES and target.name in self.imported and "." not in target.qualified:', '            if False:'),
    "rust Vec::new guessed": ("        if module is None:\n            return None     # `Vec::new()`", "        if module is None:\n            return self._by_name(path, facts, call, members=True, rule='x')     # `Vec::new()`"),
    "typed outside guessed": ("call.name, call.arity) if kinds else None)", "call.name, call.arity) if kinds else _UNSTATED)"),
    "field types ignored": ('            if segments is not None and "receivers" in self.features:\n                typed = self._through(path, [(path, owner)]', '            if False:\n                typed = self._through(path, [(path, owner)]'),
    "return types ignored": ("        returned = self._returned(target)", "        returned = ''"),
    "implicit this fields ignored": ("        if owner and facts.family in _IMPLICIT_THIS and \"receivers\" in self.features and self._field((path, owner), head):", "        if False:"),
    "name match is certain": ('        return len(self.targets) == 1 and self.provenance != "name"', "        return len(self.targets) == 1"),
    "first import wins": ("                imported += targets\n            if imported:", "                imported = imported or targets\n            if imported:"),
    "first qualified import wins": ("                found += self._member_of(targets, modules, rest, name)", "                found = found or self._member_of(targets, modules, rest, name)"),
    "built by uncertain call": ("        if not built.certain:\n            return None", "        if False:\n            return None"),
    "field of outside type guessed": ("            kinds = below\n            if not kinds:\n                return None", "            kinds = below\n            if not kinds:\n                return _UNSTATED"),
    "python takes the first base": ('        if family == "python":\n            order = self._linearized(kind)', '        if False:\n            order = self._linearized(kind)'),
    "go takes the first embedding": ('        if family == "go":\n            return self._shallowest(kind, name, above)', '        if False:\n            return self._shallowest(kind, name, above)'),
    "overloads ignored": ("        if own and family not in _OVERLOADING:", "        if own:"),
    "any number of arguments fits": ('        return arity >= len(kinds) - 1 if kinds and kinds[-1].endswith("...") else arity == len(kinds)', "        return True"),
    "stars bring underscore names": ('        if file is None or file.family == "python" and name.startswith("_"):', "        if file is None:"),
    "a held name beats a star": ("        if targets and starred and set(starred) != set(targets):", "        if False:"),
    "rust types of one name merged": ("        return len(mine) == 1 and theirs == mine", "        return True"),
    "absolute import inside its package": ('                if root and (root + "/__init__.py" in self.files or "packages" not in self.features):', '                if root and "packages" not in self.features:'),
    "annotation read as parameters": ('        opening = named.end() - 1 if named else signature.find("(")', '        opening = signature.find("(")'),
}
try:
    for label, (old, new) in mutations.items():
        if old not in original:
            print(f"{label}: PATTERN NOT FOUND"); continue
        target.write_text(original.replace(old, new, 1))
        out = subprocess.run(["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_graph.py"], capture_output=True, text=True).stdout
        print(f"{label}: {out.strip().splitlines()[-1]}")
finally:
    target.write_text(original)
