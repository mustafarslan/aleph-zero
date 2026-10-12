# ADR-011: Steering Claude Code towards Aleph, and how it is measured

Status: built; the measurement that decides the default rung has not been run (M4, 2026-10-12)

## Question

An MCP server only helps if the agent calls it. How far should Aleph go to make Claude Code use it, and how is it decided whether that pays?

## What was built

A Claude Code plugin in `plugin/`: a manifest, the MCP entry that starts this checkout's engine, a skill, and a mod (`hooks/register.js`, plain JavaScript, no build step). The mod holds no retrieval logic; what it knows about the code comes from the engine. Its decision rules are plain functions in `hooks/logic.js` with their own tests.

Steering is a ladder, one option (`rung`) in the plugin's settings:

| Rung | What it does | How |
|---|---|---|
| 0 | Nothing beyond the engine's tools | |
| 1 (default until measured) | All four tools loaded up front with tuned descriptions; one stable section in the system prompt saying to search before recursive grep, and when grep is still right; a pointer to the engine in the descriptions of Claude Code's own `Grep` and `Glob`, with its own switch (`grep_pointer`) so that it can be measured apart | `tool.describe`, `prompt.compose` |
| 2 | On the first prompt of a session, if it is a sentence and two of the engine's generators agree on the top hit, up to five locations (path, lines, kind, name; never code) ride along as context | `prompt.submit` |
| 3 | A recursive `grep`, `rg` or `git grep` that reported no match (by its exit status, not merely by printing nothing), or a `find -name` without an action that printed nothing, gets ranked locations appended, worded so that it cannot be read as "occurrences remain" | `tool.call` on Bash, after the command |

A meter runs on every rung: tokens per request (main conversation and subagents apart), tool calls by name, and which of Aleph and grep was used first. `/aleph stats` shows it, `/aleph stats last` the session before, `/aleph check` whether the mod can reach the engine.

L4 of the design (answering or redirecting a grep before it runs) is not built; it was conditional on L1 to L3 leaving a measured gap.

## Decisions that differ from the design, and why

- **The standing instruction is added through `prompt.compose`.** The design named `prompt.section`, which only rewrites a section that already exists.
- **L2 acts on the first prompt of a session only.** The design attached candidates to every prompt. Unrequested context on every turn ("thanks", "commit this") anchors the agent on files it did not ask about; agy argued for dropping L2 or restricting it to the first prompt. It is still measured, as the design asks, and dropped if flat or negative.
- **L2's bar is agreement, not a score.** Rank-fusion scores say little by their size. The bar is that the top hit was proposed by at least two generators (words, names, paths, vectors). agy's second condition, a 25% margin over the second hit, is not used: two hits that both have two generators differ by a percent or two, so it would nearly always refuse.
- **L3 acts only when the search reported no match, never on a flood.** A flood means a generic pattern, for which ranked locations are just as generic. "No match" is the command's exit status as Claude Code reports it, not an empty output: a quiet search (`-q`) prints nothing when it matches, and a `find` with `-delete` or `-exec` prints nothing after doing its work. The review found the first version telling the agent "it is gone" in both cases. Commands whose silence means something else (quiet, inverted, files-without-match, any `find` action), commands aimed outside the repository (also through a leading `cd`), several commands on several lines, a search of history (`git grep pattern HEAD~5`) and a search of one file are all left alone.
- **L2's one chance goes to the first prompt that is a question,** not to the first prompt: "hi" does not use it up, and `/clear` gives it back.
- **Subagent prompts are not rewritten.** The documentation gives `agent.spawn` a model change or a refusal, nothing else. A search subagent still has the tools and the section.
- **No status line.** The mods API has nothing to contribute to one.

## What the documentation did not say (found in real sessions, Claude Code 2.1.296)

- `$.mcp.call` wants the server's full name (`plugin:aleph-zero:aleph`); the manifest's short name is refused. `$.mcp.connect('aleph')` returns the full one.
- A mod's own calls to its server are subject to tool permission. Until the engine's tools are allowed (`"mcp__plugin_aleph-zero_aleph__*"` in `permissions.allow`), rungs 2 and 3 add nothing; `/aleph check` says so.
- A mod's own engine calls arrive at its `tool.call` hooks like the agent's. The meter tells them apart by `next.origin`.
- Time inside a mods API call does not count against a hook's ten seconds, and `$.mcp.call` has no timeout. A transport that never answers would hold the prompt for ever, so every engine call is raced against `$.clock.sleep` (450 ms; the engine itself is given 400).
- A skill and a mod command with the same name collide and the skill wins; the skill is `find-code`, the command `/aleph`.
- Option values for a plugin loaded with `--plugin-dir` are set as `{"pluginConfigs": {"aleph-zero@inline": {"options": {"rung": "3"}}}}`.
- The type declarations for the installed build are written only when a mod is loaded in an interactive session. This work could not start one; the hooks were written against the documentation and the shapes of real events recorded by a probe mod in a headless session.

## What was verified, and how

- 32 tests under `claude plugin test` (decision rules; each rung through the test kit; an engine that fails, is refused, or never answers leaves the prompt and the command result untouched). Breaking the timeout race and one rung check made three of them fail.
- `claude plugin validate --strict` passes; the Python suite runs it and the mod's tests where Claude Code is installed.
- In real headless sessions at rung 3 the model quoted back the locations it received with its prompt and the note appended to an empty grep, and its system prompt held the tuned description and the section.

## How it is measured

`bench/ab/run.py`: for each Loc-Bench issue and each arm, a fresh `claude -p` session in a temporary worktree at the issue's base commit is asked which files must change, bounded by a cost limit and a wall clock. Arms: `none` (no plugin), the plugin at rungs 0 to 3, and rung 1 without the Grep and Glob pointer, interleaved per task. A task run more than once in an arm counts with the mean of its runs; failed sessions and tasks without a partner are reported, and records of more than one model are refused. Read from the session's event stream: tokens, tool calls by name, time, the files it named.

**Fixed before any session was run**, so the gate is not chosen after seeing results:

- Primary measure: total tokens of the session (input, output, cache read and cache write; every model, so subagents count).
- Comparison: the ratio per task between an arm and the baseline, geometric mean, with a bootstrap interval over tasks.
- Second measure: tool calls, the same way.
- "No drop in hit rate": the arm's file hit rate is not lower than the baseline's by more than one task of the set.
- Reported beside these and deciding nothing: tool calls before the first read of a gold file; sessions that named no file (a rung that makes the agent give up early looks cheap); cost.

Two things to know when reading it:

- The design's gate says "than no plugin" while its ladder calls L0 "the engine without the mod". Those are different baselines. `none` answers whether Aleph helps at all; L0 answers what steering adds. Which one the gate is read against is the owner's decision.
- L0 already has `search` loaded up front (the engine marks it so). L1 adds the tuned descriptions, the other three tools up front and the prompt section, not the visibility of `search`.

## Can the planned run decide the gate?

agy's arithmetic, which I could not fault: with run-to-run noise of a factor of two, 30 paired tasks detect only effects of about 30% or more; a true 20% often misses, and one task is 3.3 points of hit rate. Four rungs at 30 tasks each (120 sessions) therefore cannot read a 20% gate. The same 120 sessions on two arms (60 tasks, or 30 tasks twice) come closer. The pilot's own noise is below.

## The pilot (2026-10-12): the harness works; rung 1 alone is mostly ignored

Eight sessions, to prove the plumbing and to see cost and noise. Not a result about the gate.

| Task | Model | Arm | Tokens | Tool calls | Aleph calls | Right file named | Cost |
|---|---|---|---|---|---|---|---|
| jax 25114 | cheap | none | 147,909 | 5 | | yes | $0.007 |
| | | rung 1 without the pointer | 166,708 | 5 | 1 | yes | $0.011 |
| moto 8401 | mid | none | 457,930 | 10 | | yes | $0.31 |
| | | rung 1 without the pointer | 438,367 | 13 | 1 | yes | $0.35 |
| trl 2433 | mid | none | 280,258 | 9 | | yes | $0.27 |
| | | rung 1 without the pointer | 222,870 | 6 | **0** | yes | $0.22 |
| pydantic 10210 | mid | none | 689,333 | 17 | | yes | $0.42 |
| | | rung 1 without the pointer | 426,426 | 10 | **0** | yes | $0.25 |

What it shows:

- **The harness reads sessions correctly** and the rung reaches the mod (the plugin arm searched with Aleph where the baseline could not).
- **Cost:** about $0.30 a session on the mid model, a cent on the cheap one. Sixty sessions on the mid model are about $20 of plan usage; the model you work with costs more.
- **Noise is as large as the effect the gate asks for.** On the three mid-model tasks the plugin arm used 22% fewer tokens on average, but in two of those three sessions the agent never called Aleph at all: that difference is run-to-run variation, nothing else. Three tasks say nothing about the gate, and this is the size of difference a real run has to tell apart from chance.
- **Rung 1 as first written was followed in two sessions of four** (one of three on the mid model). The agent read the standing instruction and still reached for Grep. Since then rung 1 also adds a pointer to the descriptions of Claude Code's own `Grep` and `Glob` tools, the text the agent is reading when it chooses them. That change has not been measured: **rung 1 as it ships has not been run in any session.** The pointer has its own switch, and the harness has an arm for rung 1 without it (`L1n`, what the pilot ran), so a run can tell the pointer from the rest of the rung.
- Tokens are dominated by cache reads of the fixed context (about 34,000 tokens per request before the plugin adds its descriptions), which is the same in both arms; a 20% saving in total tokens therefore needs a much larger saving in the part that varies. Tool calls and cost are reported beside tokens for that reason.

The four tasks of the pilot are listed in `bench/ab/dev_tasks.txt` and left out of every later run: the steering was adjusted after looking at them.

## Views consulted

- **Advisor:** the wrong event for adding a prompt section; the hung-transport hole in the "never blocks" rule; prove headless loading in one command before building rungs; fix the measures before any session runs; pilot with two sessions before eight; do not wait for the build's types.
- **Independent review of the branch (a fresh reviewer, most capable model):** no critical finding. Three important ones, all fixed: L3 said "it is gone" after a quiet grep that matched and after `find -delete`; L3 fired on searches aimed outside the repository through a leading `cd` and on multi-line command lists; the report kept only the last run of a task, which defeats the "thirty tasks twice" design. Smaller: patterns misread in several shapes, L2's chance used up by "hi", the pointer not separable from rung 1, file names in answers stripped wrongly. It confirmed the pilot's figures against the data and that the rung setting reaches the mod.
- **agy (no boost):** L2 on the first prompt only and by agreement of generators (adopted, except its score margin); L3 on empty results only, with the three misfires designed against (adopted); the power arithmetic and the "gave up early" trap (adopted into what the harness reports).
