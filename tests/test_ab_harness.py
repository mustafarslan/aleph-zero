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
                "gave_up": False, "first_search": "aleph" if arm != "none" else "grep", "cost_usd": 0.1}

    records = [row("t1", "none", 1000, 10, True), row("t1", "L1", 500, 5, True),
               row("t2", "none", 2000, 8, True), row("t2", "L1", 1000, 4, False),
               row("t3", "none", 3000, 6, False),                      # no partner: not in the paired figures
               {"task": "t4", "arm": "L1", "ok": False}]              # a failed session is left out
    found = ab.report(records, "none")
    arm = found["arms"]["L1"]
    assert arm["sessions"] == 2 and arm["tokens_ratio"]["paired_tasks"] == 2
    assert arm["tokens_ratio"]["geometric_mean"] == 0.5 and arm["tool_calls_ratio"]["geometric_mean"] == 0.5
    assert arm["file_hits_against_baseline"] == [1, 2]
    assert found["arms"]["none"]["sessions"] == 3 and "tokens_ratio" not in found["arms"]["none"]


def test_the_command_differs_between_arms_only_by_the_plugin_and_its_rung():
    plain = ab.command("none", "haiku", 1.0, "q")
    rung2 = ab.command("L2", "haiku", 1.0, "q")
    assert "--plugin-dir" not in plain and ab.ENGINE_TOOLS not in plain
    assert rung2[rung2.index("--plugin-dir") + 1] == str(ab.PLUGIN)
    assert json.loads(rung2[rung2.index("--settings") + 1]) == {"pluginConfigs": {"aleph-zero@inline": {"options": {"rung": "2"}}}}
    assert ab.ENGINE_TOOLS in rung2
    assert [a for a in plain if a not in rung2] == []          # everything the baseline is given, the arm is given too
