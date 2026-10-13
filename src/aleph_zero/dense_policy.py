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


# Filled in from Benchmark C, one language at a time. A language not listed has not been measured.
VERDICTS: dict[str, Verdict] = {
    # The gate was fixed before any run (ADR-002): 5 points of file recall@10 on issue titles, with an interval
    # that excludes zero. On the first 235 questions it read +5.5, at a candidate pool chosen on those questions,
    # and was recorded as provisional. The 165 questions held out for the check gave +1.6 (-3.4 to +6.9), and all
    # 400 together +3.9 (+0.6 to +7.2): vectors help Python, by less than the bar. On whole issue texts they add
    # 11 points; an agent's query is nearer a title.
    "python": Verdict(False, "+3.9 points of file recall@10 on 400 Loc-Bench questions asked by their issue titles, interval +0.6 to +7.2, "
                             "and +1.6 on the 165 held out: under the 5-point bar; on whole issue texts +11.0; ADR-002, 2026-10-13"),
}

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
    return False, (f"the benchmark for {language} says vectors do not help enough ({verdict.evidence}); "
                   "`--dense` or ALEPH_DENSE=1 switches them on by hand")
