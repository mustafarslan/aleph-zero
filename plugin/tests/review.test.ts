// Findings of the independent review of M4: each of these was wrong before.
import { expect, mock, test } from 'claude-code/testing'
import { isSearchCommand, searchPattern } from '../hooks/logic.js'

const FULL_NAME = 'plugin:aleph-zero:aleph'
const hit = { path: 'src/auth/token_store.py', start_line: 4, end_line: 6, kind: 'function', symbol: 'refresh_token', sources: ['lexical', 'symbol'], score: 0.03 }
const engine = () => ({ value: { content: [{ type: 'text', text: JSON.stringify({ hits: [hit], pending: 0 }) }] } })
const ran = (interpretation: string) => ({ ref: 1, result: { stdout: '', stderr: '', interrupted: false, returnCodeInterpretation: interpretation }, text: '(Bash completed with no output)' })
const rung = (value: string, more = {}) => ({ options: { rung: value, ...more } })

test('a command whose silence does not mean "no match" is left alone', async () => {
  // Quiet, inverted and files-without-match searches print nothing when they DO match (or in every case).
  for (const command of ['grep -rq refresh_token .', 'rg -q refresh_token', 'rg --quiet refresh_token', 'git grep -q refresh_token',
    'grep -rL refresh_token .', 'rg --files-without-match refresh_token', 'grep -rv refresh_token .', 'rg --invert-match refresh_token']) {
    expect(searchPattern(command)).toBe(null)
  }
  // find with an action does something; its silence says nothing about matches.
  for (const command of ['find . -name "__pycache__" -type d -exec rm -rf {} +', 'find . -name "*cache*" -delete',
    'find . -name "*token*" -execdir rm {} +', 'find . -name "*.orig" -ok rm {} ;']) {
    expect(searchPattern(command)).toBe(null)
  }
})

test('a search aimed outside the repository, or one of several commands, is left alone', async () => {
  expect(searchPattern('cd /tmp && rg refresh_token')).toBe(null)
  expect(searchPattern('cd ../other && rg refresh_token')).toBe(null)
  expect(searchPattern('cd ~/notes && grep -rn refresh_token .')).toBe(null)
  expect(searchPattern('rg refresh_token\nrm -rf build')).toBe(null)
  expect(searchPattern('cd src/auth && rg refresh_token')).toBe('refresh_token')   // inside: still fine
})

test('the pattern that is searched is the pattern that was written', async () => {
  expect(searchPattern('grep -rn -- "-foo_bar" src')).toBe('-foo_bar')            // after --, a dash starts a pattern
  expect(searchPattern('rg -r REPL refresh_token')).toBe(null)                    // -r is rg's --replace: not a search for REPL
  expect(searchPattern('rg --replace REPL refresh_token')).toBe(null)
  expect(searchPattern('rg -e alpha_one -e beta_two')).toBe('alpha_one beta_two') // every pattern, not the first
  expect(searchPattern('git grep refresh_token HEAD~5')).toBe(null)               // a search of history, not of the tree
  expect(searchPattern('git grep refresh_token -- src')).toBe('refresh_token')
  expect(searchPattern('grep -rn refresh_token single_file.py')).toBe(null)       // one file is not the repository
  expect(searchPattern('egrep -r "refresh_token|rotate" .')).toBe('refresh_token rotate')
  expect(searchPattern('/usr/bin/grep -rn refresh_token .')).toBe('refresh_token')
})

test('the meter recognises a search command however the program is spelled', async () => {
  for (const command of ['/usr/bin/grep -rn foo .', 'egrep -r foo .', 'fgrep -r foo .', 'ggrep -r foo .']) {
    expect(isSearchCommand(command)).toBe(true)
  }
})

test('L3 needs the command to have reported no match, not just to have printed nothing', rung('3'), async ($, on) => {
  mock.clock(on)
  on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
  on('mcp.call', engine)
  let next = ran('Success')
  on('tool.call', () => next)
  // grep exited 0 and printed nothing (possible with some flags and wrappers): no claim is made.
  expect(await $.tool.call({ tool: 'Bash', command: 'grep -rn refresh_token .' })).toEqual(ran('Success'))
  next = ran('No matches found')
  const out = await $.tool.call({ tool: 'Bash', command: 'grep -rn refresh_token .' })
  expect(out.text).toContain('src/auth/token_store.py:4-6')
  // find always exits 0: for a plain find, printing nothing is the only signal there is.
  next = ran('Success')
  expect((await $.tool.call({ tool: 'Bash', command: 'find . -name "*token_store*"' })).text).toContain('src/auth/token_store.py:4-6')
})

test('L2 keeps its one chance for the first prompt that is a question, and gets it back after /clear', rung('2'), async ($, on) => {
  mock.clock(on)
  on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
  on('mcp.call', engine)
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  on('session.end', () => ({ sessionId: 's' }))
  await $.prompt.submit({ text: 'hi' })
  await $.prompt.submit({ text: 'where are refresh tokens rotated in this service?' })
  await $.prompt.submit({ text: 'and where are invoices rendered every night?' })
  expect(sent[0].context).toBeUndefined()
  expect(sent[1].context.length).toBe(1)          // "hi" did not use the chance up
  expect(sent[2].context).toBeUndefined()
  await $.session.end({ reason: 'clear' })
  await $.prompt.submit({ text: 'where is the billing invoice total computed?' })
  expect(sent[3].context.length).toBe(1)          // a new conversation
})

test('the pointer in Grep and Glob can be switched off by itself, and speaks of each tool in its own terms', rung('1', { grep_pointer: 'off' }), async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  expect(await $.tool.describe({ tool: 'Grep', description: 'Search file contents.', isDeferred: false }))
    .toEqual({ description: 'Search file contents.', isDeferred: false })
  const search = await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__search', description: 'old', isDeferred: true })
  expect(search.isDeferred).toBe(false)           // the rest of rung 1 is still on
})

test('the pointer on Glob does not give grep advice', rung('1'), async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  const glob = (await $.tool.describe({ tool: 'Glob', description: 'Find files.', isDeferred: false })).description
  expect(glob).toContain('Aleph Zero')
  expect(glob).not.toContain('exact string')
})

test('rung 1 loads the four search tools up front and leaves the memory tools on demand', rung('1'), async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  for (const name of ['recall', 'remember']) {
    expect(await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__' + name, description: 'theirs', isDeferred: true }))
      .toEqual({ description: 'theirs', isDeferred: true })
  }
  expect((await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__symbol', description: 'old', isDeferred: true })).isDeferred).toBe(false)
})
