# ADR-011: Steering Claude Code towards Aleph, and how it is measured

Status: built (M4, 2026-10-12); measured once (2026-10-13): rung 2 is not shown to save tokens or tool calls on issue-to-file localisation, and the design's gate is not met. The default stays rung 1, which is not shown to do anything either (below).

## Question

An MCP server only helps if the agent calls it. How far should Aleph go to make Claude Code use it, and how is it decided whether that pays?

## What was built

A Claude Code plugin in `plugin/`: a manifest, the MCP entry that starts this checkout's engine, a skill, and a mod (`hooks/register.js`, plain JavaScript, no build step). The mod holds no retrieval logic; what it knows about the code comes from the engine. Its decision rules are plain functions in `hooks/logic.js` with their own tests.

Steering is a ladder, one option (`rung`) in the plugin's settings:

| Rung | What it does | How |
|---|---|---|
| 0 | Nothing beyond the engine's tools | |
| 1 (default until measured) | All four tools loaded up front with tuned descriptions; one stable section in the system prompt saying to search before recursive grep, and when grep is still right; a pointer to the engine in the descriptions of Claude Code's own `Grep` and `Glob`, with its own switch (`grep_pointer`) so that it can be measured apart | `tool.describe`, `prompt.compose` |
| 2 | On the first prompt of a session, if it is a sentence and two of the engine's generators agree on the top hit, up to five files of code (path, the lines of up to three places in it, kind, name; never code itself) ride along as context. Until 2026-10-13: five places of any kind, a line each | `prompt.submit` |
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

## The run of 2026-10-13: what was fixed before its first session

The owner released the run and left its choices to me with the advisor and agy ("go work on 7 items and yes you can do them"). Three voices on the design, kept apart. *agy:* compare no plugin with rung 2, where the context is put in for the agent, so that whether the agent followed the steering is no longer mixed into the result (in the pilot rung 1 was followed in two sessions of four); run with vectors on; do not stop for low use of the tools. *The advisor:* rung 2, yes; vectors as the product ships them, which is off since the held-out check (ADR-002): a win with them on would not say that what ships wins; the mid model; a first pass of 60 sessions capped by the harness; and start only when the benchmark's indexes are up to date, because the sessions' worktrees share them. *Mine:* the advisor's.

- **Arms:** `none` (Claude Code as it is) and `L2`. **Model:** sonnet. **Tasks:** the harness's 30 (`--count 30`, seed 11), each once in each arm, interleaved: 60 sessions.
- **Bounds:** `--budget-usd 0.75 --max-sessions 60`: at most $45, about $18 expected from the pilot. 900 s of wall clock a session.
- **The product as it ships:** vectors off (the embedding worker is not consulted), the handshake revision of MCP (the stateless one stays behind its switch until this pass is over), the plugin from this repository at the commit of the run.
- **Read as fixed above:** total tokens, the ratio per task, geometric mean with a bootstrap interval over tasks; tool calls the same way; the hit rate not lower by more than one task. The gate is the design's: 20% fewer tokens or tool calls than no plugin. Thirty pairs can show an effect of about 30% and cannot settle a smaller one (see below): a ratio whose interval holds 1 is recorded as "not shown", not as a pass and not as a fail.
- **A second pass** of the same 60 sessions is made only if the first cost at most $22.50 and its token ratio is 0.90 or lower: an effect worth the money to resolve, within the same $45.
- **What the agent types, kept and read (new).** The gate for vectors is read on issue titles as "the closest to what an agent types"; that was never looked at. Every query a session sends to the engine's search is kept word for word. If at least 20 queries are logged: a median of 12 words or fewer leaves the title form as it is; a median over 20 words means the Python verdict is read again on whole-issue text, by a rule written down then, before that reading; in between, it is noted and nothing changes. Fewer than 20 queries: nothing is concluded.
- **Known and equal in both arms:** the owner's other plugins and connectors load in every headless session (about 35 servers were listed in a check): they add tokens to both arms alike and are not removed, since this is the owner's Claude Code as it runs.

### Result of the first pass (2026-10-13, 07:01 to 07:37)

60 sessions, none failed, $12.10 ($6.04 without the plugin, $6.06 on rung 2). Read by the rules above, in their order:

| Measure, rung 2 against no plugin, 30 pairs | Ratio (geometric mean) | 95% interval | Reading |
|---|---|---|---|
| Total tokens | 1.02 | 0.915 to 1.142 | **not shown**: the interval holds 1 |
| Tool calls | 0.948 | 0.824 to 1.089 | **not shown**: the interval holds 1 |

- **Hit rate:** the right file was named in 29 of 30 sessions on rung 2 and in 30 of 30 without the plugin: lower by one task, which is as far as the rule allows and no further.
- **The design's gate** (20% fewer tokens or tool calls than no plugin) **is not met on this pass.** Thirty pairs could have shown an effect of about 30%; they show none, and they cannot settle a smaller one either way. The point estimate for tokens is on the wrong side of 1.
- **No second pass.** The cost was under the $22.50 that was one condition; the token ratio is not 0.90 or lower, which was the other.
- **What the agent types:** two queries were sent to the engine in 60 sessions (6 and 10 words). Fewer than 20: nothing is concluded, and Python's verdict on vectors is not read again.
- Medians, for scale: 160,080 tokens and 4 tool calls a session without the plugin, 167,958 and 5 on rung 2.

### Read afterwards, and not a measure of the run

What follows was looked at after the result was known. It says where to look next; it does not change the reading above.

- **Whether rung 2 did anything in a session was not kept by the harness.** The mod adds its locations only when two generators proposed the first hit. `bench/ab/injected.py` asks the engine again with each session's prompt at its commit and reads the answer as the mod does; against the mod's own meter, copied after 26 of the 30 rung-2 sessions, it agrees in all 26. Locations were added in 22 of 30 sessions; a file of the answer was among them in 14.
- **The pairs by what rung 2 gave them** (ratio of tokens, of tool calls): a file of the answer among the locations, 14 pairs: 0.984, 0.94. Locations without a file of the answer, 8 pairs: 1.18, 1.114. Nothing added, 8 pairs: 0.937, 0.817. Groups of 8 and 14 with run-to-run differences of 20 to 40% (above) carry no interval worth printing; the order of the three is what one would expect and no more is claimed.
- **What the locations were.** Of five, two are kept for documentation, and for an issue text those were often the repository's own issue templates, its `CONTRIBUTING.md`, its `README.md` or its pull-request template: the words of a bug report match the form it was written on.
- **What a session costs in either arm.** A session without the plugin took 5 turns at the median and 30,000 tokens a turn; on rung 2, 6 turns and 33,100 a turn. The agent without the plugin named the right file in all 30 tasks, in 4 tool calls at the median: on these tasks there is little left to save.
- **The engine was called by the agent in 2 of 30 rung-2 sessions.** Two sessions say nothing.

### What was changed after the pass, and on what evidence (2026-10-13)

Rung 2 now asks the engine for code only and fills five lines with five files, the further places of a listed file riding on its line (`src/a.py:3-9, 40-46  function  alpha`); until now it took the engine's five results of any kind, a line a result. The engine's ranking and its rule for documentation among results are not touched: every verdict of ADR-002 was read with them as they are.

What "better" would mean was written before anything was run (the ledger of that day): on the 400 prepared Loc-Bench questions, each asked as a session's prompt is (issue text and the harness's instruction, first 2,000 characters; words and names, since vectors are off for Python), the share of questions where a file of the answer is among the files named up by at least 5 points, and the share where anything is added not down by more than 2. No session is spent on this: `bench/ab/injected.py bench/data/benchc`.

| What rung 2 asks for and shows | Something added | A file of the answer among them | Files named when added |
|---|---|---|---|
| Five places, any kind (the run of 2026-10-13) | 258 of 400 | 126 (31.5%) | 4.45 |
| Five places, code only | 258 | 141 (35.3%) | 4.24 |
| Five files, any kind | 258 | 143 (35.8%) | 5.0 |
| **Five files, code only (now)** | 258 | **146 (36.5%)** | 5.0 |

Up by 20 questions, which is 5.0 points: the rule is met exactly at its edge, and that is all it is. On the 30 tasks of the run it is 16 instead of 14. The mod's tests were changed first and failed first; one real session on rung 2 showed the changed mod loading and adding its lines.

**What this does not show.** Whether a session then costs less. In the pass above the 14 pairs that were handed a file of the answer came out at 0.984 of the tokens, which with 14 pairs and differences of 20 to 40% between two runs of one task is a lean and not a finding. A better list is a condition for rung 2 to help, not evidence that it does, and the pass above is not read again for it.

**Three things the look afterwards raises, none decided.**
- *The standing instruction is not followed.* Rung 2 includes rung 1 (a section in the system prompt, and a pointer in the descriptions of Grep and Glob, both saying to call `search` first). The agent's first search was the engine's in 2 of 30 sessions; in the pilot it was 2 of 4. Rungs 0 and 1 rest on that instruction alone.
- *Rung 2 asks with a whole prompt, and on whole issue texts vectors pass their gate for Python* (+11.0 points on the 400, ADR-002), while the verdict that switched them off was read on first lines as "the closest to what an agent types". What an agent types was hardly seen (two queries in sixty sessions). Whether rung 2's own question should use vectors where the worker is installed is a question for the owner: it costs the embedding of every repository.
- *The tasks leave little room.* Without the plugin the agent named the right file in 30 of 30 tasks, in four tool calls at the median. A fifth fewer is one call of four, or two turns of five. Whether the gate or the tasks should change is the owner's to decide; both readings are in the deferred list.

### What was decided after the pass (2026-10-13)

The owner's instruction for the questions the pass left open: "for open questions ger help from advisor and agy to answer them". Six questions; the three voices are kept apart in the ledger of that day and summed up here.

1. **The gate stands and is not met**, in the scope of the run: rung 2, issue-to-file localisation, 30 pairs, sonnet. All three voices. Reading the gate on other tasks is another question with its own rule written first, not a second reading of this one.
2. **Nothing changes for the later milestones** (code graph, knowledge graph, compiler indexes): their tools stay behind `ALEPH_GRAPH=1`. All three voices. Nothing in the README claims a saving.
3. **No further paid session now.** All three voices, for different reasons: agy, because it is not understood why a handed answer saved nothing; the advisor, because no arm is designed (the default rung is open, the changed rung 2 has one session behind it, a filter for tasks with room does not exist). agy's aside, to force vectors on in such a run, is not adopted: the product as it ships was the fixed position, and the check under 4 tests the idea without a session. $32.90 of the stated limit is unspent.
4. **Vectors for rung 2's own question: measured without a session, nothing built.** The rule, written before the measurement: the same 400 questions and the same four variants as above, vectors on for that one query; a question whose index is not fully embedded is left out and counted. "A file of the answer among them" up by 5 points or more over the same variant with words alone is a reason to put vectors on in a next measured arm, and nothing more.
5. **One result a file among the engine's ten: measured without a session, nothing changed.** The rule, written before the measurement: Python's 400 and C++'s 108 by their first lines, each language in the arm it ships with (Python: words and names; C++: with vectors), the other arm beside it; at most one result a file, applied where the engine cuts its list to ten. It passes if file recall@10 rises by 5 points or more with a paired interval clear of zero in both languages' shipped arms and first place is not lower. A pass is a proposal that re-reads both vector verdicts, not a change.
6. **The default stays rung 1.** agy would make it rung 0: the standing instruction was followed in 2 of 30 sessions and its text rides on every turn. Not adopted, for the advisor's reason: there was no rung-0 arm, so 2 of 30 may be what the tools alone give; the text is about a thousand characters; and how often it is followed differs by model and prompt (two short sessions on the cheap model that day called the engine first, both). What is known about rung 1 is this and no more: **followed in 2 of 30 sonnet sessions on issue texts; no rung-0 control.** Rung 0 against rung 1 on that count alone is the cheapest arm to add to a later run.

**Result of 4 (vectors for rung 2's one query; 400 questions, none left out, no session):**

| What rung 2 asks for and shows | Words and names: added / answer among them | With vectors: added / answer among them |
|---|---|---|
| Five places, any kind | 258 / 126 (31.5%) | 367 / 181 (45.3%) |
| Five places, code only | 258 / 141 (35.3%) | 375 / 209 (52.3%) |
| Five files, any kind | 258 / 143 (35.8%) | 367 / 215 (53.8%) |
| Five files, code only (the mod now) | 258 / 146 (36.5%) | 375 / 214 (53.5%) |

Up by 17.0 points for the mod as it is now: by the rule, a reason to put vectors on in a next measured arm. Two things the table also says. The gain is in how often rung 2 speaks, not in how often it is right when it does: a third generator makes "two agree on the first hit" easier to meet, so locations are added for 375 questions instead of 258, and a file of the answer is among them in 57% of those either way. And so the sessions that are handed locations without the answer rise too, from 112 to 161; in the pass those were the pairs that cost more (1.18, 8 pairs, a lean). Whether the bar for speaking should rise when vectors take part is part of designing that arm.

**Result of 5 (at most one result a file among the ten; both benchmarks run again with the two arms beside the engine's own; no session).** The harness's own cut of the deeper list is what the engine returns for ten in every question (0 differences in 3,048 comparisons: 508 questions, three forms of each, two arms), and the engine's arms reproduce the recorded verdicts to the digit (Python +3.9, C++ +6.7).

| First lines, file recall@10 | As it is (files named) | One result a file (files named) | Gain (95% interval) | First place |
|---|---|---|---|---|
| **Python, words and names** (its shipped arm), 400 questions | 0.563 (7.3) | 0.597 (10.0) | +3.4 points (+1.8 to +5.2) | unchanged |
| Python, with vectors | 0.603 (6.8) | 0.643 (10.0) | +4.1 (+2.2 to +6.1) | unchanged |
| C++, words and names, 108 questions | 0.389 (6.6) | 0.445 (9.9) | +5.5 (+2.5 to +9.2) | unchanged |
| **C++, with vectors** (its shipped arm) | 0.456 (6.2) | 0.526 (9.8) | +7.0 (+3.6 to +11.0) | unchanged |

**By the rule it does not pass:** 5 points were asked of both languages' shipped arms, and Python's gains 3.4. So there is no proposal and the layout stays. What is recorded: the gain is real in every cell (no interval holds zero, on whole issue texts neither: +1.5 to +3.4 points), it costs nothing at first place, and it would flip neither vector verdict (under it Python's gate reads +4.6, interval +1.3 to +7.9, still under 5; C++'s +8.1, +2.2 to +14.6). What it would cost is not measured here: the second and third place inside a file, which an agent also uses. Rung 2's own lines now keep those places on the file's line; a layout for the engine's results that does the same is the form in which this would be worth asking again.

The design said rung 2 would be "dropped if flat or negative". It is flat within what 30 pairs can see. It was already off by default and stays an option: the run could not exclude a saving under about 30%, and what it hands over has changed since.

### A second arm on TypeScript: asked for free first, and not run (2026-10-13, evening)

The owner asked for the open items to be looked at with the advisor and agy (with boost) and worked on. One of them was a second paid arm where rung 2 should have most to offer: TypeScript, which ships with vectors since the same day. The advisor was for one arm of 60 sessions inside the limit; agy was against any run ("the wrong bottleneck": the agent does not follow the steering, and more arms measure that again). Before a session was paid for, the harness was made to ask what rung 2 would hand over on its 30 tasks, as the mod asks it and with the mod's 400 ms (`bench/ab/injected.py --tasks-only`; the embedding worker awake, which is kinder than the product: it sleeps after ten idle minutes).

| 30 tasks, the mod as it is (15 hits, code only, a line a file) | In time | Vectors used | Locations added | A file of the answer among them | Median |
|---|---|---|---|---|---|
| TypeScript set, scratch worktrees as the harness made them | 22 | 17 | 20 | 9 | about 300 ms |
| TypeScript set, worktrees as after a first git command | 27 | 18 | 24 | 11 | 119 ms |
| Python (pass 1's tasks) | 30 | none | 22 | 16 | 154 ms |

- **The first row was the harness's own doing.** Its scratch worktrees were in the state ADR-008 describes for a fresh checkout, which no worktree in use stays in: every question paid 320 to 620 ms of `git status`. The harness now runs one ordinary `git status` first, in both arms. The three tasks still late are babel's (ranking by path, 150 to 210 ms a question there).
- **A crash found on the way and fixed:** a search with a kind filter that ran out of time while narrowing answered with an error ("no such savepoint") instead of "out of time". Rung 2 had used that path since the morning's change to code only.
- **Not run, and why.** In time is repaired; what it hands over is not better than where pass 1 showed nothing: a file of the answer in 11 of 30 against Python's 16 of 30. And the set is not what its name says: 12 of the 30 tasks are in repositories Aleph calls JavaScript or Go (babel, material-ui, tailwindcss, preact; prometheus) and are answered with words alone, so the arm would be "TypeScript as it ships" for 18. *The advisor* after these figures: do not spend. *agy:* was against from the start. *Mine:* the same. $32.90 of the limit is unspent; one word from the owner runs the arm (`--set others --language typescript --warm-worker`).
- **What it costs if wrong:** an effect of rung 2 on TypeScript stays unseen.
- **Noted, not changed:** asking for code only costs time once a session on the largest repositories (63 to 80 ms where the scope is rebuilt, nothing on the next question), and the morning's rule for that change had no term for time. On Python the median went from 86 to 154 ms, inside the limit.

## Views consulted

- **Advisor:** the wrong event for adding a prompt section; the hung-transport hole in the "never blocks" rule; prove headless loading in one command before building rungs; fix the measures before any session runs; pilot with two sessions before eight; do not wait for the build's types.
- **Independent review of the branch (a fresh reviewer, most capable model):** no critical finding. Three important ones, all fixed: L3 said "it is gone" after a quiet grep that matched and after `find -delete`; L3 fired on searches aimed outside the repository through a leading `cd` and on multi-line command lists; the report kept only the last run of a task, which defeats the "thirty tasks twice" design. Smaller: patterns misread in several shapes, L2's chance used up by "hi", the pointer not separable from rung 1, file names in answers stripped wrongly. It confirmed the pilot's figures against the data and that the rung setting reaches the mod.
- **agy (no boost):** L2 on the first prompt only and by agreement of generators (adopted, except its score margin); L3 on empty results only, with the three misfires designed against (adopted); the power arithmetic and the "gave up early" trap (adopted into what the harness reports).
