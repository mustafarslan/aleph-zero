"""The A/B harness reads a session's event stream; these check the reading, not Claude."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "ab"))
import run as ab  # noqa: E402


def _stream(*events) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\nnot json at all\n"


def _use(name, parent=None, **inputs):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"content": [{"type": "tool_use", "name": name, "input": inputs}]}}


RESULT = {"type": "result", "is_error": False, "terminal_reason": "completed", "num_turns": 5, "duration_ms": 4200,
          "total_cost_usd": 0.02, "result": 'It is in the store.\n{"files": ["src/auth/store.py", "./src/x.py"]}',
          "modelUsage": {"m-main": {"inputTokens": 10, "outputTokens": 200, "cacheReadInputTokens": 3000, "cacheCreationInputTokens": 400},
                         "m-sub": {"inputTokens": 5, "outputTokens": 50, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 0}}}


def test_a_session_is_read_from_its_stream():
    text = _stream(
        _use("mcp__plugin_aleph-zero_aleph__search", query="token rotation"),
        _use("Bash", command="grep -rn rotate src"),
        _use("Read", parent="toolu_1", file_path="/tmp/w/repo/src/other.py"),
        _use("Read", file_path="/tmp/w/repo/src/auth/store.py"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking aloud"}]}},
        RESULT,
    )
    record = ab.read_stream(text, ["src/auth/store.py"])
    assert record["ok"] and record["ended"] == "completed"
    assert record["tokens"] == 3765 and record["tokens_by_kind"]["cacheReadInputTokens"] == 3100   # every model counted
    assert record["tool_calls"] == 4 and record["tools"]["Read"] == 2
    assert (record["aleph_calls"], record["grep_calls"], record["first_search"]) == (1, 1, "aleph")
    assert record["calls_before_first_gold_read"] == 3
    assert record["named"] == ["src/auth/store.py", "src/x.py"] and record["file_hit"] and record["file_recall"] == 1.0
    assert not record["gave_up"]


def test_a_session_that_named_no_file_or_never_finished_is_marked():
    silent = ab.read_stream(_stream({**RESULT, "result": "I could not find it."}), ["a.py"])
    assert silent["gave_up"] and not silent["file_hit"] and silent["named"] == []
    cut = ab.read_stream(_stream(_use("Grep", pattern="x")), ["a.py"])
    assert not cut["ok"] and cut["ended"] == "no result event" and cut["tool_calls"] == 1
    assert ab.read_stream("", ["a.py"])["ok"] is False


def test_a_file_is_a_gold_read_only_when_the_whole_path_matches():
    assert ab._reads_gold({"name": "Read", "input": {"file_path": "/w/repo/src/store.py"}}, ["src/store.py"])
    assert not ab._reads_gold({"name": "Read", "input": {"file_path": "/w/repo/src/token_store.py"}}, ["store.py"])
    assert ab._reads_gold({"name": "mcp__plugin_aleph-zero_aleph__outline", "input": {"path": "src/store.py"}}, ["src/store.py"])
    assert not ab._reads_gold({"name": "Grep", "input": {"path": "src/store.py"}}, ["src/store.py"])


def test_the_report_pairs_arms_by_task_and_counts_hits():
    def row(task, arm, tokens, calls, hit):
        return {"task": task, "arm": arm, "ok": True, "tokens": tokens, "tool_calls": calls, "file_hit": hit,
                "gave_up": False, "first_search": "aleph" if arm != "none" else "grep", "cost_usd": 0.1, "model": "m"}

    records = [row("t1", "none", 1000, 10, True), row("t1", "L1", 500, 5, True),
               row("t2", "none", 2000, 8, True), row("t2", "L1", 1000, 4, False),
               row("t3", "none", 3000, 6, False),                      # no partner: not in the paired figures
               {"task": "t4", "arm": "L1", "ok": False, "model": "m"}]  # a failed session is left out
    found = ab.report(records, "none")
    arm = found["arms"]["L1"]
    assert arm["sessions"] == 2 and arm["tokens_ratio"]["paired_tasks"] == 2
    assert arm["tokens_ratio"]["geometric_mean"] == 0.5 and arm["tool_calls_ratio"]["geometric_mean"] == 0.5
    assert arm["file_hits_against_baseline"] == [1, 2]
    assert arm["unpaired_tasks"] == ["t3"] and arm["failed_sessions"] == 1
    assert found["arms"]["none"]["sessions"] == 3 and "tokens_ratio" not in found["arms"]["none"]


def test_the_command_differs_between_arms_only_by_the_plugin_and_its_rung():
    plain = ab.command("none", "haiku", 1.0, "q")
    rung2 = ab.command("L2", "haiku", 1.0, "q")
    assert "--plugin-dir" not in plain and ab.ENGINE_TOOLS not in plain
    assert rung2[rung2.index("--plugin-dir") + 1] == str(ab.PLUGIN)
    assert json.loads(rung2[rung2.index("--settings") + 1]) == {"pluginConfigs": {"aleph-zero@inline": {"options": {"rung": "2"}}}}
    assert ab.ENGINE_TOOLS in rung2
    assert [a for a in plain if a not in rung2] == []          # everything the baseline is given, the arm is given too


# ---- findings of the M4 review --------------------------------------------------------------------------

def test_files_are_named_as_written_whatever_surrounds_them():
    assert ab._named_files('{"files": [".github/workflows/ci.yml", "./src/a.py"]}') == [".github/workflows/ci.yml", "src/a.py"]
    assert ab._named_files('{"files": ["/tmp/az-ab-1/repo/src/x.py"]}', root="/tmp/az-ab-1/repo") == ["src/x.py"]
    assert ab._named_files('Done.\n{"files": ["a.py"], "note": {"why": "nested"}}') == ["a.py"]
    assert ab._named_files('I looked at {x} and {"files": 3} and then\n{"files": ["b.py"]}') == ["b.py"]
    assert ab._named_files("no json here") == []


def _row(task, arm, tokens, calls, hit, model="m", ok=True):
    return {"task": task, "arm": arm, "ok": ok, "tokens": tokens, "tool_calls": calls, "file_hit": hit, "gave_up": False,
            "first_search": None, "cost_usd": 0.1, "model": model}


def test_repeated_runs_of_a_task_are_all_used():
    """Thirty tasks run twice is one of the designs the run may take: the second run must not replace the first."""
    records = [_row("t1", "none", 1000, 10, True), _row("t1", "none", 3000, 10, True),     # mean 2000
               _row("t1", "L1", 900, 5, True), _row("t1", "L1", 1100, 5, False),          # mean 1000
               _row("t2", "none", 4000, 8, True), _row("t2", "L1", 2000, 4, True)]
    found = ab.report(records, "none")
    arm = found["arms"]["L1"]
    assert arm["sessions"] == 3 and arm["tasks"] == 2
    assert arm["tokens_ratio"]["paired_tasks"] == 2 and arm["tokens_ratio"]["geometric_mean"] == 0.5
    assert arm["file_hit_rate"] == 0.75                      # t1: one of two; t2: one of one


def test_the_report_says_which_tasks_have_no_partner_and_refuses_mixed_models():
    import pytest

    records = [_row("t1", "none", 1000, 10, True), _row("t1", "L1", 500, 5, True),
               _row("t2", "none", 1000, 10, True), _row("t2", "L1", 0, 0, False, ok=False),
               _row("t3", "none", 1000, 10, True), _row("t3", "L1", 500, 5, True)]
    found = ab.report(records, "none")
    assert found["arms"]["L1"]["unpaired_tasks"] == ["t2"] and found["arms"]["L1"]["failed_sessions"] == 1
    with pytest.raises(ValueError, match="more than one model"):
        ab.report([*records, _row("t9", "L1", 1, 1, True, model="other")], "none")


def test_an_arm_can_leave_the_grep_pointer_out():
    plain = json.loads(ab.command("L1", "m", 1.0, "q")[ab.command("L1", "m", 1.0, "q").index("--settings") + 1])
    bare = ab.command("L1n", "m", 1.0, "q")
    assert plain == {"pluginConfigs": {"aleph-zero@inline": {"options": {"rung": "1"}}}}
    assert json.loads(bare[bare.index("--settings") + 1]) == {
        "pluginConfigs": {"aleph-zero@inline": {"options": {"rung": "1", "grep_pointer": "off"}}}}


def test_a_run_taken_up_again_does_only_the_sessions_that_are_missing():
    chosen = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    arms = ["none", "L1"]
    done = [{"task": "a", "arm": "none", "ok": True}, {"task": "a", "arm": "L1", "ok": True}, {"task": "b", "arm": "none", "ok": True},
            {"task": "b", "arm": "L1", "ok": False}]                 # a failed session is asked again
    assert ab.sessions_left(chosen, arms, done) == [("b", "L1"), ("c", "none"), ("c", "L1")]
    assert ab.sessions_left(chosen, arms, []) == [(task["id"], arm) for task in chosen for arm in arms]
    assert ab.sessions_left(chosen, arms, done + [{"task": "b", "arm": "L1", "ok": True}, {"task": "c", "arm": "none", "ok": True},
                                                  {"task": "c", "arm": "L1", "ok": True}]) == []


def test_taking_up_a_run_refuses_another_plan_s_file_and_does_not_ask_a_failing_session_for_ever():
    chosen, arms = [{"id": "c"}, {"id": "d"}], ["none", "L1"]
    other = [{"task": "a", "arm": "none", "ok": True}, {"task": "b", "arm": "L1", "ok": True}]
    assert ab.foreign_tasks(chosen, other) == ["a", "b"] and ab.foreign_tasks(chosen, [{"task": "c", "arm": "none", "ok": True}]) == []
    failing = [{"task": "c", "arm": "L1", "ok": False}, {"task": "c", "arm": "L1", "ok": False}]
    assert ("c", "L1") not in ab.sessions_left(chosen, arms, failing)             # two failures: not a third paid session
    assert ("c", "L1") in ab.sessions_left(chosen, arms, failing[:1])             # one failure is asked again


def test_a_run_is_steered_through_its_results_directory_and_no_other_job_is_paused_by_mistake(tmp_path, capsys):
    import longrun

    gpu, results = tmp_path / "benchc", tmp_path / "results"
    longrun.Control(gpu, "prepare", total=3, say=lambda text: None)                # the GPU job: `run` is given this directory
    assert ab.main(["pause", str(gpu)]) == 2 and not (gpu / longrun.PAUSE).exists()
    assert "another job (prepare)" in capsys.readouterr().err
    assert ab.main(["stop", str(tmp_path / "nowhere")]) == 2 and not (tmp_path / "nowhere").exists()
    longrun.Control(results, "A/B run into x.jsonl", total=2, say=lambda text: None)
    (results / "x.jsonl").write_text("")
    assert ab.main(["pause", str(results / "x.jsonl")]) == 0 and (results / longrun.PAUSE).exists()      # the file names its directory
    assert ab.main(["resume", str(results)]) == 0 and not (results / longrun.PAUSE).exists()


def test_taking_up_counts_and_runs_only_what_is_missing(tmp_path, monkeypatch, capsys):
    import pytest

    chosen = [{"id": "c"}, {"id": "d"}]
    monkeypatch.setattr(ab, "tasks", lambda *_: chosen)
    asked = []

    def one(workdir, task, arm, *_):
        asked.append((task["id"], arm))
        return _row(task["id"], arm, 100, 3, True)

    monkeypatch.setattr(ab, "run_one", one)
    out = tmp_path / "ab" / "run.jsonl"
    out.parent.mkdir()
    out.write_text(json.dumps(_row("c", "none", 100, 3, True)) + "\n" + json.dumps(_row("c", "L1", 1, 1, False, ok=False)) + "\n")
    with pytest.raises(SystemExit):                  # three are missing: a limit of two is still a limit
        ab.main(["run", str(tmp_path), "--take-up", str(out), "--max-sessions", "2"])
    assert asked == []
    assert ab.main(["run", str(tmp_path), "--take-up", str(out), "--max-sessions", "3"]) == 0       # the plan is four: three are run
    assert sorted(asked) == [("c", "L1"), ("d", "L1"), ("d", "none")] and len(out.read_text().splitlines()) == 5
    other = tmp_path / "ab" / "other.jsonl"
    other.write_text(json.dumps(_row("z", "none", 100, 3, True)) + "\n")
    with pytest.raises(SystemExit):
        ab.main(["run", str(tmp_path), "--take-up", str(other)])
    assert "another" in capsys.readouterr().err and len(asked) == 3


def test_a_run_can_be_stopped_before_its_first_session_has_written_the_results_file(tmp_path):
    import longrun

    results = tmp_path / "results"
    longrun.Control(results, "A/B run into x.jsonl", total=2, say=lambda text: None)
    assert not (results / "x.jsonl").exists()
    assert ab.main(["stop", str(results / "x.jsonl")]) == 0 and (results / longrun.STOP).exists()


def test_a_run_does_not_start_afresh_on_a_mistyped_take_up_or_wait_on_a_pause_left_over(tmp_path, monkeypatch, capsys):
    import longrun
    import pytest

    monkeypatch.setattr(ab, "tasks", lambda *_: [{"id": "c"}])
    asked = []
    monkeypatch.setattr(ab, "run_one", lambda workdir, task, arm, *_: asked.append(arm) or _row(task["id"], arm, 100, 3, True))
    out = tmp_path / "ab" / "run.jsonl"
    with pytest.raises(SystemExit):
        ab.main(["run", str(tmp_path), "--take-up", str(out)])        # no such file: not "nothing is done yet, run it all"
    assert "no such" in capsys.readouterr().err and asked == []
    longrun.pause(out.parent) if out.parent.exists() else (out.parent.mkdir(), longrun.pause(out.parent))
    with pytest.raises(SystemExit):
        ab.main(["run", str(tmp_path), "--out", str(out)])
    assert "pause" in capsys.readouterr().err and asked == []
    longrun.resume(out.parent)
    assert ab.main(["run", str(tmp_path), "--out", str(out)]) == 0 and sorted(asked) == ["L1", "none"]


def test_what_the_agent_asked_the_engine_is_kept_word_for_word():
    """The gate for vectors was read on issue titles as "the closest to what an agent types". Nobody had looked at
    what an agent types. Every query a session sends to the engine's search is kept, with its length in words."""
    text = _stream(
        _use("mcp__plugin_aleph-zero_aleph__search", query="token rotation"),
        _use("mcp__plugin_aleph-zero_aleph__symbol", k=5),               # another tool of the engine: not a search query
        _use("mcp__plugin_aleph-zero_aleph__search", parent="toolu_1", query="where are refresh tokens stored and how are they rotated"),
        _use("Bash", command="grep -rn rotate src"),
        RESULT,
    )
    record = ab.read_stream(text, ["src/auth/store.py"])
    assert record["aleph_queries"] == ["token rotation", "where are refresh tokens stored and how are they rotated"]
    assert record["aleph_query_words"] == [2, 10]
    assert ab.read_stream(_stream(RESULT), [])["aleph_queries"] == []
    summary = ab.query_lengths([record, {"aleph_query_words": [4]}, {"ok": False}])
    assert summary == {"queries": 3, "median_words": 4, "up_to_8_words": 2, "over_20_words": 0}


def test_what_rung_2_put_before_the_agent_is_worked_out_as_the_mod_does_it():
    import injected

    hit = lambda path, start, sources: {"path": path, "start_line": start, "end_line": start + 9, "sources": sources}  # noqa: E731
    hits = [hit("src/a.py", 1, ["lexical", "symbol"]), hit("src/a.py", 1, ["lexical"]), hit("src/b.py", 40, ["lexical"])]
    assert injected.added(hits, gold=["src/b.py"]) == {"added": True, "locations": ["src/a.py:1-10", "src/b.py:40-49"],
                                                        "files": ["src/a.py", "src/b.py"], "answer_named": True}
    # The first hit was proposed by one generator only: the mod stays silent, whatever is further down.
    assert injected.added([hits[2], hits[0]], gold=["src/b.py"])["added"] is False
    assert injected.added([], gold=["src/b.py"]) == {"added": False, "locations": [], "files": [], "answer_named": False}


def test_the_pairs_of_a_run_are_read_by_what_rung_2_gave_them():
    import injected

    def session(task, arm, tokens, calls, hit):
        return {"task": task, "arm": arm, "ok": True, "tokens": tokens, "tool_calls": calls, "file_hit": hit, "model": "m"}

    records = [session("t1", "none", 1000, 10, True), session("t1", "L2", 500, 5, True),
               session("t2", "none", 1000, 10, True), session("t2", "L2", 2000, 10, False),
               session("t3", "none", 1000, 4, False), session("t3", "L2", 1000, 4, False),
               session("t4", "none", 1000, 4, True)]                                         # no partner: not in any group
    given = {"t1": {"added": True, "answer_named": True}, "t2": {"added": True, "answer_named": False},
             "t3": {"added": False, "answer_named": False}, "t4": {"added": True, "answer_named": True}}
    groups = injected.groups(records, given, baseline="none", arm="L2")
    assert groups["locations added, a file of the answer among them"] == {
        "tasks": 1, "tokens_ratio": 0.5, "tool_calls_ratio": 0.5, "file_hits": [1, 1]}
    assert groups["locations added, no file of the answer among them"] == {
        "tasks": 1, "tokens_ratio": 2.0, "tool_calls_ratio": 1.0, "file_hits": [0, 1]}
    assert groups["nothing added"] == {"tasks": 1, "tokens_ratio": 1.0, "tool_calls_ratio": 1.0, "file_hits": [0, 0]}


def test_locations_can_be_read_one_line_a_file_with_the_further_places_of_a_file_on_its_line():
    import injected

    hit = lambda path, start, sources=("lexical", "symbol"): {"path": path, "start_line": start, "end_line": start + 9, "sources": list(sources)}  # noqa: E731
    hits = [hit("src/a.py", 1), hit("src/a.py", 40), hit("src/b.py", 5), hit("src/a.py", 80), hit("src/a.py", 120), hit("src/c.py", 7)]
    assert injected.added(hits, gold=["src/c.py"], files=2, per_file=True) == {
        "added": True, "locations": ["src/a.py:1-10, 40-49, 80-89", "src/b.py:5-14"], "files": ["src/a.py", "src/b.py"], "answer_named": False}
    assert injected.added(hits, gold=["src/c.py"], files=5, per_file=True)["answer_named"] is True
    assert injected.added(hits, gold=["src/c.py"])["files"] == ["src/a.py", "src/b.py"]          # as the mod has it: five places, three of them in one file
