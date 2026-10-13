import { expect, test } from 'claude-code/testing'
import {
  confident, foundNothing, hitsOf, isSearchCommand, locations, resultText, searchPattern, worthAsking,
} from '../hooks/logic.js'

const hit = (path: string, sources: string[], symbol = 'f') =>
  ({ path, start_line: 3, end_line: 9, kind: 'function', symbol, sources, score: 0.03 })

test('a search command is recognised, and its pattern becomes words', async () => {
  expect(searchPattern('grep -rn "refresh_token" .')).toBe('refresh_token')
  expect(searchPattern('grep -rni --include="*.py" "def test_.*login" src')).toBe('def test_ login')
  expect(searchPattern('rg -i -t py "class\\s+TokenStore"')).toBe('class TokenStore')
  expect(searchPattern('rg -e rotate_keys -g "*.py"')).toBe('rotate_keys')
  expect(searchPattern('git grep -n -E "^class Invoice"')).toBe('class Invoice')
  expect(searchPattern('cd src && grep -R "session.revoke" .')).toBe('session revoke')
  expect(searchPattern('find . -name "*token_store*.py"')).toBe('token_store')
  expect(searchPattern('LC_ALL=C grep -r "invoice total" .')).toBe('invoice total')
})

test('anything that is not a plain search of the repository is left alone', async () => {
  expect(searchPattern('grep -n "x" file.py')).toBe(null)                    // not recursive: one file
  expect(searchPattern('grep -rn "refresh_token" . | wc -l')).toBe(null)     // piped: the agent wants a count
  expect(searchPattern('rg refresh_token > out.txt')).toBe(null)             // redirected
  expect(searchPattern('rg refresh_token /var/log')).toBe(null)              // not this repository
  expect(searchPattern('rg refresh_token ../other')).toBe(null)
  expect(searchPattern('rg foo; rm -rf build')).toBe(null)                   // a list of commands
  expect(searchPattern('rg "$(cat pattern.txt)"')).toBe(null)
  expect(searchPattern('find . -type f')).toBe(null)                         // no name asked for
  expect(searchPattern('rg "^.$"')).toBe(null)                               // nothing to ask in words
  expect(searchPattern('ls -la')).toBe(null)
  expect(searchPattern('pytest -k grep')).toBe(null)
  expect(searchPattern('')).toBe(null)
})

test('found nothing means: ran, printed nothing, and (for grep) reported no match', async () => {
  const ran = (stdout: string, extra = {}) =>
    ({ result: { stdout, stderr: '', interrupted: false, returnCodeInterpretation: 'No matches found', ...extra }, text: stdout })
  expect(foundNothing(ran(''), 'grep')).toBe(true)
  expect(foundNothing(ran('  \n'), 'grep')).toBe(true)
  expect(foundNothing(ran('', { returnCodeInterpretation: 'Success' }), 'grep')).toBe(false)   // silent, but it matched
  expect(foundNothing(ran('', { returnCodeInterpretation: undefined }), 'grep')).toBe(false)
  expect(foundNothing(ran('', { returnCodeInterpretation: 'Success' }), 'find')).toBe(true)    // find always exits 0
  expect(foundNothing(ran('src/a.py:3: refresh_token'), 'grep')).toBe(false)
  expect(foundNothing(ran('', { stderr: 'grep: bad regex' }), 'grep')).toBe(false)
  expect(foundNothing(ran('', { interrupted: true }), 'find')).toBe(false)
  expect(foundNothing({ deny: 'not allowed' }, 'grep')).toBe(false)
  expect(foundNothing({ isError: true, result: { stdout: '', stderr: '' } }, 'find')).toBe(false)
  expect(foundNothing(undefined, 'grep')).toBe(false)
})

test('the first prompt is asked about only when it is a sentence', async () => {
  expect(worthAsking('where are refresh tokens rotated?')).toBe(true)
  expect(worthAsking('thanks')).toBe(false)
  expect(worthAsking('commit this')).toBe(false)
  expect(worthAsking('/aleph stats now please ok')).toBe(false)
  expect(worthAsking('')).toBe(false)
})

test('locations go with a prompt only when two generators agree on the top hit', async () => {
  expect(confident([hit('a.py', ['lexical', 'symbol']), hit('b.py', ['lexical'])])).toBe(true)
  expect(confident([hit('a.py', ['lexical']), hit('b.py', ['lexical', 'symbol'])])).toBe(false)
  expect(confident([])).toBe(false)
})

test('an engine answer is read in whatever shape it arrives, and anything else is no hits', async () => {
  const payload = JSON.stringify({ hits: [hit('a.py', ['lexical'])], pending: 0 })
  expect(hitsOf({ content: [{ type: 'text', text: payload }] }).length).toBe(1)
  expect(hitsOf({ result: [{ type: 'text', text: payload }], text: payload }).length).toBe(1)
  expect(hitsOf([{ type: 'text', text: payload }]).length).toBe(1)
  expect(hitsOf(payload).length).toBe(1)
  expect(hitsOf({ content: [{ type: 'text', text: payload }], isError: true })).toEqual([])
  expect(hitsOf({ content: [{ type: 'text', text: 'search failed: boom' }] })).toEqual([])
  expect(hitsOf(null)).toEqual([])
  expect(resultText({ content: [] })).toBe('')
})

test('locations are one line a file: its path, its places, kind and name of the first, never code', async () => {
  const at = (path: string, start: number, symbol = 'f') =>
    ({ path, start_line: start, end_line: start + 6, kind: 'function', symbol, sources: ['x'], score: 0.03 })
  // Five results of which four lie in one file named two files; now the lines go to files and a file's further places ride on its line.
  const hits = [at('a.py', 3, 'alpha'), at('a.py', 3, 'alpha'), at('b.py', 3, ''), at('a.py', 40, 'beta'), at('a.py', 80), at('a.py', 120), at('c.py', 3)]
  expect(locations(hits, 2)).toBe('a.py:3-9, 40-46, 80-86  function  alpha\nb.py:3-9  function')        // three places of a file at most; c.py is a third file
  expect(locations(hits, 5).split('\n')).toEqual(['a.py:3-9, 40-46, 80-86  function  alpha', 'b.py:3-9  function', 'c.py:3-9  function  f'])
  expect(locations([hit('a.py', ['x']), hit('b.py', ['x']), hit('c.py', ['x'])], 2).split('\n').length).toBe(2)
})

test('search commands are counted as grep by the meter', async () => {
  expect(isSearchCommand('grep -rn foo .')).toBe(true)
  expect(isSearchCommand('cd src && rg foo')).toBe(true)
  expect(isSearchCommand('git grep foo')).toBe(true)
  expect(isSearchCommand('find . -name "*.py"')).toBe(true)
  expect(isSearchCommand('git status')).toBe(false)
  expect(isSearchCommand('pytest -q')).toBe(false)
})
