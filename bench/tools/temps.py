"""CPU and GPU temperatures on an Apple Silicon Mac, without sudo.

Usage: python bench/tools/temps.py [--watch SECONDS] [--limit CELSIUS] [--minutes N] [--json]

Reads the machine's own thermal sensors through IOKit's HID event system (the same
source Activity Monitor's sensors come from), and what macOS says about thermal
pressure (`pmset -g therm`). Sensors are grouped by their names. On Apple's chips the
CPU cores and the GPU sit on one die, and the die sensors (`tdie`) are what heats up
under either load; some machines also name CPU clusters or the GPU separately. With
--watch it prints a line every SECONDS and exits with status 3 as soon as the hottest
of them reaches --limit or macOS reports thermal pressure, so a long job can be
guarded by it.

Apple does not document these sensors; the names differ between chips. A machine where
no die, CPU or GPU sensor is recognised still prints the hottest of all sensors.
"""
import argparse
import ctypes
import ctypes.util
import json
import subprocess
import sys
import time

_TEMPERATURE = 15                       # kIOHIDEventTypeTemperature
_FIELD = _TEMPERATURE << 16             # IOHIDEventFieldBase(kIOHIDEventTypeTemperature)
_UTF8 = 0x08000100                      # kCFStringEncodingUTF8
_SINT32 = 3                             # kCFNumberSInt32Type


def _libraries():
    core = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
    pointer = ctypes.c_void_p
    core.CFStringCreateWithCString.restype = pointer
    core.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    core.CFNumberCreate.restype = pointer
    core.CFNumberCreate.argtypes = [pointer, ctypes.c_long, pointer]
    core.CFDictionaryCreate.restype = pointer
    core.CFDictionaryCreate.argtypes = [pointer, pointer, pointer, ctypes.c_long, pointer, pointer]
    core.CFArrayGetCount.restype = ctypes.c_long
    core.CFArrayGetCount.argtypes = [pointer]
    core.CFArrayGetValueAtIndex.restype = pointer
    core.CFArrayGetValueAtIndex.argtypes = [pointer, ctypes.c_long]
    core.CFStringGetCString.restype = ctypes.c_bool
    core.CFStringGetCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    core.CFRelease.argtypes = [pointer]
    iokit.IOHIDEventSystemClientCreate.restype = pointer
    iokit.IOHIDEventSystemClientCreate.argtypes = [pointer]
    iokit.IOHIDEventSystemClientSetMatching.argtypes = [pointer, pointer]
    iokit.IOHIDEventSystemClientCopyServices.restype = pointer
    iokit.IOHIDEventSystemClientCopyServices.argtypes = [pointer]
    iokit.IOHIDServiceClientCopyProperty.restype = pointer
    iokit.IOHIDServiceClientCopyProperty.argtypes = [pointer, pointer]
    iokit.IOHIDServiceClientCopyEvent.restype = pointer
    iokit.IOHIDServiceClientCopyEvent.argtypes = [pointer, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]
    iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double
    iokit.IOHIDEventGetFloatValue.argtypes = [pointer, ctypes.c_int32]
    return core, iokit


def sensors() -> dict[str, float]:
    """Every temperature sensor the machine reports: name -> degrees Celsius."""
    if sys.platform != "darwin":
        raise RuntimeError("this reads Apple's sensors; on Linux use `sensors` and `nvidia-smi`")
    core, iokit = _libraries()

    def string(text: str):
        return core.CFStringCreateWithCString(None, text.encode(), _UTF8)

    def number(value: int):
        return core.CFNumberCreate(None, _SINT32, ctypes.byref(ctypes.c_int32(value)))

    # Apple's vendor page 0xff00, usage 5: the temperature sensors.
    keys = (ctypes.c_void_p * 2)(string("PrimaryUsagePage"), string("PrimaryUsage"))
    values = (ctypes.c_void_p * 2)(number(0xFF00), number(5))
    callbacks = ctypes.c_void_p.in_dll(core, "kCFTypeDictionaryKeyCallBacks")
    value_callbacks = ctypes.c_void_p.in_dll(core, "kCFTypeDictionaryValueCallBacks")
    matching = core.CFDictionaryCreate(None, keys, values, 2, ctypes.addressof(callbacks), ctypes.addressof(value_callbacks))
    client = iokit.IOHIDEventSystemClientCreate(None)
    iokit.IOHIDEventSystemClientSetMatching(client, matching)
    services = iokit.IOHIDEventSystemClientCopyServices(client)
    found: dict[str, float] = {}
    product = string("Product")
    if services:
        for position in range(core.CFArrayGetCount(services)):
            service = core.CFArrayGetValueAtIndex(services, position)
            name_ref = iokit.IOHIDServiceClientCopyProperty(service, product)
            event = iokit.IOHIDServiceClientCopyEvent(service, _TEMPERATURE, 0, 0)
            if not name_ref or not event:
                continue
            buffer = ctypes.create_string_buffer(256)
            core.CFStringGetCString(name_ref, buffer, 256, _UTF8)
            degrees = iokit.IOHIDEventGetFloatValue(event, _FIELD)
            core.CFRelease(event)
            core.CFRelease(name_ref)
            if 0 < degrees < 150:  # a sensor that is absent reports nonsense
                name = buffer.value.decode(errors="replace")
                found[name] = max(degrees, found.get(name, 0.0))
        core.CFRelease(services)
    return found


def _group(name: str) -> str:
    """Which part of the machine a sensor is on, from its name. On Apple's chips the CPU
    cores and the GPU are on one die: `tdie` sensors are that die, and many machines
    (an M4 Max among them) report no sensor named for the GPU alone."""
    lowered = name.lower()
    if "gpu" in lowered:
        return "gpu"
    if "pacc" in lowered or "eacc" in lowered:
        return "cpu"
    if "tdie" in lowered:
        return "die"
    if "battery" in lowered or "gas gauge" in lowered:
        return "battery"
    if "nand" in lowered or "ssd" in lowered:
        return "storage"
    return "other"


def pressure() -> str:
    """What macOS says about thermal pressure: 'nominal', or the limits it has imposed."""
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    limits = [line.strip() for line in out.splitlines() if "=" in line and "Note" not in line]
    return "; ".join(limits) if limits else "nominal"


def reading() -> dict:
    found = sensors()
    groups: dict[str, list[float]] = {}
    for name, degrees in found.items():
        groups.setdefault(_group(name), []).append(degrees)
    summary = {group: {"max": round(max(values), 1), "mean": round(sum(values) / len(values), 1), "sensors": len(values)}
               for group, values in sorted(groups.items())}
    top = lambda group: summary.get(group, {}).get("max")  # noqa: E731
    return {"die_max": top("die"), "cpu_max": top("cpu"), "gpu_max": top("gpu"),
            "hottest": round(max(found.values()), 1) if found else None, "groups": summary, "pressure": pressure()}


def hottest_chip(found: dict) -> float:
    """The hottest reading on the processor: die, CPU cores or GPU, whichever this machine names."""
    return max((found[key] or 0.0) for key in ("die_max", "cpu_max", "gpu_max", "hottest"))


def line(found: dict) -> str:
    parts = [time.strftime("%H:%M:%S")]
    if found["die_max"] is not None:
        parts.append(f"chip die (CPU and GPU share it) {found['die_max']:.1f} C")
    for label, key in (("CPU cores", "cpu_max"), ("GPU", "gpu_max")):
        if found[key] is not None:
            parts.append(f"{label} {found[key]:.1f} C")
    if found["hottest"] is not None:
        parts.append(f"hottest sensor {found['hottest']:.1f} C")
    parts.append(f"macOS thermal pressure: {found['pressure']}")
    return "  ".join(parts)


class Guard:
    """Keeps a long job from cooking the machine. Call it often (it reads the sensors at
    most every `every` seconds): when the chip reaches `limit` or macOS reports thermal
    pressure, it waits until the chip is back under `resume`, then lets the job go on.
    It remembers the highest reading it saw."""

    def __init__(self, limit: float = 95.0, resume: float = 80.0, every: float = 15.0, say=print) -> None:
        self.limit, self.resume, self.every, self.say = limit, resume, every, say
        self.highest = 0.0
        self.pauses = 0
        self._next = 0.0

    def __call__(self, *_ignored) -> None:
        if time.monotonic() < self._next:
            return
        self._next = time.monotonic() + self.every
        try:
            found = reading()
        except Exception:  # noqa: BLE001 - a job must not die because a sensor could not be read
            return
        hot = hottest_chip(found)
        self.highest = max(self.highest, hot)
        if hot < self.limit and found["pressure"] in ("nominal", "unknown"):
            return
        self.pauses += 1
        self.say(f"chip at {hot:.0f} C (limit {self.limit:.0f} C; thermal pressure: {found['pressure']}): "
                 f"pausing until it is under {self.resume:.0f} C")
        while True:
            time.sleep(self.every)
            try:
                found = reading()
            except Exception:  # noqa: BLE001
                return
            hot = hottest_chip(found)
            if hot < self.resume and found["pressure"] in ("nominal", "unknown"):
                self.say(f"chip at {hot:.0f} C: going on")
                self._next = time.monotonic() + self.every
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="print a line every SECONDS until stopped")
    parser.add_argument("--limit", type=float, default=100.0, metavar="CELSIUS",
                        help="with --watch: exit with status 3 when a CPU or GPU sensor reaches this (default 100)")
    parser.add_argument("--minutes", type=float, help="with --watch: stop after this long (status 0)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    started = time.monotonic()
    highest = 0.0
    while True:
        found = reading()
        print(json.dumps(found) if args.json else line(found), flush=True)
        if args.watch is None:
            return 0
        hot = hottest_chip(found)
        highest = max(highest, hot)
        if hot >= args.limit or found["pressure"] not in ("nominal", "unknown"):
            print(f"TOO HOT: {hot:.1f} C (limit {args.limit:.0f} C), macOS thermal pressure: {found['pressure']}", flush=True)
            return 3
        if args.minutes is not None and time.monotonic() - started >= args.minutes * 60:
            print(f"highest in {args.minutes:g} minutes: {highest:.1f} C", flush=True)
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
