// The side chat's pane: what it draws, what it calls, and what it never touches.
import { expect, mock, test } from 'claude-code/testing'

const FULL_NAME = 'plugin:aleph-zero:aleph'
const PANE = {
  plugin: 'aleph-zero', component: 'Pane', requestId: 'aleph-chat', viewport: { columns: 160, rows: 40 },
  props: { title: 'Aleph side chat', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const
const hit = { path: 'src/auth/token_store.py', start_line: 4, end_line: 6, kind: 'function', symbol: 'refresh_token', sources: ['lexical'], score: 0.03, text: 'def refresh_token(session):\n    return session.post("/oauth/refresh")' }
const usage = { input_tokens: 900, output_tokens: 60, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }

// Everything Claude Code would answer, with counters for what must stay untouched.
function world(on: any, options: { answer?: string, model?: () => any } = {}) {
  const seen = { fills: [] as any[], appends: 0, submits: 0, models: [] as any[], engine: [] as any[], opened: [] as any[], closed: [] as any[] }
  const saved = new Map<string, any>()
  const clock = mock.clock(on)
  on('store.get', ($: any, e: any) => ({ value: saved.get(e.key) }))
  on('store.set', ($: any, e: any) => {
    saved.set(e.key, e.value)
    return { value: undefined }
  })
  on('ui.open', ($: any, e: any) => {
    seen.opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('ui.close', ($: any, e: any) => {
    seen.closed.push(e)
    return { value: undefined }
  })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['not ours'] }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
  on('mcp.call', ($: any, e: any) => {
    seen.engine.push(e)
    const body = JSON.stringify(e).includes('"symbol"') || e.tool === 'symbol'
      ? { symbols: [{ path: 'src/auth/token_store.py', line: 4, kind: 'function', name: 'refresh_token', qualified: 'refresh_token', signature: 'def refresh_token(session)' }] }
      : { hits: [hit], pending: 0 }
    return { value: { content: [{ type: 'text', text: JSON.stringify(body) }] } }
  })
  on('model.complete', options.model || (($: any, e: any) => {
    seen.models.push(e)
    return { value: { isAnswered: true, text: options.answer || 'The exchange is in src/auth/token_store.py:4-6.', usage } }
  }))
  on('prompt.fill', ($: any, e: any) => {
    seen.fills.push(e)
    return { isFilled: true }
  })
  on('prompt.submit', ($: any, e: any) => {
    seen.submits += 1
    return { text: e.text }
  })
  on('session.append', ($: any, e: any) => {
    seen.appends += 1
    return e
  })
  return { seen, saved, clock }
}

test('/aleph opens the pane and prints nothing; stats still prints', async ($, on) => {
  const { seen } = world(on)
  expect(await $.command.run({ command: 'aleph', args: '' })).toEqual({})
  expect(seen.opened.length).toBe(1)
  expect(JSON.stringify(seen.opened[0])).toContain('aleph-chat')
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toMatch(/^rung 1 /)
})

for (const surface of ['terminal', 'desktop'] as const) {
  test('a question is answered from the index with a citation, on ' + surface, async ($, on) => {
    const { seen } = world(on)
    await $.command.run({ command: 'aleph', args: '' })
    const ui = await $.ui.mount({ ...PANE, surface })
    await ui.input({ key: 'question', text: 'where is refresh_token exchanged for an access token?' })
    expect(await ui.find({ type: 'Text', text: /cites src\/auth\/token_store\.py:4-6/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '> where is refresh_token exchanged for an access token?' })).toBeDefined()
    expect(await ui.find({ key: 'insert' })).toBeDefined()
    // Retrieval went to the engine (a search and a lookup of the name), by its full name, as JSON.
    expect(seen.engine.filter((call: any) => !JSON.stringify(call).includes('"remember"')).length).toBe(2)
    expect(JSON.stringify(seen.engine[0])).toContain(FULL_NAME)
    // One model call: the fixed rules first and cacheable, the excerpt with its location, no tools.
    expect(seen.models.length).toBe(1)
    const sent = JSON.stringify(seen.models[0])
    expect(sent).toContain('You have no tools')
    expect(sent).toContain('<source location=\\"src/auth/token_store.py:4-6\\"')
    expect(sent).toContain('haiku')
    await ui.unmount()
  })
}

test('a question and its answer leave the main conversation untouched', async ($, on) => {
  const { seen } = world(on)
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.input({ key: 'question', text: 'where is refresh_token exchanged for an access token?' })
  expect([seen.fills.length, seen.submits, seen.appends]).toEqual([0, 0, 0])
  // An insert is exactly one draft into the prompt box, wrapped and labelled, and nothing else.
  await ui.press({ key: 'insert' })
  expect([seen.fills.length, seen.submits, seen.appends]).toEqual([1, 0, 0])
  const filled = JSON.stringify(seen.fills[0])
  expect(filled).toContain('aleph_side_note status=\\"unverified\\"')
  expect(filled).toContain('src/auth/token_store.py:4-6')
  expect(await ui.find({ type: 'Text', text: /Drafted into the prompt box/ })).toBeDefined()
})

test('m and e choose the model and effort of the next answer, and they are remembered', async ($, on) => {
  const { seen, saved } = world(on)
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'model-next' })
  await ui.press({ key: 'effort-next' })
  expect((await ui.find({ key: 'model-next' }))?.text).toBe('model: mid')        // each key says what is chosen
  expect((await ui.find({ key: 'effort-next' }))?.text).toBe('effort: high')
  await ui.input({ key: 'question', text: 'where is refresh_token exchanged for an access token?' })
  expect(JSON.stringify(seen.models[0])).toContain('sonnet')
  expect(JSON.stringify(seen.models[0])).toContain('high')
  expect(saved.get('aleph-zero.chat-settings')).toEqual({ model: 'sonnet', effort: 'high' })
})

test('docked, the pane is a conversation: the window\'s height, newest at the bottom, the question field under it', async ($, on) => {
  world(on)
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ key: 'chat' }))?.props.height).toBe(30)                  // PANE's scroll.bodyRows
  const messages = await ui.find({ key: 'messages' })
  expect([messages?.props.justifyContent, messages?.props.overflow, messages?.props.flexGrow]).toEqual(['flex-end', 'hidden', 1])
  expect(await ui.find({ type: 'Text', text: /ctrl\+x tab/ })).toBeDefined()      // said while the pane is empty
  expect(await ui.find({ type: 'Text', text: /memory list/ })).toBeDefined()
  await ui.input({ key: 'question', text: 'where is refresh_token exchanged for an access token?' })
  expect(await ui.find({ type: 'Text', text: '⏺' })).toBeDefined()                 // an answer is marked as Claude Code marks its own
  expect(await ui.find({ type: 'Text', text: /ctrl\+x tab/ })).toBeUndefined()    // the empty pane's words go once there is a conversation
  expect(await ui.find({ type: 'Select' })).toBeUndefined()                         // settings are keys, not pickers
  const footer = await ui.find({ type: 'Text', text: /^Esc, then:/ })     // by its words: a Text keeps no key here
  expect(footer).toBeDefined()
  expect(String(footer?.text).length <= 60).toBe(true)                             // one line in the pane's 60 columns
})

test('above the prompt, the pane fits its tree and shows the last two turns', async ($, on) => {
  world(on)
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, placement: 'inline' } })
  expect((await ui.find({ key: 'chat' }))?.props.height).toBeUndefined()
  for (const question of ['where is refresh_token kept now?', 'and who calls refresh_token then?', 'and what does refresh_token return?']) {
    await ui.input({ key: 'question', text: question })
  }
  expect(await ui.find({ type: 'Text', text: '> where is refresh_token kept now?' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '> and what does refresh_token return?' })).toBeDefined()
})

test('a model that does not answer, and an answer with no citation, are shown as what they are', async ($, on) => {
  let mode = 'refuse'
  world(on, { model: () => (mode === 'refuse'
    ? { value: { isAnswered: false, reason: 'rate limited' } }
    : { value: { isAnswered: true, text: 'The excerpts do not say.', usage } }) })
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.input({ key: 'question', text: 'where is refresh_token exchanged for an access token?' })
  expect(await ui.find({ key: 'insert' })).toBeUndefined()        // nothing to insert
  mode = 'answer'
  await ui.input({ key: 'question', text: 'and why is the build slow on mondays?' })
  expect(await ui.find({ type: 'Text', text: /no location cited/ })).toBeDefined()
})

test('another mod\'s pane is left to it', async ($, on) => {
  world(on)
  const ui = await $.ui.mount({ ...PANE, requestId: 'someone-else', surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: 'not ours' })).toBeDefined()
})

test('/aleph check model says whether the side chat can call the model, without printing what it wrote', async ($, on) => {
  let mode = 'ok'
  const { seen } = world(on, { model: ($: any, e: any) => {
    seen.models.push(e)
    return mode === 'ok' ? { value: { isAnswered: true, text: 'SECRET WORDS', usage } } : { deny: 'your organization blocks this model' }
  } })
  const fine = (await $.command.run({ command: 'aleph', args: 'check model' })).text
  expect(fine).toContain('can call the model (haiku, effort medium, 960 tokens)')
  expect(fine).not.toContain('SECRET')
  mode = 'refused'
  expect((await $.command.run({ command: 'aleph', args: 'check model' })).text).toContain('refused')
})


test('the pane says how to close it and how to change its settings, and has keys for both', async ($, on) => {
  const { seen, saved } = world(on)
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /Esc/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /\/aleph configure/ })).toBeDefined()
  await ui.press({ key: 'model-next' })
  await ui.press({ key: 'effort-next' })
  expect(saved.get('aleph-zero.chat-settings')).toEqual({ model: 'sonnet', effort: 'high' })
  await ui.press({ key: 'close' })
  expect(JSON.stringify(seen.closed[0])).toContain('aleph-chat')
})

test('a key typed into the question field does what the key does, not ask it as a question', async ($, on) => {
  // Seen in use: "m" typed while the field had the keys was sent as a question and answered.
  const { seen, saved } = world(on)
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.input({ key: 'question', text: 'm' })
  await ui.input({ key: 'question', text: 'E' })
  expect(seen.models.length).toBe(0)                                  // no question was asked
  expect(saved.get('aleph-zero.chat-settings')).toEqual({ model: 'sonnet', effort: 'high' })
  await ui.input({ key: 'question', text: 'c' })
  expect(JSON.stringify(seen.closed[0])).toContain('aleph-chat')
})

test('the conversation and the meter are kept in the session\'s state, which a reload of the mod keeps', async ($, on) => {
  world(on)
  const kept: any[] = []
  on('state.set', ($: any, e: any, next: any) => {
    kept.push(e)
    return next(e)
  })
  await $.command.run({ command: 'aleph', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.input({ key: 'question', text: 'where is refresh_token exchanged for an access token?' })
  const last = kept.filter((set) => set.plugin === 'aleph-zero' && set.key === 'session').at(-1)
  const value = last.value
  expect(value.turns.length).toBe(1)
  expect(value.turns[0].state).toBe('answered')
  expect(value.turns[0].cited).toEqual(['src/auth/token_store.py:4-6'])
  expect(typeof value.meter.turns).toBe('number')
})
