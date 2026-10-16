// /aleph configure: what it shows, what each key changes and through what, and what it never runs by itself.
import { expect, test } from 'claude-code/testing'
import { commandLine, help, meaning, nextValue, OPTIONS, valueOf, vectorsRow } from '../hooks/configure.js'

const PANE = {
  plugin: 'aleph-zero', component: 'Pane', requestId: 'aleph-config', viewport: { columns: 160, rows: 40 },
  props: { title: 'Aleph configure', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const
const ENGINE = '/Users/someone/.local/bin/aleph-zero'

function status(overrides: any = {}) {
  return {
    root: '/work/shop', repository: true, untracked_cache: false, main_language: 'python',
    sources: [{ name: 'vue-docs', kind: 'path', location: '/work/vue-docs', files: 312, last_error: null }],
    vectors: { chosen: null, verdict: { helps: false, why: 'python: under the bar' }, worker_installed: true, worker_current: true, with_vector: 0, chunks: 1800 },
    memory: { aeon_installed: false, aeon_current: false },
    engine: ENGINE,                       // which engine answered: commands are drafted with it
    ...overrides,
  }
}

// Claude Code's side: the engine's commands, git, the settings writer and the prompt box, each one recorded.
function world(on: any, options: { status?: any, engine?: number, denied?: string, refused?: any } = {}) {
  const seen = { runs: [] as string[][], spawns: [] as string[][], sets: [] as any[], fills: [] as any[], copies: [] as any[], opened: [] as any[], submits: 0 }
  on('session.cwd', () => ({ value: '/work/shop' }))
  on('ui.open', ($: any, e: any) => {
    seen.opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['not ours'] }))
  on('process.run', ($: any, e: any) => {
    seen.runs.push([...e.argv])
    if (e.argv[0] === 'git') return { value: { exitCode: 0, stdout: '', stderr: '' } }
    if (options.refused) return { value: { exitCode: 2, stdout: JSON.stringify(options.refused), stderr: options.refused.refused + '\n' } }
    if (options.engine) return { value: { exitCode: options.engine, stdout: '', stderr: 'aleph-zero: the engine is not installed: uv tool install git+https://github.com/mustafarslan/aleph-zero.git\n' } }
    return { value: { exitCode: 0, stdout: JSON.stringify(options.status || status()), stderr: '' } }
  })
  on('process.spawn', async function* ($: any, e: any) {
    seen.spawns.push([...e.argv])
    yield { stream: 'stdout', text: 'done: ' + e.argv.slice(-2).join(' ') }
    yield { stream: 'stdout', text: '\n' }                     // a line can arrive in pieces
    return { value: { code: 0, signal: null } }
  })
  on('config.set', ($: any, e: any) => {
    seen.sets.push(e)
    return options.denied ? { deny: options.denied } : { value: e.value }
  })
  on('ui.focus', () => ({ value: {} }))     // the engine's side: the ring lands where asked
  on('ui.copy', ($: any, e: any) => {
    seen.copies.push(e)
    return { value: { isCopied: true } }
  })
  on('prompt.fill', ($: any, e: any) => {
    seen.fills.push(e)
    return { isFilled: true }
  })
  on('prompt.submit', ($: any, e: any) => {
    seen.submits += 1
    return { text: e.text }
  })
  return seen
}

async function open($: any, surface: 'terminal' | 'desktop' = 'terminal') {
  expect(await $.command.run({ command: 'aleph', args: 'configure' })).toEqual({})
  return $.ui.mount({ ...PANE, surface })
}

for (const surface of ['terminal', 'desktop'] as const) {
  test('the pane shows the plugin\'s options, this repository\'s settings and what is built here, on ' + surface, async ($, on) => {
    const seen = world(on)
    const ui = await open($, surface)
    expect(JSON.stringify(seen.opened[0])).toContain('aleph-config')
    expect(await ui.find({ key: 'b-rung' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /tools up front, a rule/ })).toBeDefined()          // rung 1, the default, and what it means
    expect(await ui.find({ type: 'Text', text: 'off, as measured' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /\/work\/vue-docs/ })).toBeDefined()
    expect(await ui.find({ key: 'b-src-0' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'not installed' })).toBeDefined()                // Aeon
    // The engine is reached through the plugin's launcher, for the session's directory.
    const asked = seen.runs.find((argv) => argv.includes('status'))!
    expect(asked[0]).toBe('sh')
    expect(asked[1]).toMatch(/scripts\/engine\.sh$/)
    expect(asked.slice(2)).toEqual(['--cwd', '/work/shop', 'status'])
    const footer = await ui.find({ type: 'Text', text: /explains a row/ })     // by its words: a Text keeps no key here
    expect(footer).toBeDefined()
    expect(String(footer?.text).length <= 60).toBe(true)
    await ui.unmount()
  })
}

test('a plugin option is changed through Claude Code\'s settings, to the next value', { options: { rung: '2' } }, async ($, on) => {
  const seen = world(on)
  const ui = await open($)
  expect(await ui.find({ type: 'Text', text: /first prompt/ })).toBeDefined()
  await ui.press({ key: 'b-rung' })
  await ui.press({ key: 'b-graph' })
  expect(seen.sets.map((set: any) => [set.key, set.value])).toEqual([['aleph-zero.rung', '3'], ['aleph-zero.graph', 'off']])
})

test('a refused change says why', async ($, on) => {
  world(on, { denied: 'a managed setting owns it' })
  const ui = await open($)
  await ui.press({ key: 'b-grep_pointer' })
  expect(await ui.find({ type: 'Text', text: /pointer was not changed: a managed setting owns it/ })).toBeDefined()
})

test('the plugin\'s options are there when the engine is not; the rest says why it is not', async ($, on) => {
  world(on, { engine: 127 })
  const ui = await open($)
  expect(await ui.find({ key: 'b-rung' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /the engine is not installed: uv tool install/ })).toBeDefined()
  expect(await ui.find({ key: 'b-dense' })).toBeUndefined()
  expect(await ui.find({ key: 'b-worker' })).toBeUndefined()
})

test('search by meaning steps from the verdict to on, through the engine\'s own setup', async ($, on) => {
  const seen = world(on)
  const ui = await open($)
  await ui.press({ key: 'b-dense' })
  expect(seen.spawns.length).toBe(1)
  expect(seen.spawns[0].slice(2)).toEqual(['--cwd', '/work/shop', 'setup', '--dense'])
  expect(await ui.find({ type: 'Text', text: /done: setup --dense/ })).toBeDefined()
})

test('a linked source is unlinked only after a yes', async ($, on) => {
  const seen = world(on)
  const ui = await open($)
  await ui.press({ key: 'b-src-0' })
  expect(seen.spawns.length).toBe(0)
  expect(await ui.find({ type: 'Text', text: /Unlink vue-docs\?/ })).toBeDefined()
  await ui.press({ key: 'b-no' })
  expect(seen.spawns.length).toBe(0)
  expect(await ui.find({ type: 'Text', text: /Unlink vue-docs\?/ })).toBeUndefined()
  await ui.press({ key: 'b-src-0' })
  await ui.press({ key: 'b-yes' })
  expect(seen.spawns.map((argv) => argv.slice(-3))).toEqual([['source', 'remove', 'vue-docs']])
})

test('a path or URL typed into the field is linked', async ($, on) => {
  const seen = world(on)
  const ui = await open($)
  const field = await ui.find({ key: 'source' })
  expect(field?.props.autoFocus).toBeFalsy()                       // the row keys must not type into it
  await ui.press({ key: 'b-link' })                                // a: the focus onto the field (the kit keeps focus itself)
  await ui.input({ key: 'source', text: 'https://github.com/vuejs/docs' })
  expect(seen.spawns.map((argv) => argv.slice(-3))).toEqual([['source', 'add', 'https://github.com/vuejs/docs']])
})

test('git\'s untracked cache is switched with git itself, in the session\'s directory', async ($, on) => {
  const seen = world(on)
  const ui = await open($)
  await ui.press({ key: 'b-cache' })
  expect(seen.runs.find((argv) => argv[0] === 'git')).toEqual(['git', 'update-index', '--untracked-cache'])
})

test('a build is copied for a terminal with the engine\'s path and shown whole, never run and never sent', async ($, on) => {
  const seen = world(on)
  const ui = await open($)
  await ui.press({ key: 'b-memory' })
  expect(seen.copies.map((copy: any) => copy.text)).toEqual([ENGINE + ' setup --memory'])
  expect(await ui.find({ type: 'Text', text: ENGINE + ' setup --memory' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Copied; run it in a terminal/ })).toBeDefined()
  // A line starting with ! filled into the prompt box goes to the model as a message: nothing is drafted there.
  expect([seen.fills.length, seen.submits, seen.spawns.length]).toEqual([0, 0, 0])
})

test('an engine older than the plugin is named, with how to update it, and its rows are left out', async ($, on) => {
  const older = status()
  delete (older.vectors as any).chosen
  delete (older.memory as any).aeon_installed
  world(on, { status: older })
  const ui = await open($)
  expect(await ui.find({ type: 'Text', text: /The engine is older than this plugin: `uv tool install --reinstall/ })).toBeDefined()
  expect(await ui.find({ key: 'b-dense' })).toBeUndefined()
  expect(await ui.find({ key: 'b-worker' })).toBeUndefined()
  expect(await ui.find({ key: 'b-rung' })).toBeDefined()
})

test('in a directory the engine refuses, the refusal is said and what is built on this machine still shows', async ($, on) => {
  const refused = { root: '/Users/someone', repository: false, refused: 'aleph: /Users/someone is your home directory', vectors: { chosen: null, worker_installed: true, worker_current: true }, memory: { aeon_installed: false, aeon_current: false } }
  world(on, { refused })
  const ui = await open($)
  expect(await ui.find({ type: 'Text', text: /is your home directory/ })).toBeDefined()
  expect(await ui.find({ key: 'b-dense' })).toBeUndefined()
  expect(await ui.find({ key: 'b-worker' })).toBeDefined()
  expect(await ui.find({ key: 'b-memory' })).toBeDefined()
})

test('git\'s untracked cache kept by git\'s configuration has no key: only the configuration turns it off', async ($, on) => {
  world(on, { status: status({ untracked_cache: true, untracked_cache_from: 'config' }) })
  const ui = await open($)
  expect(await ui.find({ key: 'b-cache' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /set in git's configuration/ })).toBeDefined()
})

test('outside a git repository the repository rows are left out, and said why', async ($, on) => {
  world(on, { status: status({ repository: false, sources: [] }) })
  const ui = await open($)
  expect(await ui.find({ type: 'Text', text: /not a git repository/ })).toBeDefined()
  expect(await ui.find({ key: 'b-dense' })).toBeUndefined()
  expect(await ui.find({ key: 'source' })).toBeUndefined()
  expect(await ui.find({ key: 'b-worker' })).toBeDefined()
})

test('the rules: values, steps, meanings and commands for a terminal', () => {
  const [rung, , , graph] = OPTIONS
  expect(valueOf({}, rung)).toBe('1')
  expect(valueOf({ rung: 'nine' }, rung)).toBe('1')
  expect(nextValue(rung, '3')).toBe('0')
  expect(meaning(graph, 'on')).toMatch(/next session/)
  expect(vectorsRow({ chosen: null, verdict: { helps: true } }, 'typescript').flag).toBe('--dense')
  expect(vectorsRow({ chosen: null, verdict: { helps: false } }, null).value).toBe('off until indexed')
  expect(vectorsRow({ chosen: null, verdict: { helps: false } }, 'python').value).toBe('off, as measured')
  expect(vectorsRow({ chosen: true, worker_installed: false }).note).toMatch(/needs the embedding worker/)
  expect(vectorsRow({ chosen: true }).flag).toBe('--no-dense')
  expect(vectorsRow({ chosen: false }).flag).toBe('--auto-dense')
  expect(commandLine('/Users/a b/.venv/bin/aleph-zero', 'setup --worker')).toBe('\'/Users/a b/.venv/bin/aleph-zero\' setup --worker')
})

test('the help under the rows follows the focus ring: what the row does, its values with the current one marked', async ($, on) => {
  world(on)
  const ui = await open($)
  expect(await ui.find({ key: 'help-hint' })).toBeDefined()                // nothing focused: what the three parts are
  await $.ui.focus({ requestId: 'aleph-config', key: 'b-rung' })
  expect(await ui.find({ type: 'Text', text: /^── steering ─/ })).toBeDefined()
  expect(await ui.find({ key: 'help-hint' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '▸' })).toBeDefined()        // rung 1, the default, marked
  expect(await ui.find({ type: 'Text', text: /No rung has yet been shown to save tokens/ })).toBeDefined()
  await $.ui.focus({ requestId: 'aleph-config', key: 'b-graph' })
  expect(await ui.find({ type: 'Text', text: /^── graph tools ─/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /the right file read first more often/ })).toBeDefined()
})

test('every row that can take the ring has help, and the help keeps to the facts it states', () => {
  for (const key of ['b-rung', 'b-grep_pointer', 'b-lines', 'b-graph', 'b-dense', 'b-cache', 'b-src-0', 'b-link', 'source', 'b-worker', 'b-memory']) {
    const about = help(key, { options: {}, status: status(), sources: status().sources })
    expect(about).not.toBeNull()
    expect(about!.text.length < 260).toBe(true)                               // helper text, not a page
  }
  expect(help('b-graph', { options: {} })!.values!.find((item: any) => item.current)!.value).toBe('on')
  expect(help('b-dense', { status: status({ main_language: 'python' }) })!.text).toMatch(/python\*\*: off/)
})

