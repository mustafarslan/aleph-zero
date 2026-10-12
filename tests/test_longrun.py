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
    assert longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: True, chip=lambda: 40.0) == []
    problems = longrun.preflight(tmp_path, need_gb=10**9, mains=lambda: False, chip=lambda: 40.0)
    assert len(problems) == 2 and "GB free" in problems[0] and "battery" in problems[1]
    assert longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: None, chip=lambda: 40.0) == []        # a machine with no battery to ask about
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


def test_a_chip_that_cannot_be_read_is_not_taken_for_a_cool_one(tmp_path, monkeypatch):
    monkeypatch.setattr(temps.time, "sleep", lambda seconds: None)

    def broken():
        raise OSError("the sensors are gone")

    monkeypatch.setattr(temps, "reading", broken)
    away = temps.Guard(limit=85, resume=70, every=0, say=lambda text: None, stop_at=97, max_pauses=6)
    away()
    away()                                           # twice may be a hiccup
    with pytest.raises(temps.TooHot, match="could not be read"):
        away()                                       # three times in a row: a job that cannot see the heat is given up
    nothing = {"die_max": None, "cpu_max": None, "gpu_max": None, "hottest": None, "pressure": "nominal"}
    monkeypatch.setattr(temps, "reading", lambda: nothing)
    blind = temps.Guard(limit=85, resume=70, every=0, say=lambda text: None, stop_at=97)
    with pytest.raises(temps.TooHot, match="could not be read"):
        for _ in range(3):
            blind()
    said = []
    home = temps.Guard(limit=95, resume=80, every=0, say=said.append)          # somebody is there: it says so and goes on
    for _ in range(4):
        home()
    assert len(said) == 1 and "could not be read" in said[0]
    monkeypatch.setattr(temps, "reading", _then_broken([90.0]))
    waiting = temps.Guard(limit=85, resume=70, every=0, say=lambda text: None, stop_at=97)
    with pytest.raises(temps.TooHot, match="could not be read"):
        waiting()                                    # hot, then blind while waiting to cool: not "going on"
    assert longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: True, chip=lambda: None) == \
        ["the chip's temperature cannot be read on this machine: the heat guard would see nothing"]
    assert longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: True, chip=lambda: 41.0) == []


def _then_broken(readings):
    left = list(readings)

    def read():
        if not left:
            raise OSError("the sensors are gone")
        degrees = left.pop(0)
        return {"die_max": degrees, "cpu_max": None, "gpu_max": None, "hottest": degrees, "pressure": "nominal"}
    return read


def test_hours_are_checked_before_anything_starts_and_a_left_over_pause_is_said(tmp_path):
    assert longrun.parse_hours("19-7") == (19, 7) and longrun.parse_hours("9-17") == (9, 17) and longrun.parse_hours(None) is None
    for wrong in ("7", "a-b", "25-3", "3-24", "-1-5", "7-7-7", ""):
        with pytest.raises(ValueError, match="hours"):
            longrun.parse_hours(wrong)
    longrun.pause(tmp_path)
    problems = longrun.preflight(tmp_path, need_gb=0.001, mains=lambda: True, chip=lambda: 40.0)
    assert len(problems) == 1 and "pause" in problems[0] and "resume" in problems[0]


def test_a_status_that_is_damaged_or_of_another_job_is_said_not_obeyed(tmp_path):
    (tmp_path / longrun.STATUS).write_text('["not", "a", "status"]')
    assert "cannot be read" in longrun.describe(tmp_path)
    (tmp_path / longrun.STATUS).write_text('{"job": "prepare", "state": "waiting for mains power", "pid": 4200000, "highest_c": "hot"}')
    assert "ended without saying so" in longrun.describe(tmp_path)
    assert longrun.job_of(tmp_path) == "prepare" and longrun.job_of(tmp_path / "nowhere") is None


def test_a_stop_while_the_last_repository_is_embedded_is_not_called_done(tmp_path, fake_worker, monkeypatch):
    import benchmark_c

    work, rows = _two_repositories(tmp_path)
    monkeypatch.setattr(benchmark_c.worker, "ensure", lambda: fake_worker)
    monkeypatch.setattr(benchmark_c.worker, "slots", lambda: 1)
    monkeypatch.setattr(benchmark_c.worker, "capacity_tokens", lambda: 4096)
    real, calls = benchmark_c.embedder.embed_pending, []

    def stopped_on_the_last(index, store, pool, **settings):
        calls.append(1)
        if len(calls) == 2:
            longrun.stop(work)                       # arrives while the last repository is being embedded
            settings["budget_seconds"] = 0.0
        return real(index, store, pool, **settings)

    monkeypatch.setattr(benchmark_c.embedder, "embed_pending", stopped_on_the_last)
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "stopped"
    assert "again" in longrun.describe(work)


def test_a_worker_that_went_away_while_the_job_waited_is_started_again_for_the_same_repository(tmp_path, fake_worker, monkeypatch):
    import benchmark_c
    from aleph_zero.embedder import EmbedReport

    work, rows = _two_repositories(tmp_path)
    started = []
    monkeypatch.setattr(benchmark_c.worker, "ensure", lambda: started.append(1) or fake_worker)
    monkeypatch.setattr(benchmark_c.worker, "slots", lambda: 1)
    monkeypatch.setattr(benchmark_c.worker, "capacity_tokens", lambda: 4096)
    real, calls = benchmark_c.embedder.embed_pending, []

    def gone_once(index, store, pool, **settings):
        calls.append(1)
        if len(calls) == 1:                          # as after a pause longer than the worker stays up
            return EmbedReport(0, 2, 0.0, "the worker is unavailable: its socket is gone")
        return real(index, store, pool, **settings)

    monkeypatch.setattr(benchmark_c.embedder, "embed_pending", gone_once)
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "done"
    assert len(calls) == 3 and len(started) == 3     # the first repository twice, with a worker asked for again
    assert status(work)["prepared"] == 2 and status(work)["partly"] == 0


def _two_repositories(tmp_path):
    work, rows = tmp_path / "work", []
    for number in range(2):
        clone = work / f"owner__repo{number}"
        clone.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        (clone / "a.py").write_text(f"def function_{number}(value):\n    return value + {number}\n\n\ndef other_{number}():\n    return {number}\n")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(clone), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
        sha = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        rows.append({"repo": f"owner/repo{number}", "base_commit": sha, "instance_id": f"owner__repo{number}-1"})
    return work, rows


def test_hours_that_cannot_be_read_end_the_command_before_it_writes_or_fetches_anything(tmp_path, monkeypatch, capsys):
    import benchmark_c

    monkeypatch.setattr(benchmark_c, "instances", lambda *_: pytest.fail("the instances were asked for"))
    for wrong in ("7", "a-b", "25-3"):
        with pytest.raises(SystemExit):
            benchmark_c.main(["prepare", str(tmp_path / "work"), "--hours", wrong])
        assert "hours" in capsys.readouterr().err
    assert not (tmp_path / "work").exists()


def test_prepare_does_not_start_over_a_left_over_pause_and_away_never_raises_the_limit_asked_for(tmp_path, monkeypatch, capsys):
    import benchmark_c

    work, seen = tmp_path / "work", {}
    work.mkdir()
    monkeypatch.setattr(benchmark_c, "instances", lambda *_: [])
    monkeypatch.setattr(longrun, "on_mains", lambda: True)
    monkeypatch.setattr(longrun, "_chip", lambda: 40.0)
    monkeypatch.setattr(benchmark_c, "prepare", lambda workdir, rows, max_chunks, limit, **more: seen.update(limit=limit, **more) or "done")
    longrun.pause(work)
    assert benchmark_c.main(["prepare", str(work), "--away"]) == 2 and seen == {}
    assert "pause" in capsys.readouterr().err
    longrun.resume(work)
    assert benchmark_c.main(["prepare", str(work), "--away", "--temp-limit", "80"]) == 0
    assert seen["limit"] == 80.0 and seen["temp_stop"] == 97.0 and seen["max_pauses"] == 6
    assert benchmark_c.main(["prepare", str(work), "--away"]) == 0 and seen["limit"] == 85.0


def test_a_repository_that_could_not_be_fetched_is_not_called_done(tmp_path, fake_worker, monkeypatch, capsys):
    import benchmark_c

    work, rows = _two_repositories(tmp_path)
    monkeypatch.setattr(benchmark_c.worker, "ensure", lambda: fake_worker)
    monkeypatch.setattr(benchmark_c.worker, "slots", lambda: 1)
    monkeypatch.setattr(benchmark_c.worker, "capacity_tokens", lambda: 4096)
    real = benchmark_c.fetch

    def offline_for_the_first(workdir, row):
        if row is rows[0]:
            raise subprocess.CalledProcessError(128, "git fetch")        # as when the network is away for a while
        return real(workdir, row)

    monkeypatch.setattr(benchmark_c, "fetch", offline_for_the_first)
    monkeypatch.setattr(benchmark_c, "reachable", lambda: True)          # the network is there: it is this repository's fetch that fails
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "ended"
    assert "1 not fetched" in status(work)["why"] and "again" in longrun.describe(work)
    monkeypatch.setattr(benchmark_c, "fetch", real)
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "done"       # the same command gets it
    assert benchmark_c.prepare(work, rows, max_chunks=1, guard=cool) == "done"          # left out for its size on purpose: still done


def test_while_the_network_is_away_the_job_waits_and_then_fetches_the_same_repository(tmp_path, fake_worker, monkeypatch):
    import benchmark_c

    work, rows = _two_repositories(tmp_path)
    monkeypatch.setattr(benchmark_c.worker, "ensure", lambda: fake_worker)
    monkeypatch.setattr(benchmark_c.worker, "slots", lambda: 1)
    monkeypatch.setattr(benchmark_c.worker, "capacity_tokens", lambda: 4096)
    monkeypatch.setattr(benchmark_c, "NETWORK_EVERY", 0.0)
    real, tried, looks, states = benchmark_c.fetch, [], [], []

    def away_twice(workdir, row):
        tried.append(row["instance_id"])
        if len(tried) <= 2:
            raise subprocess.CalledProcessError(128, "git fetch")
        return real(workdir, row)

    def back_at_the_third_look():
        looks.append(1)
        states.append(status(work)["state"])
        return len(looks) >= 3

    monkeypatch.setattr(benchmark_c, "fetch", away_twice)
    monkeypatch.setattr(benchmark_c, "reachable", back_at_the_third_look)
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "done"
    assert tried[:3] == [rows[0]["instance_id"]] * 3 and "waiting for the network" in states
    assert status(work)["prepared"] == 2 and "not_fetched" not in status(work)


def test_a_network_that_stays_away_is_waited_for_only_so_long_and_a_stop_is_heard_meanwhile(tmp_path, fake_worker, monkeypatch):
    import benchmark_c

    work, rows = _two_repositories(tmp_path)
    monkeypatch.setattr(benchmark_c.worker, "ensure", lambda: fake_worker)
    monkeypatch.setattr(benchmark_c.worker, "slots", lambda: 1)
    monkeypatch.setattr(benchmark_c.worker, "capacity_tokens", lambda: 4096)
    monkeypatch.setattr(benchmark_c, "NETWORK_EVERY", 0.0)
    monkeypatch.setattr(benchmark_c, "NETWORK_WAIT", 0.05)
    monkeypatch.setattr(benchmark_c, "fetch", lambda *_: (_ for _ in ()).throw(subprocess.CalledProcessError(128, "git fetch")))
    monkeypatch.setattr(benchmark_c, "reachable", lambda: False)
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "ended"       # not for ever: both counted, and said
    assert "2 not fetched" in status(work)["why"]
    monkeypatch.setattr(benchmark_c, "NETWORK_WAIT", 3600.0)
    monkeypatch.setattr(benchmark_c, "reachable", lambda: longrun.stop(work) or False)   # a stop arrives while it waits
    assert benchmark_c.prepare(work, rows, max_chunks=1000, guard=cool) == "stopped"
    job = control(tmp_path / "other")
    assert job.wait_for("waiting for the network", lambda: True, every=0, at_most=1) is True and status(tmp_path / "other")["state"] == "running"
    assert "waiting for the network" in longrun.LIVE
