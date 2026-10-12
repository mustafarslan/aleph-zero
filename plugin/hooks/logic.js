// What the steering mod decides, as plain functions: nothing here touches Claude Code,
// so each rule can be tested by itself (plugin/tests/logic.test.ts).

// The engine's tools as Claude Code names them: mcp__plugin_<plugin>_<server>__<tool>.
export const ENGINE_TOOL = /^mcp__plugin_aleph-zero_aleph__(search|symbol|outline|status)$/

export const DESCRIPTIONS = {
  search: 'Find code and documentation in this repository by meaning or by name, as the working tree is right now '
    + '(uncommitted edits included). Ask in plain words or with identifiers; returns ranked locations '
    + '(path:lines, kind, name, signature) with snippets. Use this FIRST to locate where something is '
    + 'implemented, before recursive grep or reading whole files; then read only the lines it points to.',
  symbol: 'Find where a name is defined: function, method, class, constant or field. A qualified name narrows it '
    + '(TokenStore.get). Faster and more exact than grep for "where is X defined".',
  outline: 'List what a file defines (functions, classes, fields, headings) with line ranges, instead of reading '
    + 'the whole file. Use before Read on a large file.',
  status: 'Report what is indexed for this worktree and what is not searched (files outside a sparse checkout, submodules).',
}

export const SECTION_ID = 'aleph_zero'
export const SECTION = [
  '# Locating code in this repository',
  'This repository is indexed by Aleph Zero, and the index always matches the working tree.',
  '- To find where something is implemented or handled, call the `search` tool first, in plain words or with '
    + 'identifiers. It returns ranked locations; read only the lines it points to.',
  '- To find where a name is defined, call `symbol`. To see what a file contains without reading it, call `outline`.',
  '- Use grep for an exact string whose every occurrence matters (a rename, a log message) and to check that '
    + 'something is gone. Do not start with a recursive grep or by reading whole files to locate code.',
].join('\n')

// ---- what the engine answered --------------------------------------------------------------

// The text of an MCP tool result, whichever of the shapes it arrives in.
export function resultText(answer) {
  if (!answer) return ''
  if (typeof answer === 'string') return answer
  if (typeof answer.text === 'string') return answer.text
  const blocks = Array.isArray(answer) ? answer : (answer.content || answer.result)
  if (Array.isArray(blocks)) {
    const first = blocks.find((block) => block && typeof block.text === 'string')
    return first ? first.text : ''
  }
  return ''
}

// The hits of a `search` answer asked for as JSON; [] for anything else.
export function hitsOf(answer) {
  if (!answer || answer.isError) return []
  try {
    const parsed = JSON.parse(resultText(answer))
    return Array.isArray(parsed.hits) ? parsed.hits : []
  } catch {
    return []
  }
}

// Hits as a few lines an agent can act on: path:lines, kind, name. Never code.
export function locations(hits, limit) {
  const lines = []
  const seen = new Set()
  for (const hit of hits) {
    const where = hit.path + ':' + hit.start_line + '-' + hit.end_line
    if (seen.has(where)) continue
    seen.add(where)
    lines.push(where + '  ' + hit.kind + (hit.symbol ? '  ' + hit.symbol : ''))
    if (lines.length >= limit) break
  }
  return lines.join('\n')
}

// ---- L2: the first prompt of a session ------------------------------------------------------

// A prompt worth asking the engine about: a sentence, not a command or a word of thanks.
export function worthAsking(text) {
  const trimmed = String(text || '').trim()
  if (!trimmed || trimmed.startsWith('/') || trimmed.startsWith('!')) return false
  return trimmed.split(/\s+/).length >= 4
}

// Rank fusion scores say little by their size. What says something is agreement: the top
// hit was proposed by at least two of the engine's generators (words, names, paths, vectors).
export function confident(hits) {
  if (!hits.length) return false
  const sources = hits[0].sources
  return Array.isArray(sources) && sources.length >= 2
}

export function promptContext(hits) {
  return 'Aleph Zero (this repository\'s code search) ranks these locations highest for the request above. '
    + 'They are a starting point, not an answer; ignore them if the request is not about locating code.\n'
    + locations(hits, 5)
}

// ---- L3: a search command that found nothing --------------------------------------------------

function words(command) {
  // Split like a shell would, for the simple commands this cares about: quotes group, nothing else is special.
  const out = []
  const pattern = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g
  let match
  while ((match = pattern.exec(command)) !== null) {
    // Inside double quotes a shell removes the backslash only before these four characters.
    out.push(match[1] !== undefined ? match[1].replace(/\\(["\\$`])/g, '$1') : match[2] !== undefined ? match[2] : match[3])
  }
  return out
}

const TAKES_A_VALUE = new Set(['-e', '-f', '-m', '-A', '-B', '-C', '-t', '-T', '-g', '--type', '--type-not', '--glob',
  '--include', '--exclude', '--exclude-dir', '--max-count', '--context', '--after-context', '--before-context',
  '-d', '--max-depth', '-j', '--threads', '--regexp', '--file', '--max-filesize', '--color', '--colour'])

// The pattern of a repository-wide search command, as words to search by meaning; or null
// when the command is anything else. It must be right when it answers and silent otherwise:
// no pipes, no redirection, no command lists, nothing aimed outside the repository.
export function searchPattern(command) {
  let text = String(command || '').trim()
  const leading = /^cd\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s*&&\s*/.exec(text)
  if (leading) text = text.slice(leading[0].length)
  if (/[|;&<>]/.test(text.replace(/"[^"]*"|'[^']*'/g, ''))) return null
  // Substitutions and variables are live inside double quotes too: the pattern is then not what is written here.
  if (/`|\$[({A-Za-z_]/.test(text.replace(/'[^']*'/g, ''))) return null
  let parts = words(text)
  while (parts.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(parts[0])) parts = parts.slice(1)
  if (!parts.length) return null
  let program = parts[0].split('/').pop()
  let rest = parts.slice(1)
  if (program === 'git' && rest[0] === 'grep') {
    program = 'git grep'
    rest = rest.slice(1)
  }
  let pattern = null
  const targets = []
  if (program === 'find') {
    for (let index = 0; index < rest.length; index += 1) {
      if (rest[index] === '-name' || rest[index] === '-iname') pattern = rest[index + 1]
      if (index === 0 && !rest[index].startsWith('-')) targets.push(rest[index])
    }
    if (pattern === null || pattern === undefined) return null
    pattern = pattern.replace(/\.[A-Za-z0-9]+$/, '')  // `*handler*.py` is a question about handlers, not about `py`
  } else if (program === 'grep' || program === 'rg' || program === 'git grep') {
    let recursive = program !== 'grep'
    for (let index = 0; index < rest.length; index += 1) {
      const word = rest[index]
      if (word === '--') continue
      if (word.startsWith('-')) {
        if (word === '--recursive' || word === '--dereference-recursive' || /^-[A-Za-z]*[rR]/.test(word)) recursive = true
        if (word === '-e' || word === '--regexp') pattern = pattern === null ? rest[index + 1] : pattern
        if (TAKES_A_VALUE.has(word)) index += 1
        continue
      }
      if (pattern === null) pattern = word
      else targets.push(word)
    }
    if (!recursive || pattern === null || pattern === undefined) return null
  } else {
    return null
  }
  if (targets.some((target) => target.startsWith('/') || target.startsWith('~') || target.startsWith('..'))) return null
  // A pattern is a regular expression or a glob; the engine is asked in words.
  const asWords = String(pattern)
    .replace(/\\[bBwWsSdD]/g, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/[\\^$.*+?(){}|]/g, ' ')
    .split(/\s+/)
    .filter((word) => /[A-Za-z0-9]/.test(word))
  if (!asWords.some((word) => word.replace(/[^A-Za-z0-9]/g, '').length >= 3)) return null
  return asWords.join(' ')
}

// Whether a Bash result is "ran, and found nothing".
export function foundNothing(result) {
  if (!result || result.deny || result.isError) return false
  const inner = result.result
  if (!inner || typeof inner !== 'object' || inner.interrupted) return false
  return String(inner.stdout || '').trim() === '' && String(inner.stderr || '').trim() === ''
}

export function grepNote(pattern, hits) {
  return '\n\n[aleph-zero] The command matched nothing. This is not a list of occurrences of that string: '
    + 'it is where code search by meaning, asked for "' + pattern + '", ranks related code. '
    + 'If you were checking that the string is gone, it is gone.\n' + locations(hits, 5)
}

// Whether a Bash command is a search of the repository at all (for the meter).
export function isSearchCommand(command) {
  const text = String(command || '')
  return /(^|[\s;&|(])(grep|rg|ag|ack)\s/.test(text) || /(^|[\s;&|(])git\s+grep\s/.test(text)
    || /(^|[\s;&|(])find\s[^|;]*-i?name\s/.test(text)
}
