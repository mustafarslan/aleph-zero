import { expect, mock, test } from 'claude-code/testing'
import { SECTION, SECTION_ID } from '../hooks/logic.js'

const SEARCH = 'mcp__plugin_aleph-zero_aleph__search'
const hit = (path: string, sources: string[]) =>
  ({ path, start_line: 4, end_line: 6, kind: 'function', symbol: 'refresh_token', sources, score: 0.03, text: 'def refresh_token' })
const engine = (hits: unknown[]) => ({ value: { content: [{ type: 'text', text: JSON.stringify({ hits, pending: 0 }) }] } })
const bash = (stdout: string) => ({ ref: 1, result: { stdout, stderr: '', interrupted: false }, text: stdout || '(Bash completed with no output)' })
const rung = (value: string) => ({ options: { rung: value } })
// Claude Code's answer when the mod connects to its own server: the full name to call it by.
const FULL_NAME = 'plugin:aleph-zero:aleph'
const connected = (on: any) => on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
// What Claude Code passes when it renders a system prompt (recorded from a real session, 2.1.296).
const COMPOSE = { model: 'm', promptModel: 'm', tools: ['Bash'], outputStyle: null, traits: [], surfaces: [] }

test('options reach the mod: the command reports the rung', rung('3'), async ($) => {
  const answer = await $.command.run({ command: 'aleph', args: 'stats' })
  expect(answer.text).toMatch(/^rung 3 /)
})

// ---- L3 ------------------------------------------------------------------------------------------

test('L3: a recursive grep that found nothing gets ranked locations, worded as not being occurrences', rung('3'), async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return engine([hit('src/auth/token_store.py', ['lexical', 'symbol'])])
  })
  on('tool.call', () => bash(''))
  const out = await $.tool.call({ tool: 'Bash', command: 'grep -rn "refreshToken" .' })
  expect(out.text).toContain('src/auth/token_store.py:4-6  function  refresh_token')
  expect(out.text).toContain('not a list of occurrences')
  expect(out.result.stdout).toContain('src/auth/token_store.py:4-6')
  expect(JSON.stringify(asked[0])).toContain('refreshToken')
  expect(JSON.stringify(asked[0])).toContain('"timeout_ms":400')
  expect(JSON.stringify(asked[0])).toContain('"format":"json"')
  expect(JSON.stringify(asked[0])).toContain(FULL_NAME)              // called by the name connect gave, not the manifest's
})

test('an engine that is not connected, or whose tools the user has not allowed, changes nothing', rung('3'), async ($, on) => {
  mock.clock(on)
  let state = 'unlisted'
  on('mcp.connect', () => ({ value: state === 'unlisted' ? { isConnected: false, reason: 'unlisted' } : { isConnected: true, server: FULL_NAME } }))
  on('mcp.call', () => ({ deny: 'Claude requested permissions to use mcp__plugin_aleph-zero_aleph__search, but you haven\'t granted it yet.' }))
  on('tool.call', () => bash(''))
  expect(await $.tool.call({ tool: 'Bash', command: 'rg refreshToken' })).toEqual(bash(''))
  state = 'connected'
  expect(await $.tool.call({ tool: 'Bash', command: 'rg refreshToken' })).toEqual(bash(''))
  const check = (await $.command.run({ command: 'aleph', args: 'check' })).text
  expect(check).toContain('permissions.allow')
})

test('L3 is silent when the search found something, was piped, or the engine has nothing', rung('3'), async ($, on) => {
  mock.clock(on)
  connected(on)
  let calls = 0
  on('mcp.call', () => {
    calls += 1
    return engine([])
  })
  let next = bash('src/a.py:3:refreshToken')
  on('tool.call', () => next)
  expect(await $.tool.call({ tool: 'Bash', command: 'grep -rn "refreshToken" .' })).toEqual(bash('src/a.py:3:refreshToken'))
  next = bash('')
  expect(await $.tool.call({ tool: 'Bash', command: 'grep -rn "refreshToken" . | wc -l' })).toEqual(bash(''))
  expect(await $.tool.call({ tool: 'Bash', command: 'ls -la' })).toEqual(bash(''))
  expect(calls).toBe(0)                                           // the engine was not even asked
  expect(await $.tool.call({ tool: 'Bash', command: 'rg refreshToken' })).toEqual(bash(''))
  expect(calls).toBe(1)                                           // asked, had nothing: the result is untouched
})

test('below rung 3 a Bash result is never touched', rung('2'), async ($, on) => {
  mock.clock(on)
  connected(on)
  on('mcp.call', () => engine([hit('src/x.py', ['lexical', 'symbol'])]))
  on('tool.call', () => bash(''))
  expect(await $.tool.call({ tool: 'Bash', command: 'grep -rn "refreshToken" .' })).toEqual(bash(''))
})

test('an engine that fails or never answers leaves the command result as it was', rung('3'), async ($, on) => {
  const clock = mock.clock(on)
  connected(on)
  let mode = 'deny'
  on('mcp.call', async () => {
    if (mode === 'deny') return { deny: 'the engine is down' }
    await clock.sleep(60_000)                                       // a transport that hangs
    return engine([hit('src/x.py', ['lexical', 'symbol'])])
  })
  on('tool.call', () => bash(''))
  expect(await $.tool.call({ tool: 'Bash', command: 'rg refreshToken' })).toEqual(bash(''))
  mode = 'hang'
  const pending = $.tool.call({ tool: 'Bash', command: 'rg refreshToken' })
  await clock.advance(500)                                          // the hook stops waiting at 450 ms
  expect(await pending).toEqual(bash(''))
})

// ---- L2 ------------------------------------------------------------------------------------------

test('L2: the first prompt of a session carries locations when two generators agree; later prompts do not', rung('2'), async ($, on) => {
  mock.clock(on)
  connected(on)
  on('mcp.call', () => engine([hit('src/auth/token_store.py', ['lexical', 'symbol']), hit('docs/guide.md', ['lexical'])]))
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  await $.prompt.submit({ text: 'where are refresh tokens rotated in this service?' })
  await $.prompt.submit({ text: 'and where are invoices rendered every night?' })
  expect(sent[0].context.length).toBe(1)
  expect(sent[0].context[0]).toContain('src/auth/token_store.py:4-6  function  refresh_token')
  expect(sent[0].context[0]).toContain('starting point')
  expect(sent[0].text).toBe('where are refresh tokens rotated in this service?')
  expect(sent[1].context).toBeUndefined()
})

test('L2 adds nothing to small talk, to a weak answer, or below rung 2', rung('2'), async ($, on) => {
  mock.clock(on)
  connected(on)
  let calls = 0
  on('mcp.call', () => {
    calls += 1
    return engine([hit('src/x.py', ['lexical'])])                   // one generator only: not enough
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  await $.prompt.submit({ text: 'thanks' })
  expect(calls).toBe(0)
  expect(sent[0].context).toBeUndefined()
})

test('L2: a weak top hit adds nothing', rung('2'), async ($, on) => {
  mock.clock(on)
  connected(on)
  on('mcp.call', () => engine([hit('src/x.py', ['lexical'])]))
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  await $.prompt.submit({ text: 'where are refresh tokens rotated in this service?' })
  expect(sent[0].context).toBeUndefined()
})

test('L2: at rung 1 the engine is not asked', rung('1'), async ($, on) => {
  mock.clock(on)
  connected(on)
  let calls = 0
  on('mcp.call', () => {
    calls += 1
    return engine([hit('src/x.py', ['lexical', 'symbol'])])
  })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.prompt.submit({ text: 'where are refresh tokens rotated in this service?' })
  expect(calls).toBe(0)
})

test('L2: an engine that never answers does not hold the prompt', rung('2'), async ($, on) => {
  const clock = mock.clock(on)
  connected(on)
  on('mcp.call', async () => {
    await clock.sleep(60_000)
    return engine([hit('src/x.py', ['lexical', 'symbol'])])
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  const pending = $.prompt.submit({ text: 'where are refresh tokens rotated in this service?' })
  await clock.advance(500)
  await pending
  expect(sent.length).toBe(1)
  expect(sent[0].context).toBeUndefined()
})

// ---- the meter -----------------------------------------------------------------------------------

test('the meter adds up requests, keeps subagents apart, and knows which search came first', rung('1'), async ($, on) => {
  const usage = { input_tokens: 10, output_tokens: 20, cache_read_input_tokens: 300, cache_creation_input_tokens: 40, model: 'm' }
  on('turn.step', async function* ($, e) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage }
  })
  on('turn.complete', () => ({ text: '' }))
  on('tool.call', () => ({ result: 'ok' }))
  const saved = new Map<string, any>()
  on('store.set', ($, e) => {
    saved.set(e.key, e.value)
    return { value: undefined }
  })
  on('store.get', ($, e) => ({ value: saved.get(e.key) }))
  const drain = async (stream: any) => {
    let step = await stream.next()
    while (step.done !== true) step = await stream.next()
  }
  await $.tool.call({ tool: SEARCH, query: 'refresh token' })
  await $.tool.call({ tool: 'Bash', command: 'grep -rn foo .' })
  await $.tool.call({ tool: 'Read', file_path: 'a.py' })
  await drain($.turn.step({ turnId: 't', index: 0, model: 'm', messageCount: 1 }))
  await drain($.turn.step({ turnId: 't', index: 1, model: 'm', messageCount: 3 }))
  await drain($.turn.step({ turnId: 's', index: 0, model: 'm', messageCount: 1, agentId: 'agent-1' }))
  await $.turn.complete({ turnId: 't', answer: 'done', durationMs: 5, isAborted: false, usage: null })
  const text = (await $.command.run({ command: 'aleph', args: 'stats' })).text
  expect(text).toContain('turns 1')
  expect(text).toContain('tokens 740 (740 a turn; input 20, output 40, cache read 600, cache write 80) in 2 requests')
  expect(text).toContain('subagents 370 tokens in 1 requests')
  expect(text).toContain('aleph calls 1 · grep calls 1')
  expect(text).toContain('first to be used: aleph')
  // The same figures are kept for a later session to read: a headless run cannot be asked afterwards.
  const last = (await $.command.run({ command: 'aleph', args: 'stats last' })).text
  expect(last).toContain('last session · rung 1 · turns 1')
  expect(last).toContain('aleph calls 1 · grep calls 1')
})

// ---- L1 ------------------------------------------------------------------------------------------

test('L1: the engine tools are loaded up front with the tuned descriptions', rung('1'), async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  const search = await $.tool.describe({ tool: SEARCH, description: 'old words', isDeferred: true })
  expect(search.isDeferred).toBe(false)
  expect(search.description).toContain('Use this FIRST')
  const outline = await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__outline', description: 'old', isDeferred: true })
  expect(outline.isDeferred).toBe(false)
  const other = await $.tool.describe({ tool: 'mcp__github__search', description: 'theirs', isDeferred: true })
  expect(other).toEqual({ description: 'theirs', isDeferred: true })
})

test('L1: one section is added to the system prompt, the same text every time', rung('1'), async ($, on) => {
  const base = [{ id: 'lean_body', text: 'You are an agent.', scope: 'shared' }]
  on('prompt.compose', () => ({ sections: base }))
  const first = await $.prompt.compose(COMPOSE)
  const second = await $.prompt.compose(COMPOSE)
  expect(first.sections.length).toBe(2)
  expect(first.sections[0]).toEqual(base[0])
  expect(first.sections[1]).toEqual({ id: SECTION_ID, text: SECTION, scope: 'session' })
  expect(second).toEqual(first)
})

test('rung 0 changes neither the descriptions nor the prompt', rung('0'), async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  on('prompt.compose', () => ({ sections: [{ id: 'lean_body', text: 'You are an agent.', scope: 'shared' }] }))
  expect(await $.tool.describe({ tool: SEARCH, description: 'old words', isDeferred: true }))
    .toEqual({ description: 'old words', isDeferred: true })
  expect((await $.prompt.compose(COMPOSE)).sections.length).toBe(1)
})
