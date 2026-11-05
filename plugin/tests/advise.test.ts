import { expect, mock, test } from 'claude-code/testing'
import { adviceText, agentNote, dismissalKey, open, suggest } from '../hooks/advise.js'
import { createMeter, endRun, newRun, recordCall } from '../hooks/meter.js'
import { pathsIn } from '../hooks/logic.js'

const T = (name: string) => 'mcp__plugin_aleph-zero_aleph__' + name
const CWD = '/repo'
const text = (body: string) => ({ content: [{ type: 'text', text: body }] })
const emptyGrep = { ref: 2, result: { mode: 'files_with_matches', filenames: [], numFiles: 0, totalFiles: 0 }, text: 'No files found' }
const foundGrep = { ref: 2, result: { mode: 'files_with_matches', filenames: ['a.py'], numFiles: 1, totalFiles: 1 }, text: 'a.py' }
const STATUS = {
  root: '/repo', files_in_view: 4151, untracked_cache: true, main_language: 'cpp',
  vectors: { enabled: false, chosen: false, chunks: 37883, worker_installed: true, worker_current: true,
    verdict: { helps: true, why: 'the benchmark for cpp says vectors help (+6.7 points)' } },
  memory: { aeon_installed: false, aeon_current: false },
}

// A session: `subs` subagents each grepping `greps` times (empty ones as asked), and the main loop searching with Aleph.
function session({ subs = 0, greps = 4, empty = 0, alephs = 0, missed = 0 } = {}) {
  const meter = createMeter()
  for (let index = 0; index < subs; index += 1) {
    const run = newRun('Explore')
    for (let call = 0; call < greps; call += 1) {
      recordCall(meter, run, { tool: 'Grep', pattern: 'thing' + call }, call < empty / Math.max(subs, 1) ? emptyGrep : foundGrep, pathsIn, CWD)
    }
    endRun(meter, run)
    meter.loops.Explore.runs += 1
  }
  const main = newRun('main')
  for (let call = 0; call < alephs; call += 1) {
    recordCall(meter, main, { tool: T('search'), query: 'q' }, text('{"hits": []}'), pathsIn, CWD)
    if (call < missed) recordCall(meter, main, { tool: 'Bash', command: 'rg thing' + call }, null, pathsIn, CWD)
  }
  endRun(meter, main)
  return meter
}

test('small samples suggest nothing', async () => {
  expect(suggest(createMeter(), STATUS, {}, 1)).toEqual([])
  expect(suggest(session({ subs: 3, greps: 4 }), STATUS, {}, 1)).toEqual([])          // 12 locating calls, 3 subagents
})

test('subagents that grep: rung 2, or the subagents option first when it is off', async () => {
  const meter = session({ subs: 6, greps: 5 })
  const [first] = suggest(meter, STATUS, {}, 1)
  expect(first.id).toBe('rung-2')
  expect(first.action).toEqual({ kind: 'option', key: 'rung', value: '2' })
  expect(first.why).toContain('6 subagents located code with Aleph in 0% of 30 locating calls')
  expect(first.evidence).toContain('not measured')
  expect(suggest(meter, STATUS, { subagents: 'off' }, 1)[0].action).toEqual({ kind: 'option', key: 'subagents', value: 'on' })
  expect(suggest(meter, STATUS, {}, 2).map((item) => item.id)).not.toContain('rung-2')
})

test('greps that found nothing, counted at every rung, suggest rung 3', async () => {
  const meter = session({ subs: 2, greps: 10, empty: 10 })
  expect(meter.loops.Explore.emptyGrep).toBe(10)
  const ids = suggest(meter, STATUS, {}, 1).map((item) => item.id)
  expect(ids).toContain('rung-3')
  expect(suggest(meter, STATUS, {}, 3).map((item) => item.id)).not.toContain('rung-3')
})

test('search by meaning: the worker where it is missing; vectors turned off again only on evidence, and said so', async () => {
  const noWorker = { ...STATUS, vectors: { ...STATUS.vectors, worker_installed: false } }
  const worker = suggest(createMeter(), noWorker, {}, 1)[0]
  expect(worker.action).toEqual({ kind: 'machine', command: 'aleph-zero setup --worker' })
  expect(worker.evidence).toContain('measured')
  expect(suggest(session({ alephs: 20, missed: 2 }), STATUS, {}, 1)).toEqual([])        // Aleph's answers were enough
  const vectors = suggest(session({ alephs: 20, missed: 8 }), STATUS, {}, 1)[0]
  expect(vectors.id).toBe('vectors')
  expect(vectors.action).toEqual({ kind: 'engine', args: ['setup', '--dense'] })
  expect(vectors.why).toContain('8 of 20 cases (40%)')
  expect(vectors.why).toContain('you turned it off before')
  expect(suggest(session({ alephs: 20, missed: 8 }), { ...STATUS, vectors: { ...STATUS.vectors, verdict: { helps: false } } }, {}, 1)).toEqual([])
})

test('the untracked cache where the engine is slow on a large worktree; builds that are out of date; memory where it is used', async () => {
  const meter = createMeter()
  meter.engine = { ...meter.engine, asked: 12, answered: 12, stale: 0, ms: Array(12).fill(80) }
  const slow = suggest(meter, { ...STATUS, untracked_cache: false }, {}, 1)
  expect(slow[0].action).toEqual({ kind: 'engine', args: ['setup', '--untracked-cache'] })
  expect(suggest(meter, { ...STATUS, untracked_cache: false, files_in_view: 500 }, {}, 1)).toEqual([])
  const old = suggest(createMeter(), { ...STATUS, vectors: { ...STATUS.vectors, worker_current: false } }, {}, 1)
  expect(old[0].action).toEqual({ kind: 'machine', command: 'aleph-zero update' })
  const used = createMeter()
  used.chat.answers = 5
  expect(suggest(used, STATUS, {}, 1)[0].id).toBe('memory')
})

test('a dismissed suggestion stays dismissed: a repository\'s by repository, the plugin\'s and the machine\'s everywhere', async () => {
  const meter = session({ subs: 6, greps: 5 })
  meter.engine = { ...meter.engine, asked: 12, answered: 12, stale: 0, ms: Array(12).fill(80) }
  const listed = suggest(meter, { ...STATUS, untracked_cache: false, vectors: { ...STATUS.vectors, worker_installed: false } }, {}, 1)
  expect(listed.map((item) => item.id)).toEqual(['rung-2', 'worker', 'untracked-cache'])
  const kept = listed.map((item) => dismissalKey(item, '/repo'))
  expect(kept).toEqual(['rung-2', 'worker', 'untracked-cache@/repo'])
  expect(open(listed, kept, '/repo')).toEqual([])
  expect(open(listed, kept, '/other').map((item) => item.id)).toEqual(['untracked-cache'])
})

test('at most four are listed, counted after the dismissed ones are left out', async () => {
  const many = [1, 2, 3, 4, 5].map((n) => ({ id: 's' + n, scope: 'machine' }))
  expect(open(many as any, [], '').length).toBe(4)
  expect(open(many as any, ['s1'], '').map((item: any) => item.id)).toEqual(['s2', 's3', 's4', 's5'])
})

test('what the person and the agent are told', async () => {
  const listed = suggest(session({ subs: 6, greps: 5 }), STATUS, {}, 1)
  const shown = adviceText(listed)
  expect(shown).toContain('1. Give subagents ranked locations')
  expect(shown).toContain('does: set the plugin option rung to 2')
  expect(shown).toContain('/aleph advise apply N')
  expect(adviceText([])).toContain('no suggestion')
  const note = agentNote(2)
  expect(note).toContain('/aleph advise')
  expect(note).toContain('Do not run any command')
  expect(note).not.toMatch(/aleph-zero setup|rung/)                     // never a command or a setting
})

// ---- through the mod -------------------------------------------------------------------------------

test('/aleph advise lists, applies a plugin option by its fixed key, and dismisses; the agent is told once', { options: { rung: '1' } }, async ($, on) => {
  mock.clock(on)
  on('mcp.connect', () => ({ value: { isConnected: true, server: 'plugin:aleph-zero:aleph' } }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify(STATUS) }] } }))
  on('tool.call', () => foundGrep)
  const sets: any[] = []
  on('config.set', ($: any, e: any) => {
    sets.push(e)
    return { value: e.value }
  })
  const store = new Map<string, unknown>()
  on('store.get', ($: any, e: any) => ({ value: store.get(e.key) }))
  on('store.set', ($: any, e: any) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  for (let agent = 0; agent < 6; agent += 1) {
    for (let call = 0; call < 5; call += 1) await $.tool.call({ tool: 'Grep', pattern: 'thing' + call, agentId: 'agent-' + agent } as any)
  }
  const listed = (await $.command.run({ command: 'aleph', args: 'advise' })).text
  expect(listed).toContain('1. Give subagents ranked locations')
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toContain('suggested: 1 change')
  await $.prompt.submit({ text: 'fix the bubble physics step please' })
  await $.prompt.submit({ text: 'and the jump too please now' })
  expect(sent[0].context.join('\n')).toContain('/aleph advise')
  expect(sent[1].context).toBeUndefined()                               // once
  expect(toasts.length).toBe(1)
  expect((await $.command.run({ command: 'aleph', args: 'advise apply 1' })).text).toContain('Applied: rung is now 2')
  expect(sets.map((set) => [set.key, set.value])).toEqual([['aleph-zero.rung', '2']])
  expect((await $.command.run({ command: 'aleph', args: 'advise dismiss 1' })).text).toContain('Dismissed')
  expect((await $.command.run({ command: 'aleph', args: 'advise' })).text).toContain('no suggestion')
  expect((await $.command.run({ command: 'aleph', args: 'advise apply 7' })).text).toContain('no suggestion 7')
})
