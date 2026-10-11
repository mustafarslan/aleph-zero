import re
from pathlib import Path

import pytest

from aleph_zero import filters, lang
from aleph_zero.lang import parse

SAMPLES = Path(__file__).parent / "samples"
SAMPLE_FOR = {
    "python": "sample.py", "typescript": "sample.ts", "go": "sample.go",
    "rust": "sample.rs", "java": "Sample.java", "cpp": "sample.cpp",
}


def test_each_extension_selects_its_profile():
    expected = {
        "a.py": "python", "a.pyi": "python", "a.ts": "typescript", "a.mts": "typescript", "a.tsx": "tsx",
        "a.js": "javascript", "a.jsx": "javascript", "a.mjs": "javascript", "a.cjs": "javascript",
        "a.go": "go", "a.rs": "rust", "A.java": "java",
        "a.cpp": "cpp", "a.cc": "cpp", "a.cxx": "cpp", "a.hpp": "cpp", "a.hh": "cpp", "a.h": "cpp", "a.c": "c",
        "DIR/UPPER.PY": "python",
    }
    assert {path: lang.profile_for_path(path).name for path in expected} == expected


def test_a_file_that_is_not_source_code_has_no_profile():
    assert lang.profile_for_path("README.md") is None
    assert lang.profile_for_path("Makefile") is None


def test_the_family_of_a_source_file_is_its_language():
    assert filters.family("src/a.py") == "python"
    assert filters.family("web/App.tsx") == "tsx"
    assert filters.family("include/x.h") == "cpp"
    assert filters.family("uv.lock") == "lockfile"  # a lockfile's name wins over its suffix
    assert filters.classify("python", b"x = 1\n") == ("text", "x = 1\n")


@pytest.mark.parametrize("name", sorted(SAMPLE_FOR))
def test_the_grammar_loads_and_parses_its_sample_cleanly(name):
    profile = lang.PROFILES[name]
    tree = parse.tree(profile, (SAMPLES / SAMPLE_FOR[name]).read_bytes())
    assert tree.root_node.child_count > 0
    assert not tree.root_node.has_error


@pytest.mark.parametrize("name", sorted(lang.PROFILES))
def test_every_node_kind_named_in_a_profile_exists_in_its_grammar(name):
    profile = lang.PROFILES[name]
    language = parse.language(profile)
    kinds = (set(profile.function_like) | set(profile.anonymous) | set(profile.types) | set(profile.values)
             | set(profile.wrappers) | set(profile.comment_kinds) | set(profile.string_kinds)
             | set(profile.type_refinement) | set(profile.holders) | set(profile.imports.statement_kinds)
             | set(profile.imports.relative_kinds) | set(profile.imports.wildcard_kinds)
             | set(profile.imports.alias_kinds) | set(profile.imports.string_kinds) | set(profile.imports.call_kinds))
    unknown = sorted(kind for kind in kinds if language.id_for_node_kind(kind, True) is None)
    assert unknown == []


def test_parsers_are_created_once_per_grammar():
    profile = lang.PROFILES["python"]
    assert parse.parser(profile) is parse.parser(profile)


def test_the_language_package_stands_alone():
    # It is meant to be lifted into a shared core later (ADR-007), so it may not
    # reach into the rest of aleph_zero.
    outside = re.compile(r"^\s*(from \.\.|from aleph_zero(?!\.lang)|import aleph_zero(?!\.lang))", re.M)
    for source in (Path(lang.__file__).parent).glob("*.py"):
        assert not outside.search(source.read_text()), source.name


def test_the_port_says_where_it_came_from():
    assert "oxn" in lang.__doc__ and "2c8ee6d" in lang.__doc__ and "MIT" in lang.__doc__
