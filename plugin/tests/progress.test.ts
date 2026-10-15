// How far vectors are made: shown while there is work, cleared when it is done, never where vectors are off.
import { expect, mock, test } from 'claude-code/testing'
import { etaText, nextPoll, partLines, progressOf, progressWord, statusLine, PROGRESS_FAST_MS, PROGRESS_OFF_MS, PROGRESS_SLOW_MS } from '../hooks/configure.js'

const FULL_NAME = 'plugin:aleph-zero:aleph'

function statusOf(have: number, chunks = 8239, extra: any = {}) {
  return {
    pending: 0,
    vectors: {
      enabled: true, worker_installed: true, chunks, with_vector: have,
      parts: [{ name: 'code', kind: 'code', with_vector: have, chunks: chunks - 300 }, { name: 'team', kind: 'source', with_vector: 0, chunks: 300 }],
      making: { state: 'making', reason: '', chunks_per_second: 90 },
      ...extra,
    },
  }
}

test('the status line says the counts and a measured estimate, and keeps within its share of the line', () => {
  const line = statusLine(progressOf(statusOf(3100), null, 1000))!
  expect(line).toBe('vectors 3,100/8,239 · <1 min')
  expect(line.length <= 40).toBe(true)
  expect(statusLine(progressOf(statusOf(31000, 880000), null, 1000))!.length <= 40).toBe(true)
})

test('nothing is said once every chunk has a vector, nor where vectors are off or there is no worker', () => {
  expect(statusLine(progressOf(statusOf(8239), null, 1000))).toBeUndefined()
  expect(statusLine(progressOf(statusOf(10, 8239, { enabled: false }), null, 1000))).toBeUndefined()
  expect(statusLine(progressOf(statusOf(10, 8239, { worker_installed: false }), null, 1000))).toBeUndefined()
  expect(progressWord(progressOf(statusOf(8239), null, 1000))).toBe('')
})

test('a worker that does not answer reads as waiting, with no estimate', () => {
  const waiting = progressOf(statusOf(3100, 8239, { making: { state: 'waiting', reason: 'the worker did not answer', chunks_per_second: 90 } }), null, 1000)
  expect(statusLine(waiting)).toBe('vectors 3,100/8,239 · waiting')
  expect(waiting.eta).toBeNull()
})

test('without a rate from the server the estimate is measured between two readings, on what is left', () => {
  const noRate = (have: number, chunks: number) => statusOf(have, chunks, { making: { state: 'idle', reason: '', chunks_per_second: 0 } })
  const first = progressOf(noRate(1000, 2000), null, 0)
  expect(first.eta).toBeNull()                                         // one reading: no estimate
  const second = progressOf(noRate(1500, 2000), first, 10_000)          // 500 in 10 s
  expect(Math.round(second.eta!)).toBe(10)
  const pulled = progressOf(noRate(1500, 2600), second, 20_000)        // a pull added chunks: no negative rate
  expect(pulled.eta).toBeNull()
})

test('files not indexed yet are said before the vectors, so the total does not read as done', () => {
  const indexing = progressOf({ pending: 140, vectors: { enabled: true, worker_installed: true, chunks: 100, with_vector: 100 } }, null, 0)
  expect(statusLine(indexing)).toBe('indexing 140 files')
})

test('the poll is quick while there is work, slow when caught up, slower where vectors are off', () => {
  expect(nextPoll(progressOf(statusOf(3100), null, 0))).toBe(PROGRESS_FAST_MS)
  expect(nextPoll(progressOf(statusOf(8239), null, 0))).toBe(PROGRESS_SLOW_MS)
  expect(nextPoll(progressOf(statusOf(0, 10, { enabled: false }), null, 0))).toBe(PROGRESS_OFF_MS)
  expect(etaText(5400)).toBe('~2 h')
  expect(nextPoll(progressOf(statusOf(3100), null, 0), 330)).toBe(PROGRESS_FAST_MS)    // babel's look: still every 10 s
  expect(nextPoll(progressOf(statusOf(3100), null, 0), 1500)).toBe(45000)            // a slow look paces itself
  expect(etaText(30)).toBe('<1 min')
})

test('each part has its own line: the code, the documentation, each linked source', () => {
  const lines = partLines(statusOf(3100).vectors.parts)
  expect(lines.map((line: any) => line.name)).toEqual(['code', 'team'])
  expect(lines[1].text).toBe('0 of 300 chunks · 0%')
})

test('an interactive session shows the line while the server makes vectors, and clears it when they are made', async ($, on) => {
  const clock = mock.clock(on)
  const lines: any[] = []
  let have = 3100
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify(statusOf(have)) }] } }))
  on('ui.status', ($: any, e: any) => { lines.push(e.text); return { value: undefined } })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/shop' })
  await clock.advance(1500)
  expect(lines.at(-1)).toBe('vectors 3,100/8,239 · <1 min')
  have = 8239
  await clock.advance(11000)                                      // the next look, 10 s later
  expect(lines.at(-1)).toBeUndefined()
})

test('a headless run makes no call of the plugin\'s own for this', async ($, on) => {
  const clock = mock.clock(on)
  const calls: any[] = []
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  on('mcp.connect', () => ({ value: { isConnected: true, server: FULL_NAME } }))
  on('mcp.call', ($: any, e: any) => { calls.push(e); return { value: { content: [{ type: 'text', text: '{}' }] } } })
  await $.session.start({ surface: null, isInteractive: false, cwd: '/work/shop' })
  await clock.advance(10_000)
  expect(calls.length).toBe(0)
})
