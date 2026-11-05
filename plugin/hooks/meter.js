// The meter behind /aleph stats, as plain functions over a plain object, so that every count can be tested by itself
// (plugin/tests/meter.test.ts). register.js feeds it from Claude Code's events and keeps it in $.state.
//
// What it is for: whether Aleph is used, by the main conversation and by each kind of subagent, and what came of it.
// It says nothing about what a session would have cost without Aleph: only a measurement with and without it can
// (docs/adr/ADR-011, bench/ab/run.py), and the report says so beside the counts.
import { engineFeature, engineLocates, relativePath, searchKind } from './logic.js'

export const METER_VERSION = 2
const LATENCIES = 200     // the engine's answer times kept, the latest

export function usage() {
  return { requests: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
}

// One kind of loop: the main conversation, or one type of subagent (Explore, general-purpose, a plugin's agent).
export function loopUse() {
  return {
    runs: 0,                                   // main turns, or subagents of this type started
    tools: {},                                 // calls by tool name
    locate: { aleph: 0, grep: 0 },             // the engine's locating tools against locating greps
    filter: 0,                                 // other greps: of one file, of a command's output
    glob: 0,
    features: { retrieval: 0, architecture: 0, memory: 0, status: 0, other: 0 },
    first: { aleph: 0, grep: 0, neither: 0 },  // of the runs that ended: what located code first
    reads: { whole: 0, ranged: 0, onAleph: 0, onAdded: 0 },
    grepAfterAleph: 0,                         // a locating grep within two calls after the engine located something
  }
}

export function createMeter() {
  return {
    version: METER_VERSION,
    turns: 0, main: usage(), subagents: usage(), tools: {},
    // Kept as before for the A/B harness (bench/ab/run.py reads aleph, added and listed).
    aleph: 0, grep: 0, first: null,
    added: { prompt: 0, grep: 0, facts: 0, factsLate: 0, spawn: 0, spawnLocations: 0, firstGrep: 0 },
    listed: [],
    loops: {},
    engine: { asked: 0, answered: 0, timeouts: 0, late: 0, stale: 0, refused: 0, ms: [] },
    chat: { answers: 0, cited: 0, inserted: 0 },
    rungs: [],
    since: 0,
  }
}

// A meter kept by an older load of the mod (version 1, or from before a field existed), brought to this shape.
export function normalize(kept) {
  const fresh = createMeter()
  if (!kept || typeof kept !== 'object') return fresh
  const merged = { ...fresh, ...kept, version: METER_VERSION }
  merged.added = { ...fresh.added, ...(kept.added || {}) }
  merged.engine = { ...fresh.engine, ...(kept.engine || {}) }
  merged.chat = { ...fresh.chat, ...(kept.chat || {}) }
  merged.loops = {}
  for (const [name, loop] of Object.entries(kept.loops || {})) {
    const base = loopUse()
    merged.loops[name] = {
      ...base, ...loop,
      locate: { ...base.locate, ...(loop.locate || {}) },
      features: { ...base.features, ...(loop.features || {}) },
      first: { ...base.first, ...(loop.first || {}) },
      reads: { ...base.reads, ...(loop.reads || {}) },
    }
  }
  if (kept.refused && !merged.engine.refused) merged.engine.refused = kept.refused
  if (kept.added && kept.added.factsLate && !kept.engine) merged.engine.late = kept.added.factsLate
  if (!Array.isArray(merged.rungs)) merged.rungs = []
  return merged
}

export function loopOf(meter, name) {
  if (!meter.loops[name]) meter.loops[name] = loopUse()
  return meter.loops[name]
}

export function addUsage(into, counted) {
  if (!counted) return
  into.requests += 1
  into.input += counted.input_tokens || 0
  into.output += counted.output_tokens || 0
  into.cacheRead += counted.cache_read_input_tokens || 0
  into.cacheWrite += counted.cache_creation_input_tokens || 0
}

export function total(counted) {
  return counted.input + counted.output + counted.cacheRead + counted.cacheWrite
}

export function noteRung(meter, rung) {
  if (meter.rungs[meter.rungs.length - 1] !== rung) meter.rungs.push(rung)
}

// ---- a run: one main turn or one subagent, from its start to its end --------------------------

// What a run has seen so far: which kind of loop it is, what located code first, the files the engine named to it
// (answers and what the mod added), and how many calls ago the engine last located something.
export function newRun(loop) {
  return { loop, first: null, aleph: new Set(), added: new Set(), sinceAleph: Infinity, assisted: false, counted: false }
}

// One tool call of a run, after it ran. `answer` is the call's result (for an engine call, the files it names are read
// from it). `cwd` is the session's directory, against which Read's and Grep's paths are read.
export function recordCall(meter, run, call, answer, pathsOf, cwd) {
  const loop = loopOf(meter, run.loop)
  const tool = String(call.tool || '')
  meter.tools[tool] = (meter.tools[tool] || 0) + 1
  loop.tools[tool] = (loop.tools[tool] || 0) + 1
  run.sinceAleph += 1

  const feature = engineFeature(tool)
  if (feature) {
    loop.features[feature] += 1
    if (/__(search|symbol|outline|status)$/.test(tool)) meter.aleph += 1     // as the meter has always counted it
    if (engineLocates(tool)) {
      loop.locate.aleph += 1
      if (run.first === null) run.first = 'aleph'
      if (meter.first === null) meter.first = 'aleph'
      run.sinceAleph = 0
    }
    if (feature === 'retrieval' || feature === 'architecture') for (const path of pathsOf(answer)) run.aleph.add(path)
    return
  }

  const kind = searchKind(tool, call, cwd)
  if (kind === 'locate') {
    loop.locate.grep += 1
    meter.grep += 1
    if (run.first === null) run.first = 'grep'
    if (meter.first === null) meter.first = 'grep'
    if (run.sinceAleph <= 2) loop.grepAfterAleph += 1
  } else if (kind === 'filter') {
    loop.filter += 1
  } else if (kind === 'glob') {
    loop.glob += 1
  }

  if (tool === 'Read') {
    const ranged = call.offset !== undefined || call.limit !== undefined
    loop.reads[ranged ? 'ranged' : 'whole'] += 1
    const path = relativePath(call.file_path, cwd)
    if (run.aleph.has(path)) loop.reads.onAleph += 1
    if (run.added.has(path)) loop.reads.onAdded += 1
  }
}

// What the mod added to a run (a subagent's task, a prompt, a grep's note): the files it named.
export function recordAdded(run, paths) {
  for (const path of paths) run.added.add(path)
}

// The end of a run: what located code first is counted once.
export function endRun(meter, run) {
  if (run.counted) return
  run.counted = true
  const loop = loopOf(meter, run.loop)
  loop.first[run.first || 'neither'] += 1
}

// One question the mod asked the engine for itself: how long it took and how it ended.
export function recordAsk(meter, ms, outcome) {
  const engine = meter.engine
  engine.asked += 1
  if (outcome === 'timeout') engine.timeouts += 1
  else if (outcome === 'refused') engine.refused += 1
  else {
    engine.answered += 1
    engine.ms.push(Math.round(ms))
    if (engine.ms.length > LATENCIES) engine.ms.splice(0, engine.ms.length - LATENCIES)
    if (outcome === 'late') engine.late += 1
    if (outcome === 'stale') engine.stale += 1
  }
}

// Two meters added up (a conversation into the session's total).
export function combine(into, from) {
  const a = normalize(into)
  const b = normalize(from)
  const sum = (x, y) => {
    for (const key of Object.keys(y)) {
      if (typeof y[key] === 'number') x[key] = (x[key] || 0) + y[key]
      else if (y[key] && typeof y[key] === 'object' && !Array.isArray(y[key])) sum(x[key] || (x[key] = {}), y[key])
    }
    return x
  }
  for (const key of ['turns', 'aleph', 'grep']) a[key] += b[key]
  for (const key of ['main', 'subagents', 'tools', 'added', 'chat']) sum(a[key], b[key])
  for (const [name, loop] of Object.entries(b.loops)) sum(loopOf(a, name), loop)
  for (const key of Object.keys(b.engine)) if (key !== 'ms') a.engine[key] += b.engine[key]
  a.engine.ms = [...a.engine.ms, ...b.engine.ms].slice(-LATENCIES)
  for (const rung of b.rungs) noteRung(a, rung)
  a.first = a.first || b.first
  return a
}

// ---- reading it ---------------------------------------------------------------------------------

function percentile(values, share) {
  if (!values.length) return null
  const sorted = [...values].sort((x, y) => x - y)
  return sorted[Math.min(sorted.length - 1, Math.floor(share * sorted.length))]
}

function rate(part, whole) {
  return whole ? Math.round((100 * part) / whole) + '%' : 'n/a'
}

// The figures the summary and the report are made of: main and subagents apart, and all loops together.
export function figures(meter) {
  const loops = Object.entries(meter.loops)
  const pick = (names) => {
    const sum = loopUse()
    for (const [name, loop] of loops) {
      if (!names(name)) continue
      sum.runs += loop.runs
      for (const key of ['filter', 'glob', 'grepAfterAleph']) sum[key] += loop[key]
      for (const group of ['locate', 'features', 'first', 'reads']) for (const key of Object.keys(sum[group])) sum[group][key] += loop[group][key] || 0
    }
    return sum
  }
  const main = pick((name) => name === 'main')
  const subagents = pick((name) => name !== 'main')
  const all = pick(() => true)
  return { main, subagents, all }
}

function adoption(loop) {
  return rate(loop.locate.aleph, loop.locate.aleph + loop.locate.grep)
}

function firstRate(loop) {
  return rate(loop.first.aleph, loop.first.aleph + loop.first.grep)
}

// The short screen of /aleph stats.
export function summary(rung, meter) {
  const { main, subagents, all } = figures(meter)
  const engine = meter.engine
  const reads = all.reads.whole + all.reads.ranged
  const p50 = percentile(engine.ms, 0.5)
  const p95 = percentile(engine.ms, 0.95)
  const rungs = meter.rungs.length > 1 ? meter.rungs.join(' → ') : String(rung)
  const lines = [
    'aleph-zero · rung ' + rungs + ' · ' + meter.turns + ' turns · ' + subagents.runs + ' subagent runs',
    'locating code with Aleph instead of grep: main ' + adoption(main) + ' (' + main.locate.aleph + ' of ' + (main.locate.aleph + main.locate.grep)
      + ') · subagents ' + adoption(subagents) + ' (' + subagents.locate.aleph + ' of ' + (subagents.locate.aleph + subagents.locate.grep) + ')',
    'Aleph located first: main ' + firstRate(main) + ' of turns that located code · subagents ' + firstRate(subagents) + ' of subagents that did',
    'after Aleph: ' + rate(all.reads.onAleph, reads) + ' of reads were of a file it named · a grep within two calls in '
      + all.grepAfterAleph + ' cases · reads by range ' + rate(all.reads.ranged, reads),
    'added by the mod: to ' + meter.added.spawn + ' subagent tasks (' + meter.added.spawnLocations + ' with locations), '
      + meter.added.prompt + ' prompts, ' + meter.added.grep + ' empty and ' + meter.added.firstGrep + ' first greps, facts '
      + meter.added.facts + ' times; ' + rate(all.reads.onAdded, reads) + ' of reads were of a file it added',
    'by feature: retrieval ' + all.features.retrieval + ' · architecture ' + all.features.architecture + ' · memory '
      + all.features.memory + ' · side chat ' + meter.chat.answers + ' answers (' + meter.chat.cited + ' cited, ' + meter.chat.inserted + ' inserted)',
    'engine, asked by the mod: ' + engine.asked + ' · answered ' + engine.answered + (p50 === null ? '' : ' (p50 ' + p50 + ' ms, p95 ' + p95 + ' ms)')
      + ' · timed out ' + engine.timeouts + ' · late ' + engine.late + ' · from an index catching up ' + engine.stale
      + (engine.refused ? ' · refused ' + engine.refused + ': not allowed (/aleph check)' : ''),
    'tokens: main ' + total(meter.main) + ' in ' + meter.main.requests + ' requests · subagents ' + total(meter.subagents) + ' in '
      + meter.subagents.requests + ' · these are what was used, not what Aleph saved: `/aleph stats report` says why',
  ]
  return lines.join('\n')
}

// What a with-and-without measurement of Aleph showed, quoted as the ADR records it. A new run's result is added here
// when it is recorded there; until then the report says "not shown" and no saving is claimed.
export const MEASURED = [
  { date: '2026-10-13', what: 'rung 2 (locations with the first prompt) against no plugin', pairs: 30,
    tokens: [1.02, 0.915, 1.142], tools: [0.948, 0.824, 1.089], note: 'right file named 29 of 30 against 30 of 30' },
  { date: '2026-10-16', what: 'the knowledge graph handed over (facts) against the graph off', pairs: 30,
    tokens: [0.991, 0.911, 1.082], tools: [0.973, 0.891, 1.066], note: 'first file read right 26 of 30 against 24' },
]

function reading(interval) {
  const [, low, high] = interval
  if (high < 1) return 'fewer, shown'
  if (low > 1) return 'more, shown'
  return 'not shown either way'
}

function table(rows) {
  const line = (cells) => '| ' + cells.join(' | ') + ' |'
  return [line(rows[0]), line(rows[0].map(() => '---')), ...rows.slice(1).map(line)].join('\n')
}

// The report /aleph stats report writes: the meter in full, read with what it can and cannot show.
export function reportMarkdown(rung, meter, about = {}) {
  const { main, subagents, all } = figures(meter)
  const engine = meter.engine
  const reads = (loop) => loop.reads.whole + loop.reads.ranged
  const loops = Object.entries(meter.loops).sort(([a], [b]) => (a === 'main' ? -1 : b === 'main' ? 1 : a.localeCompare(b)))
  const out = []
  out.push('# Aleph Zero: what this session used')
  out.push('')
  out.push('- Repository: ' + (about.repository || 'n/a') + '  ')
  out.push('- Written: ' + (about.written || 'n/a') + ' · plugin ' + (about.version || 'n/a') + ' · rung '
    + (meter.rungs.length > 1 ? meter.rungs.join(' → ') : rung) + '  ')
  out.push('- ' + meter.turns + ' main turns, ' + subagents.runs + ' subagent runs')
  out.push('')
  out.push('## What this shows, and what it does not')
  out.push('')
  out.push('This report counts what happened in one session: how often the agents located code with Aleph rather than grep, '
    + 'whether what Aleph named was then read, and how the engine answered. It is evidence of **use**. It cannot say what the '
    + 'same work would have cost without Aleph, because no session is run twice. That needs a measurement with and without '
    + 'it on the same tasks, which is in the last section. No "tokens saved" figure is given, because any such figure from one session '
    + 'would be an estimate.')
  out.push('')
  out.push('## Locating code: Aleph or grep')
  out.push('')
  const loopRows = [['Loop', 'Runs', 'Aleph located', 'Locating greps', 'Adoption', 'Aleph first', 'Other greps', 'Glob']]
  for (const [name, loop] of loops) {
    loopRows.push([name === 'main' ? 'main conversation' : name, loop.runs, loop.locate.aleph, loop.locate.grep, adoption(loop),
      firstRate(loop), loop.filter, loop.glob])
  }
  loopRows.push(['**all**', all.runs, all.locate.aleph, all.locate.grep, adoption(all), firstRate(all), all.filter, all.glob])
  out.push(table(loopRows))
  out.push('')
  out.push('- *Adoption*: Aleph\'s locating calls (search, symbol, facts, references) among all locating calls, where a locating '
    + 'grep is a recursive grep, rg, git grep, `find -name` or a Grep tool search of the repository (also when only shortened by `| head`).')
  out.push('- *Aleph first*: of the turns and subagents that located code at all, the share that started with Aleph.')
  out.push('')
  out.push('## What came of it')
  out.push('')
  out.push(table([
    ['', 'Main', 'Subagents'],
    ['Reads of a file Aleph named', rate(main.reads.onAleph, reads(main)), rate(subagents.reads.onAleph, reads(subagents))],
    ['Reads of a file the mod added', rate(main.reads.onAdded, reads(main)), rate(subagents.reads.onAdded, reads(subagents))],
    ['Reads by line range', rate(main.reads.ranged, reads(main)), rate(subagents.reads.ranged, reads(subagents))],
    ['Grep within two calls after Aleph', main.grepAfterAleph, subagents.grepAfterAleph],
  ]))
  out.push('')
  out.push('A grep right after Aleph located something suggests that Aleph\'s answer was not enough for that question.')
  out.push('')
  out.push('## What the mod added')
  out.push('')
  out.push(table([
    ['Where', 'Times'],
    ['Subagent tasks (the note)', meter.added.spawn],
    ['Subagent tasks (with ranked locations)', meter.added.spawnLocations],
    ['First prompt of a conversation (rung 2)', meter.added.prompt],
    ['A grep that found nothing (rung 3)', meter.added.grep],
    ['The first grep of a turn or subagent (rung 3, grep_assist first)', meter.added.firstGrep],
    ['Routes and environment names looked up (facts)', meter.added.facts],
  ]))
  out.push('')
  out.push('## By feature')
  out.push('')
  out.push(table([
    ['Feature', 'Calls by the agents', 'What it is'],
    ['Retrieval', all.features.retrieval, 'search, symbol, outline: where code is, by meaning and by name'],
    ['Architecture', all.features.architecture, 'facts, references, decisions: routes, environment names, callers, ADRs'],
    ['Memory', all.features.memory, 'recall, remember: notes kept between sessions'],
    ['Side chat', meter.chat.answers, 'questions answered beside the conversation (`/aleph`); ' + meter.chat.cited + ' cited code, ' + meter.chat.inserted + ' inserted'],
  ]))
  out.push('')
  out.push('## The engine')
  out.push('')
  const p50 = percentile(engine.ms, 0.5)
  out.push('The mod asked the engine ' + engine.asked + ' times by itself: ' + engine.answered + ' answered'
    + (p50 === null ? '' : ' (median ' + p50 + ' ms, 95th percentile ' + percentile(engine.ms, 0.95) + ' ms)')
    + ', ' + engine.timeouts + ' not in time, ' + engine.late + ' late, ' + engine.stale + ' from an index still catching up with edits, '
    + engine.refused + ' refused.')
  out.push('')
  out.push('## Tokens')
  out.push('')
  out.push(table([
    ['', 'Requests', 'Input', 'Output', 'Cache read', 'Cache write', 'Total'],
    ['Main', meter.main.requests, meter.main.input, meter.main.output, meter.main.cacheRead, meter.main.cacheWrite, total(meter.main)],
    ['Subagents', meter.subagents.requests, meter.subagents.input, meter.subagents.output, meter.subagents.cacheRead,
      meter.subagents.cacheWrite, total(meter.subagents)],
  ]))
  out.push('')
  out.push('## With and without Aleph: the measured effect')
  out.push('')
  out.push('Paired runs of the same tasks, each once with and once without, read as the ratio with/without (below 1 is fewer), '
    + 'geometric mean with a 95% bootstrap interval. An interval that holds 1 is "not shown either way".')
  out.push('')
  const measured = [['Date', 'Compared', 'Pairs', 'Tokens', 'Tool calls', 'Note']]
  for (const run of MEASURED) {
    measured.push([run.date, run.what, run.pairs,
      run.tokens[0] + ' (' + run.tokens[1] + ' to ' + run.tokens[2] + '): ' + reading(run.tokens),
      run.tools[0] + ' (' + run.tools[1] + ' to ' + run.tools[2] + '): ' + reading(run.tools), run.note])
  }
  out.push(table(measured))
  out.push('')
  out.push('None of these runs measured subagents being steered'
    + (meter.subagents.requests > meter.main.requests ? ', which is where most of this session\'s requests were made.' : '.'))
  out.push('')
  return out.join('\n')
}
