"""Whether search by meaning (vectors) is on for a project.

The answer depends on the project's main language. Vectors cost a model, a GPU and
minutes of embedding, and whether they find more of the right code than words and
names do is an empirical question whose answer need not be the same for Python and
for C++. So there is one benchmark verdict per language (Benchmark C, ADR-002), and a
project uses the verdict of the language most of its source is written in.

A language with no verdict is off: nothing is switched on by default on the strength
of another language's result. The owner can always decide by hand (`--dense`,
`--no-dense`, ALEPH_DENSE=1 or 0).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    helps: bool        # hybrid beat lexical by the gate's margin on this language's questions
    evidence: str      # the numbers, in a few words, and where they are recorded


# Filled in from Benchmark C, one language at a time. Empty means: not yet measured.
VERDICTS: dict[str, Verdict] = {}

# Languages that share a verdict because they share a benchmark.
_SAME_AS = {"tsx": "typescript"}


def decide(setting: bool | None, language: str | None) -> tuple[bool, str]:
    """(on or off, why) for a project whose main language is `language`. `setting` is the
    owner's explicit choice, or None to follow the benchmark."""
    if setting is not None:
        return setting, "switched on by hand" if setting else "switched off by hand"
    if language is None:
        return False, "this worktree has no source code in a language Aleph knows"
    language = _SAME_AS.get(language, language)
    verdict = VERDICTS.get(language)
    if verdict is None:
        return False, (f"no benchmark has been run for {language} yet, so search is by words and names; "
                       "`--dense` or ALEPH_DENSE=1 switches vectors on by hand")
    if verdict.helps:
        return True, f"the benchmark for {language} says vectors help ({verdict.evidence})"
    return False, f"the benchmark for {language} says vectors do not help enough ({verdict.evidence})"
