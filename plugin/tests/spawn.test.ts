import { expect, mock, test } from 'claude-code/testing'
import { SPAWN_NOTE } from '../hooks/logic.js'

// Subagents, the grep help of rung 3 on a first grep, the CLAUDE.md warning and the written report (2026-11-05: in one
// real session subagents made 86% of the requests and located code with grep 13,703 times against Aleph's 8).
const FULL_NAME = 'plugin:aleph-zero:aleph'
const SEARCH = 'mcp__plugin_aleph-zero_aleph__search'
const connected = (on: any) => on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
const hit = (path: string, sources: string[]) =>
  ({ path, start_line: 4, end_line: 6, kind: 'function', symbol: 'refresh_token', sources, score: 0.03 })
const engine = (hits: unknown[]) => ({ value: { content: [{ type: 'text', text: JSON.stringify({ hits, pending: 0 }) }] } })
const options = (values: Record<string, string>) => ({ options: values })
const TASK = 'Find where refresh tokens are rotated and report the files.'
const spawn = (extra: Record<string, unknown> = {}) => ({
  tool_use_id: 'tu1', prompt: TASK, description: 'find rotation', subagentType: 'Explore', provider: { plugin: 'engine', tier: 'core' },
  parentModel: 'm', background: false, fork: false, ...extra,
})
const grep = (files: string[]) => ({ ref: 2, result: { mode: 'files_with_matches', filenames: files, numFiles: files.length, totalFiles: files.length }, text: files.join('\n') })
const drain = async (stream: any) => {
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
}

test('rung 1: a subagent\'s task gets the note after its brief, and its tool calls are counted under its type', options({ rung: '1' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return engine([])
  })
  const started: any[] = []
  on('agent.spawn', ($, e) => {
    started.push(e)
    return { model: 'm', agentId: 'agent-1' }
  })
  on('tool.call', () => grep(['src/a.py']))
  on('turn.complete', () => ({ text: '' }))
  await $.agent.spawn(spawn() as any)
  expect(started[0].prompt.startsWith(TASK)).toBe(true)                 // the brief is left as it is, first
  expect(started[0].prompt).toContain(SPAWN_NOTE)
  expect(started[0].prompt).not.toContain('ranks these locations')       // rung 1: no locations, no engine call
  expect(asked.length).toBe(0)
  await $.tool.call({ tool: 'Grep', pattern: 'rotate_refresh', agentId: 'agent-1' } as any)
  await $.tool.call({ tool: SEARCH, query: 'rotation', agentId: 'agent-1' } as any)
  await $.turn.complete({ turnId: 's', answer: 'done', durationMs: 5, isAborted: false, usage: null, agentId: 'agent-1' } as any)
  const text = (await $.command.run({ command: 'aleph', args: 'stats' })).text
  expect(text).toContain('1 subagents')
  expect(text).toContain('subagents 50% (1 of 2)')
  expect(text).toContain('subagents 0% of subagents that did')          // it grepped first
  expect(text).toContain('to 1 subagent tasks (0 with locations)')
})

test('rung 2: a subagent\'s task also gets ranked locations when the engine is sure of them', options({ rung: '2' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return engine([hit('src/auth/token_store.py', ['lexical', 'symbol'])])
  })
  const started: any[] = []
  on('agent.spawn', ($, e) => {
    started.push(e)
    return { model: 'm', agentId: 'agent-2' }
  })
  on('tool.call', () => ({ result: 'ok' }))
  await $.agent.spawn(spawn({ subagentType: 'general-purpose' }) as any)
  expect(started[0].prompt).toContain('src/auth/token_store.py:4-6  function  refresh_token')
  expect(JSON.stringify(asked[0])).toContain('"kind":"code"')
  await $.tool.call({ tool: 'Read', file_path: 'src/auth/token_store.py', agentId: 'agent-2' } as any)
  const text = (await $.command.run({ command: 'aleph', args: 'stats' })).text
  expect(text).toContain('to 1 subagent tasks (1 with locations)')
  expect(text).toContain('100% of reads were of a file it added')
})

test('forks, resumed runs, workflow agents and the option switched off are left alone', options({ rung: '2' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  on('mcp.call', () => engine([hit('src/a.py', ['lexical', 'symbol'])]))
  const started: any[] = []
  on('agent.spawn', ($, e) => {
    started.push(e)
    return { model: 'm', agentId: 'agent-' + started.length }
  })
  await $.agent.spawn(spawn({ fork: true }) as any)
  await $.agent.spawn(spawn({ resume: { agentId: 'agent-0' }, prompt: 'go on with the work please' }) as any)
  await $.agent.spawn(spawn({ workflow: { runId: 'wf_1', agentIndex: 1 } }) as any)
  await $.agent.spawn(spawn({ prompt: 'hi' }) as any)
  expect(started.map((e) => e.prompt)).toEqual([TASK, 'go on with the work please', TASK, 'hi'])
})

test('the option `subagents off` leaves every task as it is', options({ rung: '2', subagents: 'off' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  on('mcp.call', () => engine([hit('src/a.py', ['lexical', 'symbol'])]))
  const started: any[] = []
  on('agent.spawn', ($, e) => {
    started.push(e)
    return { model: 'm', agentId: 'agent-1' }
  })
  await $.agent.spawn(spawn() as any)
  expect(started[0].prompt).toBe(TASK)
})

test('rung 3 with grep_assist first: the first locating grep of a run gets locations beside its result, once', options({ rung: '3', grep_assist: 'first' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return engine([hit('src/auth/token_store.py', ['lexical', 'symbol'])])
  })
  on('tool.call', () => grep(['src/x.py']))
  const first = await $.tool.call({ tool: 'Grep', pattern: 'refreshToken' })
  expect(first.result).toEqual(grep(['src/x.py']).result)               // the tool's own result is left as it is
  expect(first.context?.join('\n')).toContain('not occurrences of the pattern')
  expect(first.context?.join('\n')).toContain('src/auth/token_store.py:4-6')
  const second = await $.tool.call({ tool: 'Grep', pattern: 'rotateToken' })
  expect(second.context).toBeUndefined()                                // once a run
  const inSubagent = await $.tool.call({ tool: 'Grep', pattern: 'rotateToken', agentId: 'agent-9' } as any)
  expect(inSubagent.context?.join('\n')).toContain('src/auth/token_store.py')   // a subagent is a run of its own
  expect(asked.length).toBe(2)
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toContain('0 empty and 2 first greps')
})

test('rung 3 by default helps only a grep that found nothing', options({ rung: '3' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  let calls = 0
  on('mcp.call', () => {
    calls += 1
    return engine([hit('src/a.py', ['lexical', 'symbol'])])
  })
  on('tool.call', () => grep(['src/x.py']))
  expect((await $.tool.call({ tool: 'Grep', pattern: 'refreshToken' })).context).toBeUndefined()
  expect(calls).toBe(0)
})

test('a CLAUDE.md that names another rung is pointed out once', options({ rung: '1' }), async ($, on) => {
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: undefined }))
  on('fs.read', ($: any, e: any) => {
    if (String(e.path).endsWith('/CLAUDE.md') && !String(e.path).includes('.claude/')) return { value: '# Project\nAleph Zero runs at rung 3 with the graph on.\n' }
    throw new Error('ENOENT')
  })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  mock.clock(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/repo' } as any)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/repo' } as any)
  await Promise.resolve()
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('CLAUDE.md says rung 3, but the plugin runs at rung 1')
})

test('a project\'s settings that give the plugin another rung are pointed out first', options({ rung: '1' }), async ($, on) => {
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: undefined }))
  on('fs.read', ($: any, e: any) => {
    if (String(e.path).endsWith('.claude/settings.json')) return { value: JSON.stringify({ pluginConfigs: { 'aleph-zero@aleph-zero': { options: { rung: '3' } } } }) }
    throw new Error('ENOENT')
  })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  mock.clock(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/repo' } as any)
  await Promise.resolve()
  expect(toasts[0]).toContain('.claude/settings.json sets rung 3, but the plugin runs at rung 1')
})

test('stats report writes the report and its figures where git leaves them alone', options({ rung: '1' }), async ($, on) => {
  mock.clock(on)
  on('session.cwd', () => ({ value: '/work/repo' }))
  const written = new Map<string, string>()
  on('fs.write', ($: any, e: any) => {
    written.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.read', () => ({ value: JSON.stringify({ version: '9.9.9' }) }))
  on('turn.step', async function* ($, e) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn',
      usage: { input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 1, cache_creation_input_tokens: 1, model: 'm' } }
  })
  on('tool.call', () => ({ result: 'ok' }))
  await $.tool.call({ tool: SEARCH, query: 'x' })
  await drain($.turn.step({ turnId: 't', index: 0, model: 'm', messageCount: 1 }))
  const answer = (await $.command.run({ command: 'aleph', args: 'stats report' })).text
  expect(answer).toContain('/work/repo/.aleph/reports/')
  const paths = [...written.keys()]
  expect(paths).toContain('/work/repo/.aleph/.gitignore')
  const markdown = [...written.entries()].find(([path]) => path.endsWith('.md'))![1]
  expect(markdown).toContain('plugin 9.9.9')
  expect(markdown).toContain('evidence of **use**')
  const figures = JSON.parse([...written.entries()].find(([path]) => path.endsWith('.json'))![1])
  expect(figures.meter.loops.main.locate.aleph).toBe(1)
})

test('a late look-up is asked once more, not on every grep that names the same thing', options({ rung: '1' }), async ($, on) => {
  mock.clock(on)
  connected(on)
  let calls = 0
  on('mcp.call', () => {
    calls += 1
    return { value: { content: [{ type: 'text', text: JSON.stringify({ kind: '', like: 'DB_URL', facts: [], late: true, pending: 3 }) }] } }
  })
  on('tool.call', () => grep([]))
  for (let index = 0; index < 5; index += 1) await $.tool.call({ tool: 'Grep', pattern: 'DB_URL' })
  expect(calls).toBe(2)
})

test('the engine\'s answer times are read from the clock Claude Code resolves', options({ rung: '3' }), async ($, on) => {
  const clock = mock.clock(on)
  connected(on)
  on('mcp.call', async () => {
    await clock.sleep(120)
    return engine([hit('src/a.py', ['lexical', 'symbol'])])
  })
  on('tool.call', () => grep([]))
  const pending = $.tool.call({ tool: 'Grep', pattern: 'refreshToken' })
  await clock.advance(200)
  await pending
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toContain('p50 120 ms')
})
