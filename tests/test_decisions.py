"""Which decision governs a change: decision records ranked as whole documents for a task."""
import json

from aleph_zero import cli
from aleph_zero.engine import Engine

BUS = ("# 12. Use an event bus\n\nDate: 2026-01-02\n\n## Status\n\nAccepted\n\n## Context\n\nServices call each other directly and retries are everywhere.\n\n"
       "## Decision\n\nWe publish domain events on a bus. Consumers subscribe; delivery is at least once, so handlers are idempotent.\n")
LOADER = ("# 13. One parser for decisions\n\n## Status\n\nSuperseded by ADR-14\n\n## Decision\n\n`load_decisions` is the only reader of the ADR directory. "
          "Nothing else parses front matter.\n")
STORAGE = ("---\nid: ADR-0014\ntitle: Postgres for everything\nstatus: accepted\ntags: [storage, database]\n---\n\n"
           "We keep relational data in Postgres and do not add a second database for queues.\n")


def project(repo):
    repo.write("docs/adr/0012-use-an-event-bus.md", BUS)
    repo.write("docs/adr/0013-one-parser.md", LOADER)
    repo.write("docs/adr/0014-postgres.md", STORAGE)
    repo.write("docs/adr/README.md", "# Decisions\n\nAn index of the event bus, the parser and Postgres decisions.\n")
    repo.write("docs/guide/0001-not-a-decision.md", "# 1. A guide that happens to be numbered\n\nevent bus event bus\n")
    repo.write("docs/decisions/ADR-020-feature-flags.md", "# ADR-020: Feature flags are data\n\nStatus: proposed\n\nFlags live in the database, not in code.\n")
    return Engine(repo.root)


def test_the_decisions_of_a_view_are_the_numbered_records_with_their_number_title_and_status(repo):
    found = {hit.number: (hit.path, hit.title, hit.status) for hit in project(repo).decisions("", k=50).decisions}
    assert found == {
        12: ("docs/adr/0012-use-an-event-bus.md", "Use an event bus", "Accepted"),
        13: ("docs/adr/0013-one-parser.md", "One parser for decisions", "Superseded by ADR-14"),
        14: ("docs/adr/0014-postgres.md", "Postgres for everything", "accepted"),
        20: ("docs/decisions/ADR-020-feature-flags.md", "Feature flags are data", "proposed"),
    }           # not the README, and not a numbered file outside a directory of decisions


def test_a_task_is_ranked_against_whole_documents_with_all_its_words(repo):
    engine = project(repo)
    task = "Make the order handler idempotent, since the bus may deliver an event twice after retries"
    assert [hit.number for hit in engine.decisions(task).decisions][:1] == [12]
    assert [hit.number for hit in engine.decisions("fix the ADR loader so that it reads front matter once").decisions][:1] == [13]  # load_decisions
    assert [hit.number for hit in engine.decisions("should job queues get a second database, or stay in Postgres").decisions][0] == 14
    assert engine.decisions("zzz qqq").decisions == []                  # shares no word with any decision: none, not all of them


def test_an_edited_or_removed_decision_is_ranked_as_it_is_now(repo):
    engine = project(repo)
    assert engine.decisions("kafka partitions").decisions == []
    repo.write("docs/adr/0012-use-an-event-bus.md", BUS + "\nThe bus is Kafka; one partition per aggregate.\n")
    assert [hit.number for hit in engine.decisions("kafka partitions").decisions] == [12]
    (repo.root / "docs/adr/0012-use-an-event-bus.md").unlink()
    assert engine.decisions("kafka partitions").decisions == []


def test_the_answer_is_short_and_says_when_there_is_nothing_to_rank(repo, capsys):
    from aleph_zero import decisions

    engine = project(repo)
    text = decisions.text(engine.decisions("publish events on the bus"))
    assert text.splitlines()[0] == "docs/adr/0012-use-an-event-bus.md  ADR 12  Use an event bus  [Accepted]"
    assert len(text) < 600
    assert cli.main(["--cwd", str(repo.root), "decisions", "publish events on the bus", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decisions"][0]["number"] == 12
    for path in list((repo.root / "docs").rglob("*.md")):
        path.unlink()
    assert "no decision records" in decisions.text(Engine(repo.root).decisions("anything"))


def test_ranking_is_the_same_every_time_also_between_equal_scores(repo):
    repo.write("docs/adr/0002-b.md", "# 2. Second\n\nthe same words here\n")
    repo.write("docs/adr/0001-a.md", "# 1. First\n\nthe same words here\n")
    assert [hit.number for hit in Engine(repo.root).decisions("same words").decisions] == [1, 2]


def test_a_file_named_as_a_decision_is_one_wherever_it_lies(repo):
    repo.write("docs/adr-004-blue-green-migration.md", "# ADR-004: Blue-green migration\n\n**Status:** accepted\n\nTwo environments, one switch.\n")
    repo.write("docs/0007-release-notes.md", "# 7. Release notes\n\nNot a decision: numbered, but neither named ADR nor in a directory of decisions.\n")
    found = [(hit.path, hit.number, hit.title, hit.status) for hit in Engine(repo.root).decisions("", k=10).decisions]
    assert found == [("docs/adr-004-blue-green-migration.md", 4, "Blue-green migration", "accepted")]


ADR_WITH_SCOPE = ('---\nid: ADR-0003\ntitle: Hooks enforce, the server informs\nstatus: accepted\n'
                  'tags: [hooks]\napplies-to: ["src/app/cli.py", "src/app/rules/*"]\n---\n\n# ADR-0003\n\nHooks enforce.\n')
ADR_BLOCK_SCOPE = '---\nid: ADR-0004\ntitle: Metrics are computed once\nstatus: proposed\napplies-to:\n  - "src/app/metrics/*"\n---\n\nOnce.\n'
LAYERS = ('layers:\n  surfaces: ["src/app/cli.py"]\n  analysis: ["src/app/metrics/*", "src/app/rules/*"]\n  foundation: ["src/app/config.py"]\n'
          'contracts:\n  - name: outside-in\n    kind: layered\n    order: [surfaces, analysis, foundation]\n')


def test_which_records_govern_a_file_and_which_layer_it_is_in(repo):
    from aleph_zero import decisions
    from aleph_zero.engine import Engine

    repo.write("docs/adr/0003-hooks-enforce.md", ADR_WITH_SCOPE)
    repo.write("docs/adr/0004-metrics-once.md", ADR_BLOCK_SCOPE)
    repo.write("docs/adr/0005-no-scope.md", "# 5. Nothing in particular\n\n## Status\n\nAccepted\n")
    repo.write("oxn.yaml", LAYERS)
    repo.write("src/app/rules/size.py", "LIMIT = 1\n")
    engine = Engine(repo.root)
    answer = engine.decisions("", path="src/app/rules/size.py")
    assert [(hit.number, hit.status) for hit in answer.governing] == [(3, "accepted")]
    assert answer.place == ["layer analysis (contract outside-in): may depend on foundation; not on surfaces"]
    assert decisions.text(answer).split("\n")[:3] == [
        "src/app/rules/size.py is governed by:",
        "docs/adr/0003-hooks-enforce.md  ADR 3  Hooks enforce, the server informs  [accepted]",
        "layer analysis (contract outside-in): may depend on foundation; not on surfaces"]
    assert [hit.number for hit in engine.decisions("", path=str(repo.root / "src/app/metrics/x.py")).governing] == [4]
    nowhere = engine.decisions("hooks", path="README.md")
    assert nowhere.governing == [] and nowhere.place == ["in no layer of oxn.yaml"]
    assert decisions.text(nowhere).startswith("README.md: no decision record names it in its applies-to\nin no layer of oxn.yaml\n\ndocs/adr/0003")
    assert engine.decisions("hooks").place == [] and "governed" not in decisions.text(engine.decisions("hooks"))


def test_a_record_s_scope_is_not_ranked(repo):
    from aleph_zero import decisions

    with_scope = decisions.parse("docs/adr/0003-x.md", 3, ADR_WITH_SCOPE)
    assert with_scope.applies_to == ("src/app/cli.py", "src/app/rules/*") and "src/app/cli.py" not in with_scope.text


def test_a_file_two_layers_name_is_in_the_first_as_oxn_has_it():
    from aleph_zero import architecture

    layers, contracts = architecture.read(
        "layers:\n  orchestration: [\"src/graph/indexer.py\"]\n  graph: [\"src/graph/*\"]\n"
        "contracts:\n  - name: c\n    kind: layered\n    order: [orchestration, graph]\n")
    assert architecture.place("src/graph/indexer.py", layers, contracts) == ["layer orchestration (contract c): may depend on graph"]
    assert architecture.place("src/graph/store.py", layers, contracts) == ["layer graph (contract c): may depend on no other layer; not on orchestration"]


def test_words_that_differ_only_in_their_ending_are_one_word():
    from aleph_zero.decisions import stem

    for same in (("cache", "caches", "caching", "cached"), ("policy", "policies"), ("class", "classes"), ("bus", "buses"),
                 ("status", "statuses"), ("need", "needs", "needed"), ("add", "adds", "added", "adding"), ("map", "mapped", "mapping"),
                 ("store", "stores", "stored", "storing"), ("case", "cases"), ("index", "indexes", "indexed", "indexing"),
                 ("call", "calls", "called", "calling"), ("embed", "embedded", "embedding"), ("service", "services")):
        assert len({stem(word) for word in same}) == 1, same
    for kept in ("string", "thing", "redis", "status", "bus", "is", "its", "http2", "sqs", "less"):
        assert stem(kept) == kept, kept
    assert stem("string") != stem("str") and stem("bus") != stem("bu") and stem("analysis") == "analysis"


def test_a_ranker_that_stems_finds_the_record_by_another_form_of_its_word():
    from aleph_zero import decisions

    records = [decisions.parse("docs/adr/0001-a.md", 1, "# 1. Responses are cached at the edge\n\nWe cache.\n"),
               decisions.parse("docs/adr/0002-b.md", 2, "# 2. Queues carry the work\n\nA queue.\n")]
    assert [hit.number for hit in decisions.Records(records, stems=False).rank("caching of a response")] == [2]    # only "a" is shared
    assert [hit.number for hit in decisions.Records(records, stems=True).rank("caching of a response")] == [1, 2]


def test_the_ranking_that_was_measured_is_the_default():
    """The figure on oxn's pairs is of words alone with BM25's usual constants: the switches tried later stay off."""
    from aleph_zero import decisions

    assert (decisions.STEMS, decisions.K1, decisions.B) == (False, 1.5, 0.75)
    records = [decisions.parse(f"docs/adr/000{n}-x.md", n, text) for n, text in enumerate((
        "# 1. Responses are cached at the edge\n\nWe cache responses. Caching is cheap.\n",
        "# 2. Queues carry the work\n\nA queue per kind of work, and a cache of results for the queue.\n" + "More about queues. " * 40,
        "# 3. The cache is invalidated by events\n\nEvents.\n"), 1)]
    task = "invalidate the response cache when an order event arrives"
    default = [(hit.number, hit.score) for hit in decisions.Records(records).rank(task)]
    assert default == [(hit.number, hit.score) for hit in decisions.Records(records, stems=False, b=0.75).rank(task)]
    assert [number for number, _ in default] == [3, 1, 2]
    assert default != [(hit.number, hit.score) for hit in decisions.Records(records, stems=True).rank(task)]
    assert default != [(hit.number, hit.score) for hit in decisions.Records(records, b=0.4).rank(task)]
