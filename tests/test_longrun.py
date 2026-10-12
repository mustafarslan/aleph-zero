"""A long job that its owner can pause, resume, stop and ask about from another shell (bench/tools/longrun.py)."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import longrun  # noqa: E402
import temps  # noqa: E402


def cool(*_):
    """A heat guard on a machine that is never warm."""


def control(tmp_path, **settings):
    return longrun.Control(tmp_path, "prepare", total=3, guard=settings.pop("guard", cool), say=lambda text: None,
                           poll=0.01, **settings)


def status(tmp_path) -> dict:
    return json.loads((tmp_path / longrun.STATUS).read_text())


def test_a_pause_file_holds_the_job_until_it_is_removed_and_the_status_says_so(tmp_path):
    job = control(tmp_path)
    job.unit(1, "first")
    assert status(tmp_path)["state"] == "running" and status(tmp_path)["unit"] == 1 and status(tmp_path)["pid"] == os.getpid()
    longrun.pause(tmp_path)
    passed = threading.Event()
    waiting = threading.Thread(target=lambda: (job(10, 64), passed.set()))
    waiting.start()
    time.sleep(0.1)
    assert not passed.is_set() and status(tmp_path)["state"] == "paused"
    longrun.resume(tmp_path)
    waiting.join(timeout=5)
    assert passed.is_set() and status(tmp_path)["state"] == "running" and status(tmp_path)["paused_seconds"] > 0


def test_a_stop_file_ends_the_job_at_the_next_point_it_can_and_also_a_paused_one(tmp_path):
    job = control(tmp_path)
    job.unit(1, "first")
    assert job.stop_wanted() is False
    longrun.stop(tmp_path)
    assert job.stop_wanted() is True
    job.finish()
    assert status(tmp_path)["state"] == "stopped" and "again" in longrun.describe(tmp_path)
    paused = control(tmp_path)                       # a new start clears an old stop, and keeps a pause
    assert not (tmp_path / longrun.STOP).exists()
    longrun.pause(tmp_path)
    ended = threading.Event()
    waiting = threading.Thread(target=lambda: (paused(1, 1), ended.set()))
    waiting.start()
    time.sleep(0.05)
    longrun.stop(tmp_path)                           # stop wins over pause: the job must not sit paused for ever
    waiting.join(timeout=5)
    assert ended.is_set() and paused.stop_wanted()


def test_the_status_says_how_far_it_is_and_notices_a_job_that_died(tmp_path):
    job = control(tmp_path)
    job.unit(2, "pandas-dev__pandas-1234")
    job.note(embedded=1500)
    said = longrun.describe(tmp_path)
    assert "running" in said and "2 of 3" in said and "pandas-dev__pandas-1234" in said
    found = status(tmp_path)
    found["pid"] = 2**22 + 12345                     # nobody
    (tmp_path / longrun.STATUS).write_text(json.dumps(found))
    assert "ended without saying so" in longrun.describe(tmp_path)
    job.finish(done=True)
    assert status(tmp_path)["state"] == "done"
    assert "no job has run here" in longrun.describe(tmp_path / "elsewhere")


def test_outside_its_hours_the_job_waits(tmp_path):
    hour = [14]
    job = control(tmp_path, hours=(19, 7), clock=lambda: hour[0])
    passed = threading.Event()
    waiting = threading.Thread(target=lambda: (job(1, 1), passed.set()))
    waiting.start()
    time.sleep(0.1)
    assert not passed.is_set() and status(tmp_path)["state"] == "waiting for its hours"
    hour[0] = 23                                     # 19 to 7 wraps past midnight
    waiting.join(timeout=5)
    assert passed.is_set()
    assert longrun.in_hours((19, 7), 3) and longrun.in_hours((19, 7), 19) and not longrun.in_hours((19, 7), 7)
    assert longrun.in_hours((9, 17), 12) and not longrun.in_hours((9, 17), 17) and longrun.in_hours(None, 3)


def _fake(readings):
    left = list(readings)

    def read():
        degrees = left.pop(0) if len(left) > 1 else left[0]
        return {"die_max": degrees, "cpu_max": None, "gpu_max": None, "hottest": degrees, "pressure": "nominal"}
    return read


def test_a_machine_that_will_not_cool_stops_the_job_instead_of_waiting_for_ever(tmp_path, monkeypatch):
    monkeypatch.setattr(temps.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(temps, "reading", _fake([104.0]))
    guard = temps.Guard(limit=85, resume=70, every=0, say=lambda text: None, stop_at=100)
    with pytest.raises(temps.TooHot, match="104"):
        guard()                                      # at the ceiling: no waiting at all
    monkeypatch.setattr(temps, "reading", _fake([90.0, 60.0, 90.0, 60.0, 90.0, 60.0, 90.0, 60.0]))
    guard = temps.Guard(limit=85, resume=70, every=0, say=lambda text: None, max_pauses=3, within=1800)
    for _ in range(3):
        guard()                                      # three pauses to cool are in order
    with pytest.raises(temps.TooHot, match="4 times"):
        guard()                                      # the fourth within half an hour is a machine that should rest
    job = control(tmp_path, guard=lambda *_: (_ for _ in ()).throw(temps.TooHot("the chip is at 104 C")))
    with pytest.raises(temps.TooHot):
        job(1, 1)
    assert status(tmp_path)["state"] == "stopped for heat" and "104" in status(tmp_path)["why"]


def test_while_it_cools_a_job_still_hears_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(temps.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(temps, "reading", _fake([90.0]))          # never cools
    job = control(tmp_path)
    asked = []

    def gave_up(degrees):
        asked.append(degrees)
        return len(asked) >= 3                       # as if `.stop` appeared at the third look

    guard = temps.Guard(limit=85, resume=70, every=0, say=lambda text: None, while_waiting=gave_up)
    guard()
    assert len(asked) == 3
    assert job.cooling(88.0) is False and status(tmp_path)["state"] == "cooling"
    longrun.stop(tmp_path)
    assert job.cooling(88.0) is True


def test_results_already_worked_out_are_kept_per_question_and_per_setting(tmp_path):
    import benchmark_c

    cache = benchmark_c.ResultCache(tmp_path / "run-cache", settings={"k": 10, "min_pool": None}, fingerprint="abc")
    assert cache.get("repo__1", "c0ffee") is None
    cache.put("repo__1", "c0ffee", {"instance": "repo__1", "scores": {"full": 1}})
    assert cache.get("repo__1", "c0ffee") == {"instance": "repo__1", "scores": {"full": 1}}
    assert cache.get("repo__1", "deadbeef") is None                                              # another commit
    other = benchmark_c.ResultCache(tmp_path / "run-cache", settings={"k": 50, "min_pool": None}, fingerprint="abc")
    assert other.get("repo__1", "c0ffee") is None                                                # another setting
    changed = benchmark_c.ResultCache(tmp_path / "run-cache", settings={"k": 10, "min_pool": None}, fingerprint="xyz")
    assert changed.get("repo__1", "c0ffee") is None                                              # the engine's code changed
    (next((tmp_path / "run-cache").glob("*.json"))).write_text("{ cut short")
    assert cache.get("repo__1", "c0ffee") is None                                                # a damaged entry is worked out again
    assert benchmark_c.code_fingerprint() == benchmark_c.code_fingerprint() and len(benchmark_c.code_fingerprint()) == 16


def test_prepare_can_be_stopped_and_taken_up_again_without_embedding_anything_twice(tmp_path, fake_worker, monkeypatch):
    import benchmark_c

    work = tmp_path / "work"
    rows = []
    for number in range(3):
        clone = work / f"owner__repo{number}"
        clone.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        (clone / "a.py").write_text(f"def function_{number}(value):\n    return value + {number}\n\n\ndef other_{number}():\n    return {number}\n")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(clone), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
        sha = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        rows.append({"repo": f"owner/repo{number}", "base_commit": sha, "instance_id": f"owner__repo{number}-1"})
    monkeypatch.setattr(benchmark_c.worker, "ensure", lambda: fake_worker)
    monkeypatch.setattr(benchmark_c.worker, "slots", lambda: 1)
    monkeypatch.setattr(benchmark_c.worker, "capacity_tokens", lambda: 4096)
    embedded = []
    real = benchmark_c.embedder.embed_pending

    def counting(index, store, pool, **settings):
        report = real(index, store, pool, **settings)
        embedded.append(report.embedded)
        if len(embedded) == 1:
            longrun.stop(work)                       # the owner stops it while the first repository is being embedded
        return report

    monkeypatch.setattr(benchmark_c.embedder, "embed_pending", counting)
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "stopped"
    assert status(work)["state"] == "stopped" and status(work)["unit"] == 1
    first = sum(embedded)
    assert first > 0
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "done"
    assert status(work)["state"] == "done" and status(work)["unit"] == 3
    assert embedded[1] == 0 and sum(embedded) == 3 * first      # the first repository again: nothing left to embed


def test_a_job_checks_power_and_disk_before_it_starts_and_waits_while_on_battery(tmp_path, monkeypatch):
    assert longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: True) == []
    problems = longrun.preflight(tmp_path, need_gb=10**9, mains=lambda: False)
    assert len(problems) == 2 and "GB free" in problems[0] and "battery" in problems[1]
    assert longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: None) == []        # a machine with no battery to ask about
    plugged = [False]
    job = control(tmp_path, mains=lambda: plugged[0])
    passed = threading.Event()
    waiting = threading.Thread(target=lambda: (job(1, 1), passed.set()))
    waiting.start()
    time.sleep(0.1)
    assert not passed.is_set() and status(tmp_path)["state"] == "waiting for mains power"
    plugged[0] = True
    waiting.join(timeout=5)
    assert passed.is_set() and status(tmp_path)["state"] == "running"
    if sys.platform == "darwin":
        assert longrun.on_mains() in (True, False, None)
