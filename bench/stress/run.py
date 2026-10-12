"""Several sessions on one repository for a long time: does the index stay right?

Usage: uv run python bench/stress/run.py [--minutes 2] [--real-worker] [--harsh] [--out FILE]

One repository with three worktrees. A real `aleph serve` process per worktree, each
driven by a thread that edits, adds, deletes and renames files, commits, stashes and
switches branch, and after every step asks the server and checks the answer. A fourth
server only asks, about all three worktrees. Vectors are made in the background by a
stand-in worker (or the real one with --real-worker). A separate process collects
garbage every few seconds, with ages shortened so that there is something to collect.

Checked all the time: no call fails; every result is a file of that worktree as it is
now; a word just written is found, a word just removed is not, and a word written in
another worktree never is; no session's content is collected under it; private memory
of each session stays under its limit. Checked at the end: the database's integrity,
foreign keys and text index; the write-ahead log can be emptied; every vector row is
inside the file and belongs to one chunk; stored vectors are the right ones.

With --harsh the collector uses age zero: it takes content the other sessions are
using. Answers must still be right; only the "nothing collected under a session"
check is dropped.
"""
import argparse
import json
import os
import random
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import apsw

from aleph_zero import memstat

ROOT = Path(__file__).resolve().parents[2]
SESSION_LIMIT_MB = 130
BUDGET_MB = 1024   # four sessions and the embedding worker (ADR-005)
SEEN_REFRESH_SECONDS = 5
COLLECT_AGE_SECONDS = 40
WORDS = ("ledger", "beacon", "harbor", "quartz", "lantern", "meadow", "cinder", "pylon", "saddle", "thicket")


class Failure(Exception):
    """An invariant did not hold."""


class Session:
    """One `aleph serve` process, spoken to over its standard input and output."""

    def __init__(self, name: str, cwd: Path, env: dict, log_dir: Path) -> None:
        self.name = name
        self.errors = open(log_dir / f"{name}.stderr", "w+")
        self.proc = subprocess.Popen([sys.executable, "-m", "aleph_zero.cli", "--cwd", str(cwd), "serve"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, text=True, env=env)
        self.next_id = 0
        self.calls = 0
        self.slowest_ms = 0.0
        self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "stress", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict, seconds: float = 60) -> dict:
        self.next_id += 1
        started = time.monotonic()
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        ready, _, _ = select.select([self.proc.stdout], [], [], seconds)
        if not ready:
            raise Failure(f"{self.name}: no answer to {method} within {seconds} s")
        line = self.proc.stdout.readline()
        if not line:
            raise Failure(f"{self.name}: the server exited ({self.proc.poll()})")
        reply = json.loads(line)
        if reply.get("id") != self.next_id or "error" in reply:
            raise Failure(f"{self.name}: bad reply to {method}: {str(reply)[:300]}")
        self.slowest_ms = max(self.slowest_ms, (time.monotonic() - started) * 1000)
        return reply["result"]

    def call(self, tool: str, **arguments) -> dict:
        self.calls += 1
        result = self._request("tools/call", {"name": tool, "arguments": {**arguments, "format": "json"}})
        if result.get("isError"):
            raise Failure(f"{self.name}: {tool}({arguments}) failed: {result['content'][0]['text'][:300]}")
        return json.loads(result["content"][0]["text"])

    def close(self) -> int:
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = -9
        self.errors.seek(0)
        said = self.errors.read()
        self.errors.close()
        if "Traceback" in said:
            raise Failure(f"{self.name}: the server printed a traceback: {said[-600:]}")
        return code


class Worktree:
    """One worktree and what should be findable in it right now."""

    def __init__(self, name: str, root: Path, seed: int) -> None:
        self.name, self.root = name, root
        self.random = random.Random(seed)
        self.branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.other = f"{self.branch}-alt"
        self.git("branch", self.other)
        self.live: dict[str, str] = {}        # word -> the path that holds it, in the working tree
        self.dead: list[str] = []             # words that were here and must not be found any more
        self.committed: dict[str, dict[str, str]] = {self.branch: {}, self.other: {}}
        self.counter = 0
        self.steps = 0
        self.mutex = threading.Lock()         # held while the word lists change

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise Failure(f"{self.name}: git {' '.join(args)} failed: {proc.stderr[:300]}")
        return proc.stdout

    def word(self) -> str:
        self.counter += 1
        return f"{self.random.choice(WORDS)}_{self.name}_{self.counter}_{self.random.randrange(10**6):06d}"

    def body(self, word: str) -> str:
        return (f'"""Module for {word}."""\n\n\n'
                f"def {word}(value):\n"
                f'    """Compute the {word} of a value."""\n'
                f"    return value * {self.counter} + {len(word)}\n\n\n"
                f"class Holder{self.counter}:\n"
                f"    def describe(self):\n"
                f"        return '{word} holder'\n")

    # ---- the steps -----------------------------------------------------------------------------

    def add(self) -> None:
        word = self.word()
        path = f"gen/{self.name}/m{self.counter}.py"
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(self.body(word))
        self.live[word] = path

    def edit(self) -> None:
        if not self.live:
            return self.add()
        old = self.random.choice(sorted(self.live))
        path = self.live.pop(old)
        word = self.word()
        (self.root / path).write_text(self.body(word))
        self.live[word] = path
        self.dead.append(old)

    def delete(self) -> None:
        if len(self.live) < 3:
            return self.add()
        old = self.random.choice(sorted(self.live))
        (self.root / self.live.pop(old)).unlink()
        self.dead.append(old)

    def rename(self) -> None:
        if not self.live:
            return self.add()
        word = self.random.choice(sorted(self.live))
        self.counter += 1
        target = f"gen/{self.name}/moved{self.counter}.py"
        (self.root / target).parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.root / self.live[word], self.root / target)
        self.live[word] = target

    def commit(self) -> None:
        self.git("add", "-A")
        if self.git("status", "--porcelain").strip():
            self.git("commit", "-q", "-m", f"step {self.steps}")
        self.committed[self.branch] = dict(self.live)

    def switch(self) -> None:
        self.commit()
        here = set(self.live)
        self.branch, self.other = self.other, self.branch
        self.git("checkout", "-q", self.branch)
        self.live = dict(self.committed[self.branch])
        self.dead.extend(sorted(here - set(self.live))[:5])

    def stash(self, check) -> None:
        """Put the uncommitted work away, check the worktree as committed, and bring it back."""
        self.git("add", "-A")
        if not self.git("status", "--porcelain").strip():
            return
        working = dict(self.live)
        # Not `git stash`: the stash list is one per repository, and another worktree's
        # driver would pop this one. A stash commit that no ref points at is private.
        saved = self.git("stash", "create").strip()
        self.git("reset", "-q", "--hard")
        self.live = dict(self.committed[self.branch])
        check(sorted(set(working) - set(self.live))[:3])
        self.git("stash", "apply", "-q", saved)
        self.git("reset", "-q")
        self.live = working

    def step(self, check) -> str:
        self.steps += 1
        action = self.random.choices(["add", "edit", "delete", "rename", "commit", "switch", "stash"],
                                     weights=[30, 30, 8, 8, 12, 6, 6])[0]
        with self.mutex:
            if action == "stash":
                self.stash(check)
            else:
                getattr(self, action)()
            del self.dead[:-40]
        return action


class Run:
    def __init__(self, args) -> None:
        self.args = args
        self.work = Path(tempfile.mkdtemp(prefix="az-stress-", dir="/tmp"))
        self.logs = self.work / "logs"
        self.logs.mkdir()
        self.failures: list[str] = []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.memory_mb: dict[str, float] = {}
        self.collected = {"runs": 0, "blobs_removed": 0, "chunks_removed": 0}
        self.env = {**os.environ, "ALEPH_DENSE": "1", "ALEPH_SEEN_REFRESH_SECONDS": str(SEEN_REFRESH_SECONDS),
                    # The sessions' own collection, with a day shortened to ten seconds.
                    "ALEPH_GC_EVERY_SECONDS": "10", "ALEPH_GC_AGE_DAYS": repr(COLLECT_AGE_SECONDS / 86400),
                    "ALEPH_WORKER_IDLE_EXIT_SECONDS": "3600"}
        if not args.real_worker:
            self.env["XDG_CACHE_HOME"] = str(self.work / "cache")
            self._install_stand_in()

    def _install_stand_in(self) -> None:
        """The worker's directory, with a stand-in for the server binary and an empty model."""
        from aleph_zero.embed import worker

        home = self.work / "cache" / "aleph-zero" / "worker"
        (home / "bin").mkdir(parents=True)
        (home / "models").mkdir()
        (home / "models" / worker.MODEL_FILE).write_bytes(b"stand-in")
        binary = home / "bin" / "llama-server"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_worker.py"}" "$@"\n')
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    def fail(self, message: str) -> None:
        with self.lock:
            self.failures.append(message)
        self.stop.set()

    # ---- the repository --------------------------------------------------------------------------

    def build(self) -> list[Worktree]:
        main = self.work / "repo"
        main.mkdir()

        def git(*args, cwd=main):
            subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "stress@example.com")
        git("config", "user.name", "Stress")
        git("config", "commit.gpgsign", "false")
        for package in range(10):
            for module in range(15):
                path = main / f"pkg{package}/mod{module}.py"
                path.parent.mkdir(exist_ok=True)
                path.write_text("".join(
                    f"def seed_{package}_{module}_{n}(items):\n"
                    f'    """Seed function {n} of module {module} in package {package}."""\n'
                    f"    return [item for item in items if item % {n + 2} == 0]\n\n\n" for n in range(6)))
        (main / "README.md").write_text("# Stress repository\n\nSeed content shared by every worktree.\n")
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        trees = [Worktree("a", main, 1)]
        for number, name in enumerate(("b", "c"), 2):
            root = self.work / f"wt-{name}"
            git("worktree", "add", "-q", "-b", f"work-{name}", str(root))
            trees.append(Worktree(name, root, number))
        return trees

    # ---- checks ------------------------------------------------------------------------------------

    def ask(self, session: Session, tree: Worktree, word: str) -> list[dict]:
        """Hits for `word` in `tree`, once the index has caught up with the worktree."""
        for _ in range(40):
            answer = session.call("search", query=word, k=5, cwd=str(tree.root))
            if not answer["pending"] and not answer["timed_out"]:
                return answer["hits"]
            time.sleep(0.05)
        raise Failure(f"{session.name}: the index of worktree {tree.name} never caught up")

    def verify(self, session: Session, tree: Worktree, gone: list[str] = ()) -> None:
        """What `session` answers about `tree` matches what is in the worktree now."""
        words = tree.random.sample(sorted(tree.live), min(2, len(tree.live)))
        for word in words:
            hits = self.ask(session, tree, word)
            holding = [hit for hit in hits if word in hit["text"]]
            if not any(hit["path"] == tree.live[word] for hit in holding):
                raise Failure(f"{session.name}/{tree.name}: {word} is in {tree.live[word]} but the answer was "
                              f"{[(hit['path'], hit['start_line']) for hit in hits]}")
            self.grounded(session, tree, hits)
        for word in [*gone, *tree.random.sample(tree.dead, min(2, len(tree.dead)))]:
            if word in tree.live:
                continue
            hits = self.ask(session, tree, word)
            stale = [hit["path"] for hit in hits if word in hit["text"]]
            if stale:
                raise Failure(f"{session.name}/{tree.name}: {word} was removed but is still answered from {stale}")
            self.grounded(session, tree, hits)

    def grounded(self, session: Session, tree: Worktree, hits: list[dict]) -> None:
        """Every hit is a file of this worktree, and its first line is in that file now."""
        for hit in hits:
            path = tree.root / hit["path"]
            if not path.is_file():
                raise Failure(f"{session.name}/{tree.name}: answered {hit['path']}, which is not in the worktree")
            first = next((line.strip() for line in hit["text"].splitlines() if line.strip()), "")
            if first and first not in path.read_text(errors="replace"):
                raise Failure(f"{session.name}/{tree.name}: {hit['path']} does not contain {first!r} any more")

    def foreign(self, session: Session, tree: Worktree, others: list[Worktree]) -> None:
        """A word that exists only in another worktree is never answered here."""
        for other in others:
            with other.mutex:
                words = sorted(other.live)
            if not words:
                continue
            word = tree.random.choice(words)
            leaked = [hit["path"] for hit in self.ask(session, tree, word) if word in hit["text"]]
            if leaked:
                raise Failure(f"{session.name}/{tree.name}: {word} belongs to worktree {other.name} "
                              f"but was answered from {leaked}")

    # ---- the threads ---------------------------------------------------------------------------------

    def drive(self, session: Session, tree: Worktree, trees: list[Worktree]) -> None:
        others = [other for other in trees if other is not tree]
        try:
            while not self.stop.is_set():
                tree.step(lambda gone: self.verify(session, tree, gone))
                self.verify(session, tree)
                if tree.steps % 5 == 0:
                    self.foreign(session, tree, others)
                if tree.steps % 25 == 0:
                    session.call("status", cwd=str(tree.root))
                    session.call("symbol", name="seed_1_1_1", cwd=str(tree.root))
                    session.call("outline", path="pkg1/mod1.py", cwd=str(tree.root))
        except Failure as exc:
            self.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - anything else is a fault of the harness or the product; both end the run
            self.fail(f"{session.name}: {type(exc).__name__}: {exc}")

    def watch(self, session: Session, trees: list[Worktree]) -> None:
        """The session that only asks: seed content must be found in every worktree."""
        chooser = random.Random(99)
        try:
            while not self.stop.is_set():
                tree = chooser.choice(trees)
                package, module, number = chooser.randrange(10), chooser.randrange(15), chooser.randrange(6)
                name = f"seed_{package}_{module}_{number}"
                hits = session.call("search", query=name, k=3, cwd=str(tree.root))["hits"]
                if not hits or hits[0]["path"] != f"pkg{package}/mod{module}.py":
                    raise Failure(f"{session.name}/{tree.name}: {name} not answered from its file: "
                                  f"{[hit['path'] for hit in hits]}")
                time.sleep(0.02)
        except Failure as exc:
            self.fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"{session.name}: {type(exc).__name__}: {exc}")

    def collect(self, roots: list[Path]) -> None:
        """A separate process collects, from each worktree in turn: a collector never takes
        its own view's content, so one fixed worktree would be spared for the whole run."""
        age_days = 0 if self.args.harsh else COLLECT_AGE_SECONDS / 86400
        turn = 0
        while not self.stop.wait(self.args.collect_every):
            main = roots[turn % len(roots)]
            turn += 1
            proc = subprocess.run([sys.executable, "-m", "aleph_zero.cli", "--cwd", str(main), "gc", "--days", repr(age_days)],
                                  capture_output=True, text=True, env=self.env)
            if proc.returncode != 0:
                return self.fail(f"collector: {proc.stderr[-400:]}")
            removed = json.loads(proc.stdout)
            self.collected["runs"] += 1
            for key in ("blobs_removed", "chunks_removed"):
                self.collected[key] += removed.get(key, 0)

    def measure(self, sessions: list[Session]) -> None:
        """Private memory of every session, and of the worker and its supervisor, every ten
        seconds. Each session has its limit; all of them with the worker have the budget."""
        while not self.stop.wait(10):
            total = 0.0
            for session in sessions:
                try:
                    megabytes = memstat.private_bytes(session.proc.pid) / 1e6
                except memstat.MemstatError:
                    continue
                total += megabytes
                self.memory_mb[session.name] = max(self.memory_mb.get(session.name, 0.0), megabytes)
                if megabytes > SESSION_LIMIT_MB:
                    self.fail(f"{session.name}: {megabytes:.0f} MB of private memory, over {SESSION_LIMIT_MB} MB")
            for name, pid in self.worker_pids().items():
                try:
                    megabytes = memstat.private_bytes(pid) / 1e6
                except memstat.MemstatError:
                    continue
                total += megabytes
                self.memory_mb[name] = max(self.memory_mb.get(name, 0.0), megabytes)
            self.memory_mb["all together"] = max(self.memory_mb.get("all together", 0.0), total)
            if total > BUDGET_MB:
                self.fail(f"{total:.0f} MB of private memory in all, over the budget of {BUDGET_MB} MB")

    def worker_pids(self) -> dict[str, int]:
        cache = Path(self.env.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        try:
            state = json.loads((cache / "aleph-zero" / "worker" / "state.json").read_text())
        except (OSError, ValueError):
            return {}
        return {name: state[key] for name, key in (("worker", "worker_pid"), ("supervisor", "supervisor_pid"))
                if isinstance(state.get(key), int)}

    # ---- the end -----------------------------------------------------------------------------------------

    def final_checks(self, index: Path) -> dict:
        import numpy as np

        from aleph_zero import embedder
        from aleph_zero.vectors import ROW_BYTES, WIDTH

        conn = apsw.Connection(str(index))
        one = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
        report: dict = {}
        if one("PRAGMA integrity_check") != [("ok",)]:
            raise Failure(f"integrity_check: {one('PRAGMA integrity_check')[:3]}")
        if one("PRAGMA foreign_key_check"):
            raise Failure(f"foreign keys: {one('PRAGMA foreign_key_check')[:3]}")
        conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check')")
        chunks, indexed = one("SELECT count(*) FROM chunks")[0][0], one("SELECT count(*) FROM chunk_fts")[0][0]
        if chunks != indexed:
            raise Failure(f"{chunks} chunks but {indexed} rows in the text index")
        busy = one("PRAGMA wal_checkpoint(TRUNCATE)")[0][0]
        if busy:
            raise Failure("the write-ahead log could not be emptied: a transaction is still open somewhere")
        report.update(chunks=chunks, blobs=one("SELECT count(*) FROM blobs")[0][0])
        rows = one("SELECT chunk_id, row, kind FROM chunk_vectors")
        report["vectors"] = len(rows)
        if rows:
            kind = rows[0][2]
            limit = one("SELECT next_row FROM vector_files")[0][0]
            path = next(index.parent.glob("vectors.*.f16"))
            size = path.stat().st_size
            taken = [row for _, row, _ in rows]
            if len(set(taken)) != len(taken):
                raise Failure("two chunks share a row of the vector file")
            if max(taken) >= limit or limit * ROW_BYTES > size:
                raise Failure(f"vector rows up to {max(taken)}, next_row {limit}, file holds {size // ROW_BYTES}")
            free = [row[0] for row in one("SELECT row FROM vector_free")]
            if set(free) & set(taken) or len(free) + len(taken) != limit:
                raise Failure(f"{len(taken)} rows owned and {len(free)} free do not make up the file's {limit} rows")
            report["vector_rows_free"] = len(free)
            report.update(vector_rows_in_file=size // ROW_BYTES, vector_kind=kind)
            if not self.args.real_worker:
                from aleph_zero.embed import worker
                from aleph_zero.embed.client import EmbedClient

                os.environ["XDG_CACHE_HOME"] = self.env["XDG_CACHE_HOME"]
                client = EmbedClient(worker.socket_path(), capacity=worker.capacity_tokens())
                stored = np.memmap(path, dtype=np.float16, mode="r").reshape(-1, WIDTH)
                sample = random.Random(7).sample(rows, min(200, len(rows)))
                for chunk_id, row, _ in sample:
                    breadcrumb, signature, text = one(
                        f"SELECT breadcrumb, signature, text FROM chunks WHERE chunk_id = {chunk_id}")[0]
                    expected = client.embed([embedder.chunk_text(breadcrumb, signature, text)], kind="document")[0]
                    if not np.allclose(stored[row].astype(np.float32), np.asarray(expected, dtype=np.float32), atol=2e-3):
                        raise Failure(f"the vector stored for chunk {chunk_id} is not the vector of its text")
                report["vectors_checked"] = len(sample)
        conn.close()
        return report

    def run(self) -> dict:
        trees = self.build()
        sessions = [Session(f"session-{tree.name}", tree.root, self.env, self.logs) for tree in trees]
        asker = Session("session-asker", trees[0].root, self.env, self.logs)
        threads = [threading.Thread(target=self.drive, args=(session, tree, trees), daemon=True)
                   for session, tree in zip(sessions, trees)]
        threads.append(threading.Thread(target=self.watch, args=(asker, trees), daemon=True))
        threads.append(threading.Thread(target=self.collect, args=([tree.root for tree in trees],), daemon=True))
        threads.append(threading.Thread(target=self.measure, args=([*sessions, asker],), daemon=True))
        started = time.monotonic()
        for thread in threads:
            thread.start()
        self.stop.wait(self.args.minutes * 60)
        self.stop.set()
        for thread in threads:
            thread.join(timeout=90)
        report = {"minutes": round((time.monotonic() - started) / 60, 2), "harsh": self.args.harsh,
                  "real_worker": self.args.real_worker,
                  "steps": {tree.name: tree.steps for tree in trees},
                  "calls": {session.name: session.calls for session in [*sessions, asker]},
                  "slowest_call_ms": {session.name: round(session.slowest_ms) for session in [*sessions, asker]},
                  "collector": self.collected}
        index = None
        try:
            lost = 0
            for session, tree in zip(sessions, trees):
                status = session.call("status", cwd=str(tree.root))
                index = Path(status["index"])
                lost += status["lost_to_collection"]
                report.setdefault("vectors_by_worktree", {})[tree.name] = status["vectors"]
            for tree in trees:  # the session that only asks holds a view of each worktree, mostly idle
                lost += asker.call("status", cwd=str(tree.root))["lost_to_collection"]
            report["lost_to_collection"] = lost
            if lost and not self.args.harsh:
                raise Failure(f"{lost} blobs were collected while a session's view was using them")
        except Failure as exc:
            self.failures.append(str(exc))
        for session in [*sessions, asker]:
            try:
                session.close()
            except Failure as exc:
                self.failures.append(str(exc))
        report["memory_mb"] = {name: round(value, 1) for name, value in sorted(self.memory_mb.items())}
        if index is not None:
            try:
                report["index"] = self.final_checks(index)
            except Failure as exc:
                self.failures.append(str(exc))
        report["failures"] = self.failures
        if not self.args.real_worker:
            subprocess.run([sys.executable, "-m", "aleph_zero.cli", "embed", "stop"], env=self.env, capture_output=True)
        if not self.failures and not self.args.keep:
            shutil.rmtree(self.work, ignore_errors=True)
        else:
            report["kept"] = str(self.work)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=2.0)
    parser.add_argument("--real-worker", action="store_true", help="embed with the installed worker, not the stand-in")
    parser.add_argument("--harsh", action="store_true", help="collect with age zero: content in use is taken too")
    parser.add_argument("--collect-every", type=float, default=15.0, help="seconds between collections")
    parser.add_argument("--keep", action="store_true", help="keep the working directory even when nothing failed")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = Run(args).run()
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
