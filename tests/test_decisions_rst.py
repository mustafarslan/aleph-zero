"""Decision records written in reStructuredText, as Python projects write them, are read like the Markdown ones."""
from aleph_zero import decisions
from aleph_zero.engine import Engine

MODULESTORE = """Limit Modulestore access to the courseware app
------------------------------------------------

Status
======

Accepted

Context
=======

Apps often query authored content through the Modulestore, which is slow and complex.

Decisions
=========

#. New features are implemented without calls to the Modulestore.
#. Apps push the data they need into their own models when a course is published.

Consequences
============

Tests are easier to write.
"""


def test_a_numbered_rst_file_among_the_decisions_is_a_record_and_a_template_or_an_index_is_not():
    assert decisions.is_record("docs/decisions/0011-limit-modulestore-use-in-lms.rst") == 11
    assert decisions.is_record("docs/decisions/0009_simplify_ratelimiting.rst") == 9
    assert decisions.is_record("lms/djangoapps/grades/docs/decisions/0001-events.rst") == 1
    assert decisions.is_record("docs/adr-0004-queues.rst") == 4                        # named as one, wherever it lies
    for not_one in ("docs/decisions/index.rst", "docs/decisions/README.rst", "docs/tutorial/01-intro.rst", "docs/decisions/conf.py",
                    "docs/decisions/0000-template.rst", "docs/decisions/00-index.rst", "docs/adr/adr000-template.md",
                    "docs/adr/0000-adr-template.md", "docs/decisions/0000_TEMPLATE.rst"):
        assert decisions.is_record(not_one) is None, not_one
    assert decisions.is_record("docs/adr/0007-templates-are-rendered-on-the-server.md") == 7      # a decision about templates is one


def test_title_status_and_the_decision_are_read_from_sections_whatever_marks_them():
    record = decisions.parse("docs/decisions/0011-limit-modulestore.rst", 11, MODULESTORE)
    assert (record.number, record.title, record.status) == (11, "Limit Modulestore access to the courseware app", "Accepted")
    assert record.decision.strip().startswith("#. New features are implemented") and "into their own models" in record.decision
    assert "Tests are easier" not in record.decision and "Apps often query" not in record.decision
    assert "Tests are easier" in record.text                                           # all of it is ranked
    over = decisions.parse("docs/decisions/0002-x.rst", 2, (
        "###############################\n0002 - Inter-app APIs are public\n###############################\n\n"
        "Status\n******\n\n**Deferred** as of September 2023.\n\nDecision\n********\n\nApps call each other through `api.py`.\n"))
    assert (over.title, over.status) == ("Inter-app APIs are public", "Deferred as of September 2023.")
    assert over.decision.strip() == "Apps call each other through `api.py`."
    dotted = decisions.parse("docs/decisions/0003-x.rst", 3, "3. Reduce the browser tests\n===========================\n\nText.\n")
    assert dotted.title == "Reduce the browser tests" and dotted.status == "" and dotted.decision == ""


def test_the_status_may_be_a_field_and_a_history_s_last_entry_is_the_status():
    fielded = decisions.parse("docs/decisions/0004-x.rst", 4, (
        "Settings are managed in one place\n=================================\n\n:Status: Accepted\n:Date: 2023-01-15\n\n"
        "Context\n-------\n\nSettings were everywhere.\n"))
    assert (fielded.title, fielded.status) == ("Settings are managed in one place", "Accepted")
    assert ":Date:" not in fielded.text and "2023-01-15" not in fielded.text          # a field is not prose
    history = decisions.parse("docs/decisions/0005-x.rst", 5, (
        "Subdomain boundaries\n====================\n\nStatus\n------\n\n* 2022-01: Proposed\n* 2023-09: Superseded by ADR 14\n\n"
        "Context\n-------\n\nText.\n"))
    assert history.status == "2023-09: Superseded by ADR 14"


def test_a_comment_before_the_title_is_not_the_title_even_when_it_holds_a_heading():
    record = decisions.parse("docs/decisions/0006-x.rst", 6, (
        "..\n   Internal notes\n   --------------\n   Not for the rendered page.\n\n.. _role-of-xblock:\n\n"
        "The role of XBlock\n==================\n\nDecision\n--------\n\nXBlocks render units.\n"))
    assert record.title == "The role of XBlock" and record.decision.strip() == "XBlocks render units."
    assert "Internal notes" not in record.text


def test_a_decision_with_parts_of_its_own_is_read_to_its_end_and_a_literal_block_does_not_cut_it():
    record = decisions.parse("docs/decisions/0007-x.rst", 7, (
        "Secret keys\n===========\n\nContext\n-------\n\nKeys leaked.\n\nDecision\n--------\n\nKeys are rotated.\n\n"
        "Backend\n~~~~~~~\n\nThe backend reads them from the vault.\n\nA template::\n\n   Heading\n   -------\n   text\n\n"
        "Frontend\n~~~~~~~~\n\nThe frontend never sees them.\n\nConsequences\n------------\n\nRotation is a deploy.\n"))
    assert "Keys are rotated." in record.decision and "reads them from the vault" in record.decision
    assert "never sees them" in record.decision and "Heading" in record.decision      # its own parts, and the literal block, are in it
    assert "Rotation is a deploy" not in record.decision and "Keys leaked" not in record.decision


def test_the_scope_of_an_rst_record_governs_and_is_not_ranked():
    record = decisions.parse("docs/decisions/0008-x.rst", 8, (
        "Rate limits\n===========\n\n:applies-to: [src/app/api/*, src/app/limits.py]\n:status: accepted\n\nDecision\n--------\n\nOne limiter.\n"))
    assert record.applies_to == ("src/app/api/*", "src/app/limits.py") and record.status == "accepted"
    assert "src/app/limits.py" not in record.text
    assert [hit.number for hit in decisions.Records([record]).governing("src/app/api/views.py")] == [8]


def test_what_is_embedded_for_an_rst_record_leads_with_its_title_and_its_decision():
    record = decisions.parse("docs/decisions/0011-limit-modulestore.rst", 11, MODULESTORE)
    assert decisions.record_text(record).startswith("Limit Modulestore access to the courseware app\n\n#. New features")


def test_a_file_that_is_not_shaped_as_a_record_is_still_read_without_failing():
    for odd in ("", "\n\n", "no heading at all, only a paragraph\n", "====\n", "Title\n==\n", ".. only a comment\n", "\x00\xff" * 10,
                "A\n=\n\nDecision\n", "Decision\n--------\n"):
        record = decisions.parse("docs/decisions/0012-odd.rst", 12, odd)
        assert record.number == 12 and isinstance(record.title, str) and record.title


def test_the_engine_lists_and_ranks_rst_records_beside_markdown_ones(repo):
    repo.write("docs/decisions/0011-limit-modulestore.rst", MODULESTORE)
    repo.write("docs/decisions/0012-queues.md", "# 12. Queues carry the work\n\n## Decision\n\nA queue per kind of work.\n")
    repo.write("docs/decisions/index.rst", "Decisions\n=========\n\n.. toctree::\n\n   0011-limit-modulestore\n")
    repo.write("docs/decisions/0000-template.rst", "Title\n=====\n\nStatus\n------\n\nProposed\n")
    engine = Engine(repo.root)
    listed = engine.decisions("")
    assert [(hit.number, hit.title, hit.status) for hit in listed.decisions] == [
        (11, "Limit Modulestore access to the courseware app", "Accepted"), (12, "Queues carry the work", "")]
    assert [hit.number for hit in engine.decisions("stop calling the modulestore from new features", rrf_k=0).decisions][0] == 11


def test_a_record_that_opens_with_its_status_is_called_by_its_file_s_name():
    record = decisions.parse("docs/decisions/0003-reduce-bokchoy-testing.rst", 3, (
        "Status\n======\nAccepted\n\n\nContext\n=======\n\nThe browser tests are slow.\n\nDecision\n========\n\nWe remove most of them.\n"))
    assert (record.title, record.status) == ("reduce bokchoy testing", "Accepted")
    assert record.decision.strip() == "We remove most of them."
    under = decisions.parse("docs/decisions/0009_simplify_ratelimiting.rst", 9, "Context\n-------\n\nText.\n")
    assert under.title == "simplify ratelimiting"


def test_a_decision_s_heading_may_say_more_than_decision():
    both = decisions.parse("docs/decisions/0021-x.rst", 21, (
        "Fixing the checks\n=================\n\nDecision & Consequences\n-----------------------\n\nThe checks run in CI.\n\nRejected\n--------\n\nNo.\n"))
    assert both.decision.strip() == "The checks run in CI."
    named = decisions.parse("docs/decisions/0018-x.rst", 18, (
        "Translation files\n=================\n\nDecision: only two files\n------------------------\n\nTwo files.\n"))
    assert named.decision.strip() == "Two files."
    drivers = decisions.parse("docs/decisions/0030-x.rst", 30, (
        "Queues\n======\n\nDecision Drivers\n----------------\n\nCost.\n\nDecision Outcome\n----------------\n\nA queue per kind of work.\n"))
    assert drivers.decision.strip() == "A queue per kind of work."


def test_a_title_that_begins_with_the_name_of_a_part_is_still_the_title():
    record = decisions.parse("docs/decisions/0015-tasks.rst", 15, "Background Tasks for Workers\n============================\n\nText.\n")
    assert record.title == "Background Tasks for Workers"
    assert decisions.parse("docs/decisions/0016-x.rst", 16, "Context switching is costly\n===========================\n\nText.\n").title == "Context switching is costly"
    assert decisions.parse("docs/decisions/0017-the-name.rst", 17, "Context and Problem Statement\n=============================\n\nText.\n").title == "the name"


def test_a_status_written_as_a_line_of_its_own_is_read():
    record = decisions.parse("docs/decisions/0024-x.rst", 24, "Course Component Types\n######################\n\n**Status**: Proposed\n\nContext\n*******\n\nText.\n")
    assert record.status == "Proposed"
    assert decisions.parse("docs/decisions/0025-x.rst", 25, "Title here\n==========\n\nStatus: **Accepted** *(2025)*\n\nText.\n").status == "Accepted (2025)"


def test_a_decision_ends_at_consequences_however_that_is_marked():
    record = decisions.parse("docs/decisions/0031-x.rst", 31, (
        "Messaging\n#########\n\nDecision\n========\nUse Kafka.\n\nTopics\n~~~~~~\n\nOne per service.\n\nConsequences\n------------\nHigher latency.\n\n"
        "Rejected Alternatives\n---------------------\nPolling.\n"))
    assert "Use Kafka." in record.decision and "One per service." in record.decision
    assert "Higher latency" not in record.decision and "Polling" not in record.decision


def test_a_field_that_describes_is_ranked_and_one_that_files_is_not():
    record = decisions.parse("docs/decisions/0032-x.rst", 32, (
        "Serializers\n===========\n\n:Technical Story: Serializer standardization across the public API\n:Date: 2025-02-01\n:Authors: A. Person\n"
        ":Deciders: The API group\n\nContext\n-------\n\nText.\n"))
    assert "Serializer standardization across the public API" in record.text
    assert "2025-02-01" not in record.text and "A. Person" not in record.text and "The API group" not in record.text
