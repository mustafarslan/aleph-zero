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
