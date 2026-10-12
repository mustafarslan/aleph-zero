"""Pause, resume, stop and ask about a long job from another shell.

A job that runs for hours on a laptop is started from one connection and looked after from
others, some of them hours later and from somewhere else. So it is steered through files in
its work directory, which outlast a connection, and it writes down where it is:

    python bench/tools/longrun.py status WORKDIR     where it is, how warm the chip got, whether it still runs
    python bench/tools/longrun.py pause  WORKDIR     it waits after the batch it is on (the GPU goes idle)
    python bench/tools/longrun.py resume WORKDIR     it goes on
    python bench/tools/longrun.py stop   WORKDIR     it ends after the batch it is on; starting it again takes it up there

The job calls its `Control` after every batch of work. There the heat guard is asked (which
may wait for the chip to cool, or give the job up), then the pause file, the stop file and the
hours the job may run in. Nothing here knows what the job does: what makes a job resumable is
that its own work is kept as it goes (an index, a file of results), and that is the job's.

To start a job so that it outlives the connection, and the machine does not sleep under it:

    nohup caffeinate -i uv run --group bench python bench/eval/benchmark_c.py prepare WORKDIR ... > WORKDIR/prepare.log 2>&1 &

(`nohup` first: it is what must outlive the connection, and `caffeinate` then stays for as long as the job does.)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PAUSE, STOP, STATUS = ".pause", ".stop", "status.json"


def in_hours(hours: tuple[int, int] | None, hour: int) -> bool:
    """Whether `hour` (0 to 23) lies in the hours a job may run: from the first up to, not including, the
    second, around midnight when the first is the later (19 to 7)."""
    if hours is None or hours[0] == hours[1]:
        return True
    first, last = hours
    return first <= hour < last if first < last else hour >= first or hour < last


def on_mains() -> bool | None:
    """Whether the machine runs on mains power; None when that cannot be told (no battery, not a Mac)."""
    try:
        said = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return True if "AC Power" in said else False if "Battery Power" in said else None


def parse_hours(text: str | None) -> tuple[int, int] | None:
    """`FIRST-LAST` as two hours of the day (19-7), or ValueError that says what is expected."""
    if text is None:
        return None
    parts = text.split("-")
    if len(parts) != 2 or not all(part.isdigit() and int(part) < 24 for part in parts):
        raise ValueError(f"hours are given as FIRST-LAST, each from 0 to 23 (19-7 is from 19:00 to 7:00; 0 is midnight), not {text!r}")
    return int(parts[0]), int(parts[1])


def _chip() -> float | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import temps

    return temps.chip_now()


def preflight(workdir: Path, need_gb: float = 20.0, mains=None, chip=None) -> list[str]:
    """What stands in the way of hours of unattended work: each as one sentence. Empty when nothing does."""
    problems = []
    mains, chip = mains or on_mains, chip or _chip
    if Path(workdir, PAUSE).exists():
        problems.append(f"a pause is asked for in {workdir} (left from before?): the job would start and wait at once; "
                        f"`longrun.py resume {workdir}` takes it away")
    if chip() is None:
        problems.append("the chip's temperature cannot be read on this machine: the heat guard would see nothing")
    free = shutil.disk_usage(workdir if Path(workdir).exists() else Path(workdir).parent).free / 1e9
    if free < need_gb:
        problems.append(f"{free:.0f} GB free where {workdir} is, and the run may need {need_gb:.0f}: it would end in the middle of a fetch")
    if mains() is False:
        problems.append("the machine is on battery: plug it in (the run waits while it is on battery, and a laptop that sleeps ends it)")
    return problems


class Control:
    """What a long job asks after every batch: may I go on? And what it tells anyone who asks."""

    def __init__(self, workdir: Path, job: str, total: int, guard=None, say=print, hours: tuple[int, int] | None = None,
                 poll: float = 5.0, clock=lambda: time.localtime().tm_hour, mains=None) -> None:
        self.workdir, self.guard, self.say, self.hours, self.poll, self.clock = Path(workdir), guard, say, hours, poll, clock
        self.mains, self._mains_at, self._plugged = mains, 0.0, True     # asked at most once a minute: it starts a process
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / STOP).unlink(missing_ok=True)       # a stop was for the run before; a pause is kept
        self._started = time.time()
        self._written = 0.0
        self.found = {"job": job, "state": "running", "pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "unit": 0, "of": total, "name": "", "paused_seconds": 0.0, "why": ""}
        self._write()

    # ---- what the job calls ------------------------------------------------------------------------

    def __call__(self, *_progress) -> None:
        """After every batch. Returns when the job may go on; `stop_wanted` says whether it should."""
        if self.guard is not None:
            try:
                self.guard()
            except Exception as hot:        # the guard gave the job up: said, then passed on
                self.found.update(state="stopped for heat", why=str(hot))
                self._write()
                raise
            self._heat()
        waited = time.time()
        while not self.stop_wanted():
            state = "paused" if (self.workdir / PAUSE).exists() else \
                "waiting for its hours" if not in_hours(self.hours, self.clock()) else \
                "waiting for mains power" if self._on_battery() else ""
            if not state:
                break
            if self.found["state"] != state:
                self.say(state + (f" ({self.hours[0]}:00 to {self.hours[1]}:00)" if state == "waiting for its hours" else
                                  ": `longrun.py resume` lets it go on" if state == "paused" else ""))
                self.found["state"] = state
                self._write()
            time.sleep(self.poll)
        if self.found["state"] != "running":
            self.found["paused_seconds"] = round(self.found["paused_seconds"] + time.time() - waited, 1)
            if not self.stop_wanted():
                self.say("going on")
            self.found["state"] = "running"
            self._write()
        elif time.time() - self._written > 15:
            self._write()

    def wait_for(self, state: str, ready, every: float = 60.0, at_most: float = float("inf")) -> bool:
        """Wait, saying `state`, until `ready()`. False when a stop is asked for, or `at_most` seconds have passed."""
        began = time.time()
        try:
            while not self.stop_wanted():
                if ready():
                    return True
                if time.time() - began >= at_most:
                    return False
                if self.found["state"] != state:
                    self.say(state)
                    self.found["state"] = state
                    self._write()
                time.sleep(every)
            return False
        finally:
            if self.found["state"] == state:
                self.found["paused_seconds"] = round(self.found["paused_seconds"] + time.time() - began, 1)
                self.found["state"] = "running"
                self._write()

    def cooling(self, degrees: float) -> bool:
        """For the heat guard to call while it waits: says so in the status; True when the wait should be given up."""
        if self.found["state"] != "cooling":
            self.found["state"] = "cooling"
        self.found["last_c"] = round(degrees, 1)
        self._write()
        return self.stop_wanted()

    def stop_wanted(self) -> bool:
        return (self.workdir / STOP).exists()

    def unit(self, number: int, name: str) -> None:
        """The job has come to its `number`-th unit of work."""
        self.found.update(unit=number, name=name)
        self._write()

    def note(self, **fields) -> None:
        self.found.update(fields)
        self._write()

    def finish(self, done: bool = False, why: str = "") -> str:
        state = "done" if done else "stopped" if self.stop_wanted() else self.found["state"] if self.found["state"].startswith("stopped") else "ended"
        self.found.update(state=state, why=why or self.found["why"])
        self._heat()
        self._write()
        return state

    # ---- inside ------------------------------------------------------------------------------------

    def _on_battery(self) -> bool:
        if self.mains is None:
            return False
        if not self._plugged or time.time() - self._mains_at > 60:
            self._plugged, self._mains_at = self.mains() is not False, time.time()
        return not self._plugged

    def _heat(self) -> None:
        for field, attribute in (("highest_c", "highest"), ("heat_pauses", "pauses")):
            if hasattr(self.guard, attribute):
                self.found[field] = round(getattr(self.guard, attribute), 1)

    def _write(self) -> None:
        self.found["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.found["elapsed_seconds"] = round(time.time() - self._started)
        aside = self.workdir / f"{STATUS}.{os.getpid()}"
        try:
            aside.write_text(json.dumps(self.found, indent=1) + "\n")
            os.replace(aside, self.workdir / STATUS)
        except OSError:
            pass                            # a status that cannot be written must not end the job
        self._written = time.time()


def pause(workdir: Path) -> None:
    Path(workdir, PAUSE).touch()


def resume(workdir: Path) -> None:
    Path(workdir, PAUSE).unlink(missing_ok=True)


def stop(workdir: Path) -> None:
    Path(workdir, STOP).touch()


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


LIVE = ("running", "paused", "cooling", "waiting for its hours", "waiting for mains power", "waiting for the network")


def _read(workdir: Path) -> dict | None | str:
    """The status as written; None when there is none; a sentence when what is there is not a status."""
    try:
        text = Path(workdir, STATUS).read_text()
    except OSError:
        return None
    try:
        found = json.loads(text)
    except ValueError:
        found = None
    return found if isinstance(found, dict) else f"the status in {workdir} cannot be read ({STATUS} is damaged, or not a job's)"


def job_of(workdir: Path) -> str | None:
    """The name of the job whose status is in `workdir`; None when there is none to read."""
    found = _read(workdir)
    return str(found.get("job", "")) if isinstance(found, dict) else None


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def describe(workdir: Path) -> str:
    """What a person asking from another shell is told."""
    found = _read(workdir)
    if found is None:
        return f"no job has run here ({workdir} has no {STATUS})"
    if isinstance(found, str):
        return found
    state = str(found.get("state", "?"))
    if state in LIVE and not _alive(int(_number(found.get("pid")))):
        state = f"not running: it ended without saying so (killed, or the machine restarted) while {state}. Start it again to take it up"
    lines = [f"{found.get('job', 'job')}: {state}" + (f" ({found['why']})" if found.get("why") else ""),
             f"  at {found.get('unit', 0)} of {found.get('of', '?')}" + (f": {found['name']}" if found.get("name") else ""),
             f"  started {found.get('started', '?')}, last word {found.get('updated', '?')}, "
             f"{int(_number(found.get('elapsed_seconds'))) // 60} min so far, "
             f"{round(_number(found.get('paused_seconds'))) // 60} min of it paused by hand or by the clock"]
    if "highest_c" in found:
        lines.append(f"  chip: highest {_number(found['highest_c']):.0f} C, waited to cool {int(_number(found.get('heat_pauses')))} times")
    extra = {key: value for key, value in found.items() if key not in (
        "job", "state", "why", "unit", "of", "name", "started", "updated", "elapsed_seconds", "paused_seconds", "highest_c",
        "heat_pauses", "pid", "last_c")}
    if extra:
        lines.append("  " + ", ".join(f"{key} {value}" for key, value in extra.items()))
    if Path(workdir, PAUSE).exists():
        lines.append("  a pause is asked for (`longrun.py resume` takes it away)")
    if found.get("state") in ("stopped", "stopped for heat", "ended"):
        lines.append("  start it again with the same command to take it up where it stopped")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("status", "pause", "resume", "stop"))
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--json", action="store_true", help="status: the status file as it is")
    args = parser.parse_args(argv)
    if args.action == "status":
        print(Path(args.workdir, STATUS).read_text() if args.json and Path(args.workdir, STATUS).exists() else describe(args.workdir))
        if not args.json:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                import temps

                print("  now: " + temps.line(temps.reading()))
            except Exception:  # noqa: BLE001 - not a Mac, or the sensors cannot be read
                pass
        return 0
    {"pause": pause, "resume": resume, "stop": stop}[args.action](args.workdir)
    print({"pause": "it will wait after the batch it is on", "resume": "it goes on",
           "stop": "it will end after the batch it is on"}[args.action])
    return 0


if __name__ == "__main__":
    sys.exit(main())
