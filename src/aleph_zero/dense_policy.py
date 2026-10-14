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
    # The same gate, with how it would be read written down before the run: nothing was tuned on C++, so this
    # first reading is at settings chosen elsewhere. Two repositories give 80 of the 108 questions; resampling
    # repositories instead of questions the interval is +3.8 to +13.8.
    "cpp": Verdict(True, "+6.7 points of file recall@10, interval +1.4 to +12.3, on 108 questions from five repositories "
                         "asked by their first line; ADR-002, 2026-10-13"),
    # Four languages on public repositories, released by the owner on 2026-10-13; the reading was written down for
    # all four before anything was embedded, and is C++'s. Where the first line shows nothing, whole issue text still
    # gains much: an agent's own query is nearer a first line, and that is what the gate reads.
    #
    # A verdict is applied by a repository's main language, so it is read from the questions in repositories the
    # engine calls that language at the question's commit, not from a question set's label: 62 questions filed
    # under TypeScript are in JavaScript repositories (ADR-002, "Which questions a verdict is read from"). By label
    # TypeScript read +10.2 on 165 and JavaScript +3.7 on 230, which held zero. Read by main language JavaScript
    # passed on the 284 asked (+6.1); since that was seen after the figures, it was switched on only when 164
    # questions never asked passed alone, by a rule written before they were run.
    "typescript": Verdict(True, "+7.3 points of file recall@10, interval +1.8 to +12.9, on 110 questions from nine repositories "
                                "whose main language is TypeScript, asked by their first line; ADR-002, 2026-10-13"),
    "go": Verdict(False, "+2.0 points of file recall@10 on 111 questions from eight repositories whose main language is Go, asked by "
                         "their first line, interval -3.9 to +7.8, which holds zero; on whole issue text +18.4; ADR-002, 2026-10-13"),
    "javascript": Verdict(True, "+16.2 points of file recall@10, interval +9.4 to +23.1, on 164 questions never asked before, from five "
                                "repositories whose main language is JavaScript, asked by their first line; +9.8 (+6.0 to +13.6) on all "
                                "448 from twelve repositories; ADR-002, 2026-10-13"),
    # C, read on the owner's "check for each language" (2026-10-14), by main language from the start: half of ponyc's
    # questions are in commits that are mostly C++ and count there (C++ with them: +6.6 on 128 questions, still on).
    "c": Verdict(True, "+12.3 points of file recall@10, interval +3.7 to +21.3, on 88 questions from six repositories whose "
                       "main language is C, asked by their first line; on whole issue text +19.8; ADR-002, 2026-10-14"),
    "java": Verdict(False, "+0.1 points of file recall@10 on 296 questions from eighteen repositories asked by their first line, "
                           "interval -3.2 to +3.4, which holds zero; on whole issue text +5.3; ADR-002, 2026-10-13"),
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
