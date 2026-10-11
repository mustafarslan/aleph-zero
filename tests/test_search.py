import os

import pytest

from aleph_zero.indexer import Index
from aleph_zero.search import arrange, rrf, search, Hit
from aleph_zero.store import index_path, open_db
from aleph_zero.view import build_view


def _find(repo, query, **options):
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    return search(index, view, query, **options)


def _paths(hits):
    return [hit.path for hit in hits]


def test_finds_an_identifier_by_its_exact_name_and_by_its_words(repo):
    assert _paths(_find(repo, "MAX_RETRY_BACKOFF_EXPONENT"))[0] == "src/auth/token_store.py"
    assert _paths(_find(repo, "retry backoff"))[0] == "src/auth/token_store.py"
    assert _paths(_find(repo, "revoke all sessions"))[0] == "src/auth/token_store.py"  # camelCase in the file


def test_hit_carries_the_line_range_and_the_text(repo):
    hit = _find(repo, "render_invoice")[0]
    assert (hit.path, hit.start_line, hit.end_line) == ("src/billing/invoice.py", 1, 3)
    assert "Invoice total" in hit.text and "lexical" in hit.sources


def test_an_uncommitted_edit_is_searchable_and_the_old_content_is_gone(repo):
    repo.write("src/billing/invoice.py", "def render_receipt(order):\n    return 'zebra_marker'\n")
    assert _paths(_find(repo, "zebra_marker")) == ["src/billing/invoice.py"]
    assert all("Invoice total" not in hit.text for hit in _find(repo, "Invoice total"))


def test_a_deleted_file_is_never_returned(repo):
    assert "docs/guide.md" in _paths(_find(repo, "tokens expire"))
    os.remove(repo.root / "docs/guide.md")
    assert "docs/guide.md" not in _paths(_find(repo, "tokens expire"))


def test_content_of_another_branch_is_not_returned(repo):
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/feature_only.py", "def quokka_feature():\n    return 1\n")
    repo.commit("feature")
    assert _paths(_find(repo, "quokka_feature")) == ["src/feature_only.py"]
    repo.git("checkout", "-q", "main")
    assert _find(repo, "quokka_feature") == []


def test_identical_content_at_two_paths_returns_both(repo):
    repo.write("src/billing/copy.py", (repo.root / "src/billing/invoice.py").read_text())
    hits = _find(repo, "render_invoice")
    assert set(_paths(hits)[:2]) == {"src/billing/copy.py", "src/billing/invoice.py"}
    assert all(hit.kind == "doc" for hit in hits[2:])  # related documentation follows the code


def test_a_file_can_be_found_by_its_path(repo):
    hits = _find(repo, "token store")
    assert hits[0].path == "src/auth/token_store.py" and "path" in hits[0].sources


def test_kind_and_glob_filters(repo):
    assert all(hit.kind == "doc" for hit in _find(repo, "invoices rendered", kind="docs"))
    assert _paths(_find(repo, "invoices rendered", kind="docs")) == ["docs/guide.md"]
    assert all(hit.kind != "doc" for hit in _find(repo, "invoice", kind="code"))
    assert _paths(_find(repo, "invoice", path_glob="docs/*")) == ["docs/guide.md"]
    with pytest.raises(ValueError):
        _find(repo, "invoice", kind="everything")


def test_a_query_with_no_words_returns_nothing(repo):
    assert _find(repo, "???") == []


def test_fusion_rewards_agreement_between_generators():
    fused = rrf({"lexical": [10, 20, 30], "path": [20]})
    assert [chunk_id for chunk_id, _, _ in fused] == [20, 10, 30]
    assert fused[0][2] == ("lexical", "path")


def test_arrange_puts_code_first_and_caps_documentation():
    def hit(kind, n):
        return Hit(f"{kind}{n}", 1, 1, kind, "", "", "", "", 1.0 / (n + 1), ("lexical",))

    hits = [hit("doc", i) for i in range(6)] + [hit("block", i) for i in range(6)]
    arranged = arrange(hits, k=8, kind="any")
    assert [h.kind for h in arranged] == ["block"] * 6 + ["doc"] * 2
    assert len(arrange(hits, k=8, kind="docs")) == 8
    assert [h.kind for h in arrange(hits, k=1, kind="any")] == ["block"]  # one slot goes to code
    assert [h.kind for h in arrange(hits, k=3, kind="any")] == ["block", "block", "doc"]
    assert [h.kind for h in arrange(hits[:6] + hits[6:7], k=4, kind="any")] == ["block", "doc", "doc", "doc"]


def test_singular_and_plural_match(repo):
    assert "docs/guide.md" in _paths(_find(repo, "invoice", kind="docs"))
    assert "docs/guide.md" in _paths(_find(repo, "token expires"))


def test_file_names_with_spaces_and_non_ascii_letters_are_searchable(repo):
    repo.write("src/naïve module.py", "def wombat_counter():\n    return 3\n")
    assert _paths(_find(repo, "wombat_counter")) == ["src/naïve module.py"]
    by_name = _find(repo, "naïve")
    assert by_name[0].path == "src/naïve module.py" and "path" in by_name[0].sources


def test_query_full_of_search_syntax_is_treated_as_words(repo):
    hits = _find(repo, 'NEAR("render_invoice" OR total:*) AND (x) -y ^z')
    assert hits and hits[0].path == "src/billing/invoice.py"


def test_searching_an_empty_repository_returns_nothing(empty_repo):
    assert _find(empty_repo, "anything") == []


def test_text_in_other_scripts_is_searchable(repo):
    repo.write("docs/uebersicht.md", "# Übersicht\n\nDie Größe der Rechnung.\n")
    repo.write("src/greeting.py", "# Привет, мир\ndef greet():\n    return 'здравствуйте'\n")
    assert _paths(_find(repo, "Größe")) == ["docs/uebersicht.md"]
    assert _paths(_find(repo, "ubersicht")) == ["docs/uebersicht.md"]  # the index folds diacritics
    assert _paths(_find(repo, "привет")) == ["src/greeting.py"]


def test_filters_narrow_the_candidates_before_they_are_cut_off(repo):
    for i in range(90):  # far more matching code than the candidate pool holds
        repo.write(f"src/gen/client_{i}.py", f"def handle_invoice_{i}(invoice):\n    return invoice.total * {i}\n")
    assert _paths(_find(repo, "invoice", kind="docs")) == ["docs/guide.md"]
    assert _paths(_find(repo, "invoice", path_glob="docs/*")) == ["docs/guide.md"]
    assert _paths(_find(repo, "invoice", kind="code", path_glob="src/billing/*")) == ["src/billing/invoice.py"]
    assert len(_find(repo, "invoice", k=8)) == 8  # an unfiltered query after a filtered one sees everything again


def test_search_refuses_a_view_the_index_was_not_synced_to(repo):
    index = Index(open_db(index_path(repo.root)))
    main = build_view(repo.root)
    index.sync(main)
    assert search(index, main, "token store")
    repo.write("src/feature_only.py", "def quokka_feature():\n    return 1\n")
    edited = build_view(repo.root)
    with pytest.raises(ValueError, match="sync"):
        search(index, edited, "feature only")
    index.sync(edited)
    assert search(index, edited, "feature only")[0].path == "src/feature_only.py"
    index.sync(main)
    assert search(index, main, "feature only") == []


def test_a_word_that_cannot_tell_files_apart_is_not_a_path_match(repo):
    for i in range(40):
        repo.write(f"src/mod_{i}.py", f"value_{i} = {i}\n")
    assert not any("path" in hit.sources for hit in _find(repo, "py", k=50))   # every file's extension
    assert not any("path" in hit.sources for hit in _find(repo, "mod", k=50))  # forty files tie
    assert "path" in _find(repo, "invoice")[0].sources                         # one file is named for it


def test_the_same_bytes_under_two_treatments_are_chunked_once_for_each(repo):
    text = "# Notes\n\nThe walrusword lives here.\n"
    repo.write("notes.md", text)
    repo.write("notes.cfg", text)
    assert {hit.path: hit.kind for hit in _find(repo, "walrusword")} == {"notes.md": "doc", "notes.cfg": "block"}
    assert _paths(_find(repo, "walrusword", kind="code")) == ["notes.cfg"]
    assert _paths(_find(repo, "walrusword", kind="docs")) == ["notes.md"]
    assert _paths(_find(repo, "walrusword", path_glob="*.cfg")) == ["notes.cfg"]


def test_a_lockfiles_bytes_under_another_name_are_indexed(repo):
    repo.write("uv.lock", "ocelotword = 1\n")
    repo.write("config/settings.toml", "ocelotword = 1\n")
    assert _paths(_find(repo, "ocelotword")) == ["config/settings.toml"]
