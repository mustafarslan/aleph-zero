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


def test_a_repeated_candidate_votes_once():
    fused = rrf({"path": [5, 5, 5], "lexical": [7]})
    assert sorted(score for _, score, _ in fused) == [1 / 61, 1 / 61]


def test_files_that_share_their_first_lines_do_not_multiply_a_path_match(repo):
    opening = "import { RuntimeException } from '@core/internal';\n\n"
    for name in ("invalid-grpc-package", "invalid-grpc-message", "invalid-kafka-topic",
                 "invalid-grpc-service", "invalid-grpc-definition", "invalid-message"):
        class_name = "".join(part.title() for part in name.split("-")) + "Exception"
        repo.write(f"errors/{name}.exception.ts", f"{opening}export class {class_name} extends RuntimeException {{}}\n")
    for i in range(20):  # the same first lines in files whose paths say nothing
        repo.write(f"other/unit_{i}.ts", f"{opening}export class Unit{i} {{}}\n")
    repo.write("exceptions/grpc-exception.ts",
               "export class GrpcCancelledException extends GrpcException {\n  constructor() { super(1); }\n}\n")
    hits = _find(repo, "GrpcCancelledException", k=30)
    assert hits[0].path == "exceptions/grpc-exception.ts" and hits[0].symbol == "GrpcCancelledException"
    assert sum("path" in hit.sources for hit in hits) <= 5
    # The shared opening line also matches the word "exception", in all 26 files. Identical
    # text is shown a few times and the rest are counted, not listed.
    shared = [hit for hit in hits if hit.text.startswith("import { RuntimeException }")]
    assert len(shared) == 3 and shared[0].duplicates == 23
    assert all("path" in hit.sources for hit in shared)  # the files whose path matched are the ones shown


def test_a_private_member_is_found_with_or_without_its_marker(repo):
    repo.write("web/config.js", "class Config {\n  #normalizeRules(input) { return input; }\n}\n")
    assert _find(repo, "#normalizeRules")[0].symbol == "#normalizeRules"
    assert _find(repo, "normalizeRules")[0].symbol == "#normalizeRules"
    assert "symbol" in _find(repo, "normalizeRules")[0].sources


def test_copies_are_counted_by_file_not_by_occurrence(repo):
    body = "    record_metric('wallaby')\n" * 400  # every piece of this function has the same text
    repo.write("src/repeat.py", f"def repeater():\n    pass\n\n\ndef second():\n{body}    return 0\n")
    hits = [hit for hit in _find(repo, "wallaby", k=20) if hit.path == "src/repeat.py"]
    assert len(hits) == 1 and hits[0].duplicates == 0


def _synced(repo):
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    return index, view


def _many_functions(repo, count=40):
    """Enough other code that a plain word of the question does not find the wanted file by itself."""
    for n in range(count):
        repo.write(f"src/noise/part{n}.py", f"def helper_{n}(value):\n    # stores a session and returns the value\n    return value + {n}\n")


def test_a_name_written_with_what_it_is_called_on_is_looked_up_without_it(repo, monkeypatch):
    # `self.rotate`, `store.rotate`, `auth.TokenStore.rotate`: the part before the dot is a variable or a module, which
    # the definition's own qualified name does not hold. The whole dotted name found nothing and proposed nothing.
    import aleph_zero.search as ranker

    _many_functions(repo)
    repo.write("src/auth/rotation.py", "class TokenStore:\n    def rotate(self, session):\n        return session\n")
    index, view = _synced(repo)
    for written in ("why does self.rotate lose the session", "calling store.rotate twice", "auth.TokenStore.rotate breaks", "TokenStore::rotate breaks"):
        monkeypatch.setattr(ranker, "DOTTED_FALLBACK", False)
        assert ranker.symbol_ranking(index, written, False)[0] == [] or "TokenStore" in written      # as it was
        monkeypatch.setattr(ranker, "DOTTED_FALLBACK", True)
        chunks, alone = ranker.symbol_ranking(index, written, False)
        assert len(chunks) == 1 and not alone, written
        (text,), = index.conn.execute("SELECT text FROM chunks WHERE chunk_id = ?", chunks)
        assert "def rotate" in text
    # A name that is defined as written is found as before, and what stands before the dot never widens it.
    assert ranker.symbol_ranking(index, "TokenStore.rotate breaks", False)[0] == chunks
    # The last part alone names too many definitions to mean one: nothing is proposed, as for any such word.
    for n in range(ranker.SYMBOL_TIE_LIMIT + 1):
        repo.write(f"src/many/get{n}.py", f"def get():\n    return {n}\n")
    index, view = _synced(repo)
    assert ranker.symbol_ranking(index, "what does cache.get return here", False)[0] == []


def test_a_file_s_path_vote_goes_to_what_was_found_in_it(repo, monkeypatch):
    # The file's path matching the question used to vote for the file's first chunk only: the file came twice, once as
    # its first lines and once as the function that was found, and the function had one generator's word for it.
    import aleph_zero.search as ranker

    _many_functions(repo)
    repo.write("src/billing/receipt_printer.py",
               "import os\nimport sys\n\n\ndef unrelated_helper():\n    return os.sep\n\n\n" + "\n\n".join(
                   f"def filler_{n}():\n    return {n}\n" for n in range(6)) + "\n\ndef margin_width(paper):\n    return paper.width - 2\n")
    index, view = _synced(repo)
    question = "receipt printer margin width is wrong"
    monkeypatch.setattr(ranker, "PATH_VOTE", "first")
    before = [hit for hit in search(index, view, question, k=10) if hit.path == "src/billing/receipt_printer.py"]
    assert any("path" in hit.sources and "margin_width" not in hit.text for hit in before)      # the first lines carried the vote
    monkeypatch.setattr(ranker, "PATH_VOTE", "found")
    after = search(index, view, question, k=10)
    top = after[0]
    assert top.path == "src/billing/receipt_printer.py" and "def margin_width" in top.text and "path" in top.sources and len(top.sources) >= 2
    assert sum(hit.path == top.path and "path" in hit.sources for hit in after) == 1            # one vote, one place
    # A file found by its path alone is still shown by its first lines.
    monkeypatch.setattr(ranker, "PATH_VOTE", "found")
    alone = search(index, view, "receipt printer", k=10)
    assert alone and alone[0].path == "src/billing/receipt_printer.py" and alone[0].start_line == 1


def test_one_result_a_file_keeps_the_file_s_other_places_on_its_line(repo, monkeypatch):
    # Ten results named about six files: a file's second and third places took the places of other files. With one
    # result a file the ten name ten files, and what the file's other results were is kept beside its first.
    import aleph_zero.search as ranker
    from aleph_zero import render

    repo.write("src/billing/ledger.py", "".join(f"def invoice_total_{n}(order):\n    return order.invoice_total + {n}\n\n\n" for n in range(4)))
    for n in range(4):
        repo.write(f"src/billing/other{n}.py", f"def invoice_total_elsewhere_{n}(order):\n    return order.invoice_total * {n}\n")
    index, view = _synced(repo)
    monkeypatch.setattr(ranker, "ONE_A_FILE", False)
    before = search(index, view, "invoice total", k=4)
    assert [hit.path for hit in before].count("src/billing/ledger.py") >= 2
    monkeypatch.setattr(ranker, "ONE_A_FILE", True)
    after = search(index, view, "invoice total", k=4)
    assert len({hit.path for hit in after}) == len(after) == 4
    ledger = next(hit for hit in after if hit.path == "src/billing/ledger.py")
    assert 1 <= len(ledger.places) <= ranker.MORE_PLACES
    monkeypatch.setattr(ranker, "ONE_A_FILE", False)
    every = [(hit.start_line, hit.end_line) for hit in search(index, view, "invoice total", k=40) if hit.path == ledger.path]
    assert [(start, end) for start, end, _ in ledger.places] == every[1:1 + ranker.MORE_PLACES]     # its next places, in order
    shown = render.text(render.Answer("invoice total", after, 0, False, 1))
    assert "also at:" in shown and str(ledger.places[0][0]) in shown
