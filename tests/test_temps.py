"""The temperature guard for long jobs (bench/tools/temps.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "tools"))
import temps  # noqa: E402


@pytest.mark.skipif(sys.platform != "darwin", reason="reads Apple's sensors")
def test_the_machines_sensors_give_a_plausible_chip_temperature():
    found = temps.reading()
    assert found["hottest"] is not None and 10 < temps.hottest_chip(found) < 120
    assert found["pressure"]


def _fake(readings):
    """A sensor that gives the next of `readings` each time it is read."""
    left = list(readings)

    def read():
        degrees = left.pop(0) if len(left) > 1 else left[0]
        return {"die_max": degrees, "cpu_max": None, "gpu_max": None, "hottest": degrees, "pressure": "nominal"}
    return read


def test_a_job_goes_on_untouched_while_the_chip_is_cool(monkeypatch):
    said = []
    monkeypatch.setattr(temps, "reading", _fake([60.0]))
    guard = temps.Guard(limit=95, resume=80, every=0, say=said.append)
    guard()
    assert said == [] and guard.pauses == 0 and guard.highest == 60.0


def test_a_hot_chip_pauses_the_job_until_it_has_cooled_below_the_resume_point(monkeypatch):
    said, slept = [], []
    monkeypatch.setattr(temps, "reading", _fake([97.0, 96.0, 88.0, 79.0]))
    monkeypatch.setattr(temps.time, "sleep", slept.append)
    guard = temps.Guard(limit=95, resume=80, every=0, say=said.append)
    guard()
    assert guard.pauses == 1 and guard.highest == 97.0
    assert len(slept) == 3                       # 96 and 88 are still too warm to go on
    assert "pausing" in said[0] and "going on" in said[-1]


def test_thermal_pressure_from_macos_pauses_the_job_whatever_the_reading(monkeypatch):
    states = [{"die_max": 70.0, "cpu_max": None, "gpu_max": None, "hottest": 70.0, "pressure": "CPU_Speed_Limit = 60"},
              {"die_max": 65.0, "cpu_max": None, "gpu_max": None, "hottest": 65.0, "pressure": "nominal"}]
    monkeypatch.setattr(temps, "reading", lambda: states.pop(0) if len(states) > 1 else states[0])
    monkeypatch.setattr(temps.time, "sleep", lambda seconds: None)
    guard = temps.Guard(limit=95, resume=80, every=0, say=lambda text: None)
    guard()
    assert guard.pauses == 1


def test_a_sensor_that_cannot_be_read_never_stops_the_job(monkeypatch):
    def broken():
        raise OSError("no sensors here")

    monkeypatch.setattr(temps, "reading", broken)
    guard = temps.Guard(every=0)
    guard()
    assert guard.pauses == 0
