"""The grader for call edges: its population, its exclusions and its measures (oxn's protocol)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_grade as gg  # noqa: E402
from test_scip import document, index, occurrence  # noqa: E402

from aleph_zero import scip  # noqa: E402

SOURCE = (
    "from lib import outside\n"          # line 0
    "def helper(x):\n"                   # 1
    "    return x\n"                     # 2
    "def other(x):\n"                    # 3
    "    return x\n"                     # 4
    "def run():\n"                       # 5
    "    helper(1)\n"                    # 6: resolved in the tree
    "    other(2)\n"                     # 7: resolved in the tree
    "    outside(3)\n"                   # 8: resolved outside the tree
    "    unknown(4)\n"                   # 9: SCIP has nothing
    "    wrong(5)\n"                     # 10: SCIP names something else than what is written
)
HELPER, OTHER = "scip-python python demo 0.1 pkg/a/helper().", "scip-python python demo 0.1 pkg/a/other()."


def corpus(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(SOURCE)
    data = index(document(
        "pkg/a.py",
        occurrence(HELPER, [1, 4, 10], scip.ROLE_DEFINITION), occurrence(OTHER, [3, 4, 9], scip.ROLE_DEFINITION),
        occurrence(HELPER, [6, 4, 10]), occurrence(OTHER, [7, 4, 9]),
        occurrence("scip-python python lib 1.0 lib/outside().", [8, 4, 11]),
        occurrence(HELPER, [10, 4, 9]),
    ))
    return tmp_path, scip.parse_index(data)


def test_the_population_is_what_scip_resolved_in_the_tree_and_names_as_written(tmp_path):
    root, parsed = corpus(tmp_path)
    grade = gg.grade_corpus(root, parsed, "python")
    assert (grade.graded, grade.excluded_untrustworthy) == (2, 1)
    assert (grade.answered, grade.correct) == (0, 0)
    assert grade.as_dict()["recall"] == 0.0 and grade.as_dict()["confident_precision"] == 0.0


def test_answers_are_scored_with_oxns_definitions(tmp_path):
    root, parsed = corpus(tmp_path)
    helper = gg.Candidate("pkg/a.py", "helper", 2, 3)
    other = gg.Candidate("pkg/a.py", "other", 4, 5)

    def answer(path, call):
        if call.name == "helper":
            return [helper]                 # one candidate, and the right one
        if call.name == "other":
            return [helper, other]          # two candidates, the first one wrong
        return [helper]                     # answers for sites outside the population are never looked at

    grade = gg.grade_corpus(root, parsed, "python", answer).as_dict()
    assert (grade["answered"], grade["correct"]) == (2, 1)
    assert (grade["recall"], grade["precision"]) == (1.0, 0.5)
    assert (grade["confident_answered"], grade["confident_precision"], grade["confident_recall"]) == (1, 1.0, 0.5)


def test_a_candidate_is_right_only_for_the_same_file_name_and_place():
    truth = gg.Truth("pkg/a.py", 1, "helper")
    assert gg.is_correct(gg.Candidate("pkg/a.py", "helper", 2, 3), truth)
    assert not gg.is_correct(gg.Candidate("pkg/b.py", "helper", 2, 3), truth)
    assert not gg.is_correct(gg.Candidate("pkg/a.py", "helper", 9, 12), truth)
    assert not gg.is_correct(gg.Candidate("pkg/a.py", "other", 2, 3), truth)
    assert gg.is_correct(gg.Candidate("pkg/a.py", "anything", 2, 3), gg.Truth("pkg/a.py", 1, ""))   # a local symbol carries no name


def test_the_name_a_symbol_ends_in():
    assert gg.descriptor_name("scip-python python demo 0.1 pkg/a/helper().") == "helper"
    assert gg.descriptor_name("scip-python python httpx 0.1 httpx/_client/Client#send().") == "send"
    assert gg.descriptor_name("scip-python python httpx 0.1 httpx/_client/Client#") == "Client"
    assert gg.descriptor_name("scip-typescript npm pkg 1.0 src/`a-b.ts`/run().") == "run"
    assert gg.descriptor_name("local 12") == ""
    assert gg.descriptor_name("scip-typescript npm pkg 1.0 src/`a.ts`/Box#`<get>size`().") == "size"
    # A constructor is written as its type at the call site, or as itself through super().
    init = "scip-python python httpx 0.1 httpx/_client/Client#__init__()."
    assert (gg.descriptor_name(init), gg.constructed_type(init)) == ("__init__", "Client")
    assert gg.constructed_type("semanticdb maven . . org/demo/Owner#`<init>`().") == "Owner"
    assert gg.constructed_type("scip-typescript npm pkg 1.0 src/`a.ts`/Widget#`<constructor>`().") == "Widget"
    assert gg.constructed_type("scip-python python demo 0.1 pkg/a/helper().") == ""
    truth = gg.Truth("a.py", 10, "__init__", "Client")
    assert gg.is_correct(gg.Candidate("a.py", "Client", 5, 40), truth) and gg.is_correct(gg.Candidate("a.py", "__init__", 10, 14), truth)


def test_an_answer_at_another_place_the_index_defines_the_same_symbol_is_counted_apart():
    """scip-python names a function inside a function without the function around it: two of them in one class
    share a symbol. The protocol's truth is the first; an answer at the second is wrong by it and counted here."""
    truth = gg.Truth("t.py", 4, "generate", also=(("t.py", 18),))
    first, second, neither = gg.Candidate("t.py", "generate", 5, 7), gg.Candidate("t.py", "generate", 19, 22), gg.Candidate("t.py", "generate", 40, 41)
    assert gg.is_correct(first, truth) and not gg.is_correct(second, truth)       # the protocol does not change
    assert not gg.at_another_definition(first, truth) and gg.at_another_definition(second, truth)
    assert not gg.at_another_definition(neither, truth)
    assert not gg.at_another_definition(gg.Candidate("t.py", "other", 19, 22), truth)
