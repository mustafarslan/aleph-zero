import { expect, mock, test } from 'claude-code/testing'
import { factsNote, mayName } from '../hooks/logic.js'

// The knowledge graph handed over by default (DEFERRED 0-graph, part 4): the plugin looks up what a prompt or a grep names.
const FULL_NAME = 'plugin:aleph-zero:aleph'
const connected = (on: any) => on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
const reply = (body: object) => ({ value: { content: [{ type: 'text', text: JSON.stringify(body) }] } })
const FOUND = {
  kind: '', like: 'DELETE /client/7 | DB_URL', late: false, facts: [
    { kind: 'route', name: 'DELETE /client/{id}', path: 'router/router.go', line: 260, detail: 'go clientHandler.DeleteClient' },
    { kind: 'mount', name: '/client', path: 'router/router.go', line: 235, detail: '' },
    { kind: 'env', name: 'DB_URL', path: 'config/db.go', line: 2, detail: 'read' },
    { kind: 'handler', name: 'DELETE /client/{id}', path: 'api/client.go', line: 216, detail: 'clientHandler.DeleteClient' },
  ],
}
const factsCalls = (asked: any[]) => asked.filter((e) => JSON.stringify(e).includes('facts'))
const grep = (files: string[]) => ({ ref: 2, result: { mode: 'files_with_matches', filenames: files, numFiles: files.length, totalFiles: files.length }, text: files.join('\n') })
const bash = (stdout: string) => ({ ref: 1, result: { stdout, stderr: '', interrupted: false, returnCodeInterpretation: 'Success' }, text: stdout })

test('a text may name a route or an environment name; the engine decides which it does', async () => {
  expect(mayName('DELETE http://localhost:8080/client/7 fails and DB_URL is ignored')).toEqual(['DB_URL', '/client/7'])
  expect(mayName('rename the helper in utils')).toEqual([])
  expect(mayName('grep -rn "PAPERLESS_DBHOST" .')).toEqual(['PAPERLESS_DBHOST'])
})

test('the facts are written as where each route is registered and handled, and where each name is read', async () => {
  const note = factsNote(reply(FOUND).value)
  expect(note).toContain('- DELETE /client/{id}: registered at router/router.go:260 (clientHandler.DeleteClient); handler defined at api/client.go:216')
  expect(note).toContain('- mounted: /client at router/router.go:235')
  expect(note).toContain('- DB_URL: read at config/db.go:2')
  expect(factsNote(reply({ kind: '', like: 'MAX_SIZE', facts: [] }).value)).toBe('')
  expect(factsNote(null)).toBe('')
})

test('the facts tool is shown in full while the graph tools are on; the others stay on demand', { options: { rung: '1' } }, async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  const facts = await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__facts', description: 'old', isDeferred: true })
  expect(facts.isDeferred).toBe(false)
  expect(facts.description).toContain('`mentioned`')
  const references = await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__references', description: 'old', isDeferred: true })
  expect(references).toEqual({ description: 'old', isDeferred: true })
})

test('with the graph tools off the facts tool is left as it was', { options: { rung: '1', graph: 'off' } }, async ($, on) => {
  on('tool.describe', ($, e) => ({ description: e.description, isDeferred: e.isDeferred }))
  expect(await $.tool.describe({ tool: 'mcp__plugin_aleph-zero_aleph__facts', description: 'old', isDeferred: true }))
    .toEqual({ description: 'old', isDeferred: true })
})

test('at the default rung, every prompt that names something new gets its facts; each name is looked up once', { options: { rung: '1' } }, async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return reply(FOUND)
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  await $.prompt.submit({ text: 'DELETE http://localhost:8080/client/7 fails and DB_URL is ignored' })
  await $.prompt.submit({ text: 'rename the helper in utils' })
  await $.prompt.submit({ text: 'DB_URL again: why?' })
  expect(asked.length).toBe(1)                                         // nothing new named: no call
  expect(JSON.stringify(asked[0])).toContain('"mentioned":"DELETE http://localhost:8080/client/7 fails and DB_URL is ignored"')
  expect(JSON.stringify(asked[0])).toContain('"timeout_ms":400')
  expect(sent[0].context[0]).toContain('handler defined at api/client.go:216')
  expect(sent[1].context).toBeUndefined()
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toContain('facts added 1 times')
})

test('after /clear the next conversation gets the facts again', { options: { rung: '1' } }, async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return reply(FOUND)
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  on('session.end', () => ({ sessionId: 's' }))
  await $.prompt.submit({ text: 'the server ignores DB_URL when it starts up' })
  await $.session.end({ reason: 'clear' })
  await $.prompt.submit({ text: 'the server ignores DB_URL when it starts up' })
  expect(asked.length).toBe(2)
  expect(sent[1].context[0]).toContain('- DB_URL: read at config/db.go:2')
})

test('a Grep or a recursive grep for a name, whatever it found, gets the facts as context', { options: { rung: '1' } }, async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return reply(FOUND)
  })
  let next: any = grep(['config/db.go', 'docs/setup.md'])
  on('tool.call', () => next)
  const out = await $.tool.call({ tool: 'Grep', pattern: 'DB_URL' })
  expect(out.result).toEqual(grep(['config/db.go', 'docs/setup.md']).result)   // the tool's own result is left as it is
  expect(out.context?.join('\n')).toContain('- DB_URL: read at config/db.go:2')
  next = bash('router/router.go:260: g.DELETE(...)')
  const ran = await $.tool.call({ tool: 'Bash', command: 'grep -rn "/client/:id" .' })
  expect(ran.text).toBe('router/router.go:260: g.DELETE(...)')
  expect(ran.context?.join('\n')).toContain('registered at router/router.go:260')
  expect(factsCalls(asked).length).toBe(2)
  await $.tool.call({ tool: 'Grep', pattern: 'DB_URL' })                         // looked up already
  await $.tool.call({ tool: 'Grep', pattern: 'refreshToken' })                   // names nothing
  expect(factsCalls(asked).length).toBe(2)
})

test('with the graph tools off nothing is looked up', { options: { rung: '1', graph: 'off' } }, async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    return reply(FOUND)
  })
  on('tool.call', () => grep(['config/db.go']))
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.prompt.submit({ text: 'DB_URL is ignored when the server starts' })
  await $.tool.call({ tool: 'Grep', pattern: 'DB_URL' })
  expect(asked.length).toBe(0)
})

test('at rung 2 the first prompt gets the facts and the locations, asked together', { options: { rung: '2' } }, async ($, on) => {
  mock.clock(on)
  connected(on)
  const asked: any[] = []
  on('mcp.call', ($, e) => {
    asked.push(e)
    if (JSON.stringify(e).includes('mentioned')) return reply(FOUND)
    return reply({ hits: [{ path: 'config/db.go', start_line: 1, end_line: 3, kind: 'function', symbol: 'url', sources: ['lexical', 'symbol'], score: 0.03 }], pending: 0 })
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  await $.prompt.submit({ text: 'the server ignores DB_URL when it starts up' })
  expect(asked.length).toBe(2)
  expect(sent[0].context.length).toBe(2)
  expect(sent[0].context[0]).toContain('- DB_URL: read at config/db.go:2')
  expect(sent[0].context[1]).toContain('config/db.go:1-3')
})

test('an engine that never answers the look-up does not hold the prompt', { options: { rung: '1' } }, async ($, on) => {
  const clock = mock.clock(on)
  connected(on)
  on('mcp.call', async () => {
    await clock.sleep(60_000)
    return reply(FOUND)
  })
  const sent: any[] = []
  on('prompt.submit', ($, e) => {
    sent.push(e)
    return { text: e.text }
  })
  const pending = $.prompt.submit({ text: 'the server ignores DB_URL when it starts up' })
  await clock.advance(500)
  await pending
  expect(sent[0].context).toBeUndefined()
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toContain('(1 look-ups too late)')
})

test('when the engine\'s tools are not allowed, the person is told once what to add', { options: { rung: '1' } }, async ($, on) => {
  mock.clock(on)
  connected(on)
  on('mcp.call', () => ({ deny: 'Claude requested permissions to use mcp__plugin_aleph-zero_aleph__facts, but you haven\'t granted it yet.' }))
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.prompt.submit({ text: 'the server ignores DB_URL when it starts up' })
  await $.prompt.submit({ text: 'and REDIS_URL too' })
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('permissions.allow')
  expect((await $.command.run({ command: 'aleph', args: 'stats' })).text).toContain('not allowed yet')
})
