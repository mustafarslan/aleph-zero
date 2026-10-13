"""What a person answered when setting Aleph up, and what they chose (ADR-013).

Two files of the same shape. One for the machine, in the user's cache directory beside the embedding
worker: whoever wipes that directory has removed the worker too and is rightly asked again. One for a
repository, beside its index, as the list of linked sources is. Neither is in the index: it is a cache
and gets rebuilt. A file that cannot be read is no answers, never an error: nothing here may stop a search.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .store import state_dir

ANSWERS = ("done", "declined")


def machine_file() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "aleph-zero" / "settings.json"


def repo_file(common_dir: Path) -> Path:
    return state_dir(common_dir) / "settings.json"


def read(path: Path) -> dict:
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write(path: Path, content: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + f".{os.getpid()}.part")
    part.write_text(json.dumps(content, indent=2) + "\n")
    os.replace(part, path)     # a reader sees the old file or the new one, never half of one
    return content


@contextmanager
def _one_writer(path: Path):
    """Read, change and write as one step: a menu being answered and a first search in another terminal (the hint)
    write the same file, and the later of two unguarded writers would drop the other's answer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def change(path: Path, **values) -> dict:
    """Set the given settings; one given as None is removed."""
    with _one_writer(path):
        content = read(path)
        for name, value in values.items():
            if value is None:
                content.pop(name, None)
            else:
                content[name] = value
        return _write(path, content)


def answer(path: Path, step: str, state: str | None) -> dict:
    """Keep what was answered for one step of the setup: 'done', 'declined', or None for open again."""
    if state is not None and state not in ANSWERS:
        raise ValueError(f"an answer is one of {', '.join(ANSWERS)} or None, not {state!r}")
    with _one_writer(path):
        content = read(path)
        answers = content.get("setup") if isinstance(content.get("setup"), dict) else {}
        if state is None:
            answers.pop(step, None)
        else:
            answers[step] = state
        content["setup"] = answers
        return _write(path, content)
