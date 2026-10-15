"""`aleph setup`'s menu as a person meets it: the real command on a pseudo-terminal, every question answered by
typing, as the new-user run of 2026-10-15 drove it by hand. The other setup tests fake the terminal."""
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

QUESTION = re.compile(r"(\[[yY]/[nN]\] |: )$")


def _drive(argv: list[str], answer, seconds: float = 90.0) -> tuple[int, str, list[str]]:
    """Run `argv` on a pty; each time its output ends in a question, type what `answer(question)` says. Returns the
    exit status, everything shown, and the questions asked."""
    master, slave = pty.openpty()
    child = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=os.environ.copy())
    os.close(slave)
    shown, asked, pending = b"", [], b""
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.3)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                shown += data
                pending += data
                tail = pending[-300:].decode("utf-8", "replace")
                if QUESTION.search(tail):
                    more, _, _ = select.select([master], [], [], 0.3)    # a question is where the output stops
                    if more:
                        continue
                    question = tail.splitlines()[-1] if tail.splitlines() else tail
                    asked.append(question)
                    os.write(master, (answer(question) + "\n").encode())
                    pending = b""
            elif child.poll() is not None:
                break
        else:
            pytest.fail(f"still running after {seconds} s; shown:\n{shown.decode('utf-8', 'replace')[-2000:]}")
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)
    return child.returncode, shown.decode("utf-8", "replace"), asked


def _no(question: str) -> str:
    """No to every step (nothing is built, downloaded or linked), and an empty line for a path or URL."""
    return "n" if "[" in question else ""


def test_the_menu_asks_on_a_terminal_keeps_the_answers_and_asks_nothing_the_second_time(repo):
    # The installed command, by the name a person types (TeX Live has an `aleph`): what the menu advises is named so.
    engine = Path(sys.executable).with_name("aleph-zero")
    command = [str(engine), "--cwd", str(repo.root), "setup"]
    code, shown, asked = _drive(command, _no)
    assert code == 0, shown[-2000:]
    assert any("Install the embedding worker" in question for question in asked), asked
    assert any("A path or a URL to link" in question for question in asked), asked
    assert "Traceback" not in shown
    assert "aleph-zero setup" in shown and "`aleph " not in shown
    # Every answer was kept: the second run on a terminal asks nothing and says how to be asked again.
    code, shown, asked = _drive(command, _no)
    assert code == 0, shown[-2000:]
    assert asked == [], asked
    assert "aleph-zero setup --again" in shown
    assert shown.count("Search by meaning in this repository") == 1      # the list once, not again after nothing
