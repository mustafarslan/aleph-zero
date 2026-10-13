"""Trials of the ranking are read as a verdict is: by main language, each language in the arm it ships with."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import trial  # noqa: E402

from aleph_zero import search as ranker  # noqa: E402


TRIALS = {"one": {"RRF_K": 30}, "other": {"MIN_POOL": 100}}      # switches that exist, for the arithmetic of a reading
ARMS = trial.arms(TRIALS)


def _entry(language, words, vectors):
    score = lambda old, new: {"recall@10": new if True else old, "recall@1": 0.0, "mrr": 0.0}  # noqa: E731
    arms = {"as it is": {form: {"lexical": score(0, words[0]), "hybrid": score(0, vectors[0])} for form in trial.FORMS}}
    for arm in ARMS:
        if arm != "as it is":
            arms[arm] = {form: {"lexical": score(0, words[1]), "hybrid": score(0, vectors[1])} for form in trial.FORMS}
    return {"repo": f"o/{language}", "main_language": language, "arms": arms}


def test_each_language_is_read_in_the_arm_it_ships_with_and_all_of_them_together(monkeypatch):
    # Python ships with words and names, C++ with vectors: a trial that helps words only helps Python's questions.
    scored = {f"py-{n}": _entry("python", (0.0, 1.0), (1.0, 1.0)) for n in range(20)}
    scored.update({f"cc-{n}": _entry("cpp", (0.0, 1.0), (1.0, 1.0)) for n in range(20)})
    monkeypatch.setattr(trial, "ARMS", ARMS)
    report = trial.read(scored)["one"]
    assert report["python"]["ships with"] == "words and names" and report["python"]["first line"]["recall@10"]["gain"] == 1.0
    assert report["cpp"]["ships with"] == "vectors" and report["cpp"]["first line"]["recall@10"]["gain"] == 0.0
    assert report["cpp"]["first line, the other arm"]["recall@10"]["gain"] == 1.0
    together = report["all, each in its shipped arm"]["first line"]
    assert together["questions"] == 40 and together["recall@10"]["gain"] == 0.5 and together["recall@10"]["better"] == 20


def test_a_trial_s_switches_are_set_for_the_run_and_put_back():
    assert list(ARMS) == ["as it is", "one", "other", "all"] and list(trial.arms({"one": {}})) == ["as it is", "one"]
    before = (ranker.RRF_K, ranker.MIN_POOL)
    with trial.switched(ARMS["all"]):
        assert (ranker.RRF_K, ranker.MIN_POOL) == (30, 100)
    assert (ranker.RRF_K, ranker.MIN_POOL) == before
    assert trial.TRIALS == {}                   # none is waiting to be read: the last two were not adopted (ADR-002)
