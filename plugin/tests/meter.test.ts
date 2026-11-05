import { expect, test } from 'claude-code/testing'
import {
  engineFeature, engineLocates, pathsIn, pipeParts, relativePath, rungInInstructions, rungInSettings, searchKind, spawnBlock, spawnTakesNote,
} from '../hooks/logic.js'
import {
  MEASURED, combine, createMeter, endRun, figures, newRun, normalize, noteRung, recordAdded, recordAsk, recordCall, reportMarkdown, summary,
} from '../hooks/meter.js'

const T = (name: string) => 'mcp__plugin_aleph-zero_aleph__' + name
const CWD = '/repo'
const text = (body: string) => ({ content: [{ type: 'text', text: body }] })

test('the engine\'s tools by feature, and which of them locate code', async () => {
  expect(engineFeature(T('search'))).toBe('retrieval')
  expect(engineFeature(T('outline'))).toBe('retrieval')
  expect(engineFeature(T('facts'))).toBe('architecture')
  expect(engineFeature(T('decisions'))).toBe('architecture')
  expect(engineFeature(T('recall'))).toBe('memory')
  expect(engineFeature(T('remember'))).toBe('memory')
  expect(engineFeature(T('status'))).toBe('status')
  expect(engineFeature('Grep')).toBe(null)
  expect(engineLocates(T('search'))).toBe(true)
  expect(engineLocates(T('references'))).toBe(true)
  expect(engineLocates(T('outline'))).toBe(false)
  expect(engineLocates(T('recall'))).toBe(false)
})

test('a locating grep is told from a grep of one file, of a command\'s output, and from Glob', async () => {
  const bash = (command: string) => searchKind('Bash', { command }, CWD)
  expect(bash('grep -rn "refreshToken" .')).toBe('locate')
  expect(bash('rg refreshToken src')).toBe('locate')
  expect(bash('rg -n refreshToken | head -20')).toBe('locate')            // only shortened
  expect(bash('grep -rn TODO . | sort | uniq -c')).toBe('locate')
  expect(bash('git log --oneline | grep fix')).toBe('filter')             // a command's output
  expect(bash('grep -n foo src/a.py')).toBe('filter')                      // one file
  expect(bash('rg foo | xargs sed -i s/a/b/')).toBe('filter')
  expect(bash('ls -la')).toBe(null)
  expect(searchKind('Grep', { pattern: 'refreshToken' }, CWD)).toBe('locate')
  expect(searchKind('Grep', { pattern: 'refreshToken', path: '/repo/src/a.py' }, CWD)).toBe('filter')
  expect(searchKind('Glob', { pattern: '**/*.ts' }, CWD)).toBe('glob')
  expect(searchKind('Read', { file_path: 'a' }, CWD)).toBe(null)
  expect(pipeParts('grep "a|b" -r . | head')).toEqual(['grep "a|b" -r . ', ' head'])
  expect(pipeParts('make || grep x -r .')).toEqual(['make || grep x -r .'])
})

test('the files an engine answer names, as JSON or as text', async () => {
  expect(pathsIn(text(JSON.stringify({ hits: [{ path: 'a.py' }, { path: 'b.py' }, { path: 'a.py' }] })))).toEqual(['a.py', 'b.py'])
  expect(pathsIn(text(JSON.stringify({ symbols: [{ path: 'c.go' }] })))).toEqual(['c.go'])
  expect(pathsIn(text('src/auth/token.py:4-6  function  refresh\n  def refresh()\nlib/x.ts:9  class  X'))).toEqual(['src/auth/token.py', 'lib/x.ts'])
  expect(pathsIn(null)).toEqual([])
  // As the engine prints them for an agent (text, its default): facts and references name the place mid-line, outline in its head.
  expect(pathsIn(text('ALEPH_AEON_LIB  src/aleph_zero/memory.py:289  read\nnote: more match than are listed'))).toEqual(['src/aleph_zero/memory.py'])
  expect(pathsIn(text('plugin/hooks/meter.js  javascript  389 lines\n  9  constant  export const X = 2'))).toEqual(['plugin/hooks/meter.js'])
  expect(pathsIn(text('plugin/hooks/meter.js:102  function  recordCall\ncallers: 1 certain\n  plugin/hooks/register.js:1065  in register\n'
    + 'callees: 1\n  plugin/hooks/meter.js:105  loopOf()  ->  plugin/hooks/logic.js:72  loopOf'))).toEqual(
    ['plugin/hooks/meter.js', 'plugin/hooks/register.js', 'plugin/hooks/logic.js'])
  expect(relativePath('/repo/src/a.py', CWD)).toBe('src/a.py')
  expect(relativePath('./src/a.py', CWD)).toBe('src/a.py')
})

test('a subagent\'s task gets the note after the brief, with locations when there are some; forks, resumes and workflows none', async () => {
  const block = spawnBlock([{ path: 'src/a.py', start_line: 1, end_line: 5, kind: 'function', symbol: 'f' }])
  expect(block.startsWith('\n\n[aleph-zero]')).toBe(true)
  expect(block).toContain('src/a.py:1-5  function  f')
  expect(spawnBlock()).not.toContain('ranks these locations')
  expect(spawnBlock().length).toBeLessThan(400)                             // about 80 tokens
  expect(spawnTakesNote({ prompt: 'find where tokens are refreshed' })).toBe(true)
  expect(spawnTakesNote({ prompt: 'find where tokens are refreshed', fork: true })).toBe(false)
  expect(spawnTakesNote({ prompt: 'go on', resume: { agentId: 'a' } })).toBe(false)
  expect(spawnTakesNote({ prompt: 'find where tokens are refreshed', workflow: { runId: 'wf_1', agentIndex: 1 } })).toBe(false)
  expect(spawnTakesNote({ prompt: 'hi' })).toBe(false)
})

test('the rung a CLAUDE.md names', async () => {
  expect(rungInInstructions('Aleph runs at rung 3 with the graph on')).toBe(3)
  expect(rungInInstructions('aleph rung: 2')).toBe(2)
  expect(rungInInstructions('no word of it')).toBe(null)
  expect(rungInSettings(JSON.stringify({ pluginConfigs: { 'aleph-zero@aleph-zero': { options: { rung: '3', graph: 'on' } } } }))).toBe(3)
  expect(rungInSettings(JSON.stringify({ pluginConfigs: { 'other@x': { options: { rung: '3' } } } }))).toBe(null)
  expect(rungInSettings('not json')).toBe(null)
})

test('the meter counts each loop apart, and what located code first in each run', async () => {
  const meter = createMeter()
  const main = newRun('main')
  recordCall(meter, main, { tool: 'Grep', pattern: 'refreshToken' }, null, pathsIn, CWD)
  recordCall(meter, main, { tool: T('search'), query: 'token refresh' }, text(JSON.stringify({ hits: [{ path: 'src/a.py' }] })), pathsIn, CWD)
  recordCall(meter, main, { tool: 'Read', file_path: '/repo/src/a.py', offset: 10, limit: 40 }, null, pathsIn, CWD)
  recordCall(meter, main, { tool: 'Bash', command: 'rg refresh' }, null, pathsIn, CWD)     // a grep right after Aleph
  endRun(meter, main)
  endRun(meter, main)                                                       // counted once
  const sub = newRun('Explore')
  recordAdded(sub, ['src/b.py'])
  recordCall(meter, sub, { tool: T('symbol'), name: 'X' }, text(JSON.stringify({ symbols: [{ path: 'src/c.py' }] })), pathsIn, CWD)
  recordCall(meter, sub, { tool: 'Read', file_path: 'src/b.py' }, null, pathsIn, CWD)
  recordCall(meter, sub, { tool: 'Read', file_path: 'src/c.py' }, null, pathsIn, CWD)
  recordCall(meter, sub, { tool: 'Glob', pattern: '*.py' }, null, pathsIn, CWD)
  endRun(meter, sub)
  const quiet = newRun('Explore')
  recordCall(meter, quiet, { tool: 'Edit', file_path: 'x' }, null, pathsIn, CWD)
  endRun(meter, quiet)

  expect(meter.loops.main.locate).toEqual({ aleph: 1, grep: 2 })
  expect(meter.loops.main.first).toEqual({ aleph: 0, grep: 1, neither: 0 })
  expect(meter.loops.main.grepAfterAleph).toBe(1)
  expect(meter.loops.main.reads).toEqual({ whole: 0, ranged: 1, onAleph: 1, onAdded: 0 })
  expect(meter.loops.Explore.first).toEqual({ aleph: 1, grep: 0, neither: 1 })
  expect(meter.loops.Explore.reads).toEqual({ whole: 2, ranged: 0, onAleph: 1, onAdded: 1 })
  expect(meter.loops.Explore.glob).toBe(1)
  expect(meter.loops.Explore.features.retrieval).toBe(1)
  expect(meter.aleph).toBe(2)                                               // the harness's count, as before
  expect(meter.grep).toBe(2)
  const { main: m, subagents: s } = figures(meter)
  expect(m.locate.aleph + s.locate.aleph).toBe(2)
})

test('the engine\'s answers to the mod: times, timeouts, late and stale answers, refusals', async () => {
  const meter = createMeter()
  for (const ms of [10, 20, 30, 400]) recordAsk(meter, ms, 'answered')
  recordAsk(meter, 450, 'timeout')
  recordAsk(meter, 5, 'refused')
  recordAsk(meter, 120, 'stale')
  recordAsk(meter, 390, 'late')
  expect(meter.engine).toMatchObject({ asked: 8, answered: 6, timeouts: 1, refused: 1, stale: 1, late: 1 })
  expect(summary(1, meter)).toContain('p50 120 ms')
})

test('a meter kept by an older mod is read, and two meters add up', async () => {
  const old = { turns: 3, main: { requests: 2, input: 1, output: 1, cacheRead: 1, cacheWrite: 1 }, subagents: { requests: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    tools: { Grep: 4 }, aleph: 1, grep: 4, first: 'grep', added: { prompt: 0, grep: 0, facts: 1, factsLate: 110 }, listed: [], refused: 2 }
  const read = normalize(old)
  expect(read.engine.refused).toBe(2)
  expect(read.engine.late).toBe(110)
  expect(read.added.spawn).toBe(0)
  const meter = createMeter()
  const run = newRun('main')
  recordCall(meter, run, { tool: 'Grep', pattern: 'abcdef' }, null, pathsIn, CWD)
  noteRung(meter, 1)
  noteRung(meter, 1)
  noteRung(meter, 3)
  const both = combine(read, meter)
  expect(both.turns).toBe(3)
  expect(both.tools.Grep).toBe(5)
  expect(both.loops.main.locate.grep).toBe(1)
  expect(both.rungs).toEqual([1, 3])
})

test('the summary and the report say what was used and claim no saving the measurements did not show', async () => {
  const meter = createMeter()
  noteRung(meter, 1)
  noteRung(meter, 3)
  const main = newRun('main')
  recordCall(meter, main, { tool: T('search') }, null, pathsIn, CWD)
  endRun(meter, main)
  meter.subagents.requests = 10
  const short = summary(3, meter)
  expect(short).toContain('rung 1 → 3')
  expect(short).toContain('main 100% (1 of 1)')
  expect(short).toContain('not what Aleph saved')
  expect(short.split('\n').length).toBeLessThanOrEqual(8)
  const report = reportMarkdown(3, meter, { repository: '/repo', written: '2026-11-05', version: '0.1.21' })
  expect(report).toContain('evidence of **use**')
  expect(report).toContain('| main conversation |')
  expect(report).toContain('not shown either way')
  expect(report).toContain('where most of this session\'s requests were made')
  expect(report).not.toMatch(/tokens saved:/i)
  for (const run of MEASURED) expect(report).toContain(run.date)
})
