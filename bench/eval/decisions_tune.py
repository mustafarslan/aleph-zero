"""Choose the decision ranker's settings on development pairs, repository by repository left out.

Usage:
  uv run python bench/eval/decisions_tune.py bench/data/decisions-pub --labels labels-pub.json [--out FILE]

`<workdir>` holds a clone of each corpus and the labels `decision_labels.py` wrote. Words only: no
vectors, no worker. Nothing here reads oxn's 100 pairs.

The protocol, written down on 2026-10-12 before any setting was measured on these pairs:

- pairs and candidates: as the labels file has them (the tightened citation rule; the records listed
  there). The task text is the harness's (`decisions.task_text`): the commit minus the citation and
  the plumbing.
- the settings tried: a word of the title counts 1, 2 or 3 times; of the Decision section 1 or 2
  times; a word of the task's first line 1 or 2 times; BM25's b 0.75 or 0.4; words as written or
  reduced to stems. 48 settings. The first (1, 1, 1, 0.75, as written) is the ranker as measured.
- choosing: a corpus with at least ten pairs is a fold. For each fold, the setting with the highest
  precision at 3 on the pairs of all other corpora (ties: higher MRR, then the earlier setting in the
  list, which is the one nearer the measured ranker) is scored on the fold. The pooled result over the
  folds is the estimate of what choosing this way gives on a repository not seen.
- beside it, on the same pairs: the measured ranker, and oxn's own ranker (`decisions.reference_order`).
- the difference in precision at 3 to each is given with a 90% interval from 10,000 paired resamples
  of the pairs (seed 20261012).
- what is taken further: the setting chosen on all pairs together, if (a) the pooled held-out precision
  at 3 is at least 2 points above the measured ranker's with MRR not lower, and (b) at least two folds
  choose a setting that agrees with it in the title weight and in b. Otherwise the measured ranker
  stands and there is nothing new to run on the 100.
- the one run on oxn's 100 pairs with a setting taken further is the third on those pairs and is
  reported whatever it shows. It becomes the engine's default only if its precision at 3 there is not
  below the 0.820 measured now. "Better than oxn" is said only if it is above 0.820 there and not below
  oxn's ranker on the held-out pairs here.

Vectors (`--dense`; written down on 2026-10-12 after the word settings were measured and none was taken
further, and before any vector was made for these pairs):

- the words are the measured ranker's: its BM25 score for each record.
- a record's nearness to a task is the cosine of the task's vector (the task text, cut to 5,000
  characters, embedded as a query) with the record's (embedded as a document), in two variants:
  `record`, one vector of the title, the Decision section and then the body from its start, cut to
  5,000 characters; `passages`, the body cut at blank lines into passages of at most 1,000 characters,
  each under the title, the record's nearness being that of its nearest passage.
- fusion: for one task both scores are scaled to 0..1 over the candidates (from the lowest to the
  highest; a record that shares no word with the task has a BM25 of 0) and added as
  a * words + (1 - a) * nearness, with a one of 0.9, 0.8, 0.7, 0.6, 0.5. Ties: the record's number.
- ten settings beside the measured ranker. Choosing as above. Taken further only if the pooled
  held-out precision at 3 is at least 2 points above the measured ranker's with MRR not lower, and at
  least two folds choose the variant of the overall choice with an `a` within 0.1 of its.
- if taken further: it is built into the engine under tests, one of them that the engine's order
  equals this script's on every development pair, and then run once on oxn's 100 through the engine.
  That is the third run of `aleph decisions` on those pairs (after words, and stems) and the fifth of
  anything of Aleph's (ordinary search by words, and with vectors).
- if not: this effort ends here. By words the two rankers are one function, and no reranking model
  goes into the engine.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions as harness  # noqa: E402

from aleph_zero import decisions as records_of  # noqa: E402

SEED = 20261012
SETTINGS = [{"title": title, "decision": decision, "subject": subject, "b": b, "stems": stems}
            for stems, b, subject, decision, title in itertools.product((False, True), (0.75, 0.4), (1.0, 2.0), (1.0, 2.0), (1.0, 2.0, 3.0))]
MEASURED = SETTINGS[0]
FOLD = 10


def load(workdir: Path, labels: dict) -> dict[str, dict]:
    """Per corpus: the records as the engine parses them, their raw texts, and each pair's task text and gold."""
    corpora = {}
    for name, spec in labels["corpora"].items():
        root = workdir / name
        texts = {path: harness.git(root, "show", f"{spec['sha']}:{path}") for path in spec["records"]}
        parsed = [records_of.parse(path, records_of.is_record(path), text) for path, text in texts.items()]
        corpora[name] = {"parsed": parsed, "texts": texts, "numbers": {each.path: each.number for each in parsed},
                         "pairs": [{"sha": pair["sha"], "gold": set(pair["gold"]), "task": harness.task_text(root, pair["sha"])}
                                   for pair in spec["pairs"]]}
    return corpora


def orders(corpus: dict, setting: dict) -> list[list[int]]:
    ranker = records_of.Records(corpus["parsed"], stems=setting["stems"], b=setting["b"],
                                fields=(setting["title"], setting["decision"], 1.0), subject=setting["subject"])
    return [[ranker.decisions[at].number for at, _ in ranker.by_words(pair["task"])] for pair in corpus["pairs"]]


def rows_of(corpus: dict, ranked: list[list[int]]) -> list[dict]:
    return [{"gold": pair["gold"], "order": order} for pair, order in zip(corpus["pairs"], ranked)]


def hit3(row: dict) -> int:
    return int(any(number in row["gold"] for number in row["order"][:3]))


def interval(ours: list[dict], theirs: list[dict]) -> list[float]:
    """The difference in precision at 3, ours minus theirs, with a 90% interval over paired resamples."""
    gains = [hit3(a) - hit3(b) for a, b in zip(ours, theirs)]
    chooser = random.Random(SEED)
    means = sorted(sum(chooser.choices(gains, k=len(gains))) / len(gains) for _ in range(10_000))
    return [round(sum(gains) / len(gains), 3), round(means[500], 3), round(means[9500], 3)]


def best(table: dict[int, dict[str, list[dict]]], names: list[str]) -> int:
    """The setting with the highest precision at 3 on the named corpora; ties: MRR, then the earlier setting."""
    def key(at: int):
        found = harness.score([row for name in names for row in table[at][name]])
        return (-found["p_at_3"], -found["mrr"], at)
    return min(range(len(SETTINGS)), key=key)


# ---- vectors -------------------------------------------------------------------------------------------

CUT, PASSAGE = 5000, 1000
VARIANTS = ("record", "passages")
WEIGHTS = (0.9, 0.8, 0.7, 0.6, 0.5)
DENSE = [{"variant": variant, "a": a} for variant in VARIANTS for a in WEIGHTS]


def record_text(each) -> str:
    return f"{each.title}\n\n{each.decision}\n\n{each.text}"[:CUT]


def passages(each) -> list[str]:
    out, held = [], ""
    for block in each.text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if held and len(held) + len(block) + 2 > PASSAGE:
            out.append(held)
            held = ""
        held = f"{held}\n\n{block}" if held else block
        while len(held) > PASSAGE:
            out.append(held[:PASSAGE])
            held = held[PASSAGE:]
    if held:
        out.append(held)
    return [f"{each.title}\n\n{passage}" for passage in out] or [each.title]


class Vectors:
    """Vectors of texts, made once by the embedding worker and kept beside the clones."""

    def __init__(self, workdir: Path) -> None:
        import numpy as np
        from aleph_zero.embed import client

        self._np, self._kind, self._file = np, client.VECTOR_KIND, workdir / "tune-vectors.npz"
        self._known = dict(np.load(self._file)) if self._file.exists() else {}

    def of(self, texts: list[str], kind: str):
        import hashlib

        from aleph_zero.embed import client, worker

        keys = [hashlib.sha256(f"{self._kind}\0{kind}\0{text}".encode()).hexdigest() for text in texts]
        missing = [at for at, key in enumerate(keys) if key not in self._known]
        if missing:
            pool = client.EmbedPool(worker.ensure(), capacity=worker.capacity_tokens(), connections=worker.slots())
            try:
                made = pool.embed([texts[at] for at in missing], kind)
            finally:
                pool.close()
            for at, row in zip(missing, made):
                self._known[keys[at]] = row
            self._np.savez(self._file, **self._known)
        return self._np.stack([self._known[key] for key in keys]) if keys else self._np.zeros((0, 1))


def nearness(corpus: dict, vectors: Vectors) -> dict[str, list[list[float]]]:
    """Per variant: for each pair, the cosine of its task with every record, in the records' order."""
    np = vectors._np
    tasks = vectors.of([pair["task"][:CUT] or "(empty)" for pair in corpus["pairs"]], "query")
    whole = vectors.of([record_text(each) for each in corpus["parsed"]], "document")
    cut = [passages(each) for each in corpus["parsed"]]
    flat = vectors.of([passage for some in cut for passage in some], "document")
    owner = np.array([at for at, some in enumerate(cut) for _ in some])
    by_passage = tasks @ flat.T
    nearest = np.stack([by_passage[:, owner == at].max(axis=1) for at in range(len(cut))], axis=1)
    return {"record": (tasks @ whole.T).tolist(), "passages": nearest.tolist()}


def scaled(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    return [(value - low) / (high - low) if high > low else 0.0 for value in values]


def fused_orders(corpus: dict, near: list[list[float]], a: float) -> list[list[int]]:
    ranker = records_of.Records(corpus["parsed"], stems=False, b=0.75, fields=(1.0, 1.0, 1.0), subject=1.0)
    numbers = [each.number for each in ranker.decisions]
    place = {each.path: at for at, each in enumerate(corpus["parsed"])}        # `near` is in the order of `parsed`
    out = []
    for pair, cosines in zip(corpus["pairs"], near):
        words = dict(ranker.by_words(pair["task"]))
        by_words = scaled([words.get(at, 0.0) for at in range(len(numbers))])
        by_nearness = scaled([cosines[place[each.path]] for each in ranker.decisions])
        total = [a * w + (1 - a) * n for w, n in zip(by_words, by_nearness)]
        out.append([numbers[at] for at in sorted(range(len(numbers)), key=lambda at: (-total[at], numbers[at]))])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--labels", default="labels-pub.json")
    parser.add_argument("--dense", action="store_true", help="the ten settings with vectors instead of the 48 by words (needs the embed worker)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    corpora = load(args.workdir, json.loads((args.workdir / args.labels).read_text()))
    global SETTINGS
    if args.dense:
        vectors = Vectors(args.workdir)
        near = {name: nearness(corpus, vectors) for name, corpus in corpora.items()}
        SETTINGS = [MEASURED, *DENSE]
        table = {0: {name: rows_of(corpus, orders(corpus, MEASURED)) for name, corpus in corpora.items()}}
        for at, setting in enumerate(DENSE, 1):
            table[at] = {name: rows_of(corpus, fused_orders(corpus, near[name][setting["variant"]], setting["a"])) for name, corpus in corpora.items()}
    else:
        table = {at: {name: rows_of(corpus, orders(corpus, setting)) for name, corpus in corpora.items()} for at, setting in enumerate(SETTINGS)}
    reference = {name: rows_of(corpus, [harness.reference_order(corpus["texts"], corpus["numbers"], pair["task"]) for pair in corpus["pairs"]])
                 for name, corpus in corpora.items()}
    folds = [name for name, corpus in corpora.items() if len(corpus["pairs"]) >= FOLD]
    chosen, held, measured, theirs = {}, [], [], []
    for fold in folds:
        at = best(table, [name for name in corpora if name != fold])
        chosen[fold] = at
        held += table[at][fold]
        measured += table[0][fold]
        theirs += reference[fold]
    overall = best(table, list(corpora))
    everything = lambda rows: [row for name in corpora for row in rows[name]]  # noqa: E731
    gain = interval(held, measured) if held else [0.0, 0.0, 0.0]
    same = (lambda one, other: one.get("variant") == other.get("variant") and abs(one.get("a", 1.0) - other.get("a", 1.0)) <= 0.1001) if args.dense \
        else (lambda one, other: one["title"] == other["title"] and one["b"] == other["b"])
    agree = sum(same(SETTINGS[at], SETTINGS[overall]) for at in chosen.values())
    report = {
        "pairs": {name: len(corpus["pairs"]) for name, corpus in corpora.items()},
        "records": {name: len(corpus["parsed"]) for name, corpus in corpora.items()},
        "folds": {fold: {"chosen on the others": SETTINGS[at], "on this fold": harness.score(table[at][fold]),
                         "the measured ranker": harness.score(table[0][fold]), "oxn's ranker": harness.score(reference[fold])}
                  for fold, at in chosen.items()},
        "held out, pooled": {"chosen": harness.score(held), "the measured ranker": harness.score(measured), "oxn's ranker": harness.score(theirs),
                             "p_at_3 chosen minus measured [difference, 5%, 95%]": gain,
                             "p_at_3 chosen minus oxn's [difference, 5%, 95%]": interval(held, theirs) if held else None,
                             "p_at_3 measured minus oxn's [difference, 5%, 95%]": interval(measured, theirs) if held else None},
        "on all pairs (not held out)": {"chosen": SETTINGS[overall], "its score": harness.score(everything(table[overall])),
                                        "the measured ranker": harness.score(everything(table[0])), "oxn's ranker": harness.score(everything(reference))},
        "taken further": bool(held) and overall != 0 and gain[0] >= 0.02 and harness.score(held)["mrr"] >= harness.score(measured)["mrr"] and agree >= 2,
        "folds that agree with the overall choice": agree,
        "every setting on all pairs (not held out)": [{**SETTINGS[at], **harness.score(everything(table[at]))} for at in sorted(table)],
    }
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
