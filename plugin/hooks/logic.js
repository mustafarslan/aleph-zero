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

// Added to the descriptions of Claude Code's own search tools: the place the agent is
// looking at the moment it chooses grep. In the pilot of the measurement the standing
// instruction alone was followed in one session of three.
export const POINTERS = {
  Grep: '\n\nIn this repository, to locate where something is implemented or defined, call the Aleph Zero '
    + '`search` tool (or `symbol` for a name) first: it answers by meaning and by name, with ranked locations. '
    + 'Use this tool for an exact string whose every occurrence matters.',
  Glob: '\n\nIn this repository, to locate where something is implemented or defined, call the Aleph Zero '
    + '`search` tool (or `symbol` for a name) first. Use this tool when it is file names you are after.',
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

// Hits as a few lines an agent can act on: path:lines, kind, name. Never code. A file gets one line: of five results
// several lie in one file, and a line a result named few files. The further places of a listed file ride on its
// line (three at most), with the kind and name of its best.
const PLACES_A_FILE = 3

export function locations(hits, limit) {
  const files = new Map()
  for (const hit of hits) {
    let file = files.get(hit.path)
    if (!file) {
      if (files.size >= limit) continue  // a later file: the lines are full, but a listed file may still gain a place
      file = { places: [], what: '  ' + hit.kind + (hit.symbol ? '  ' + hit.symbol : '') }
      files.set(hit.path, file)
    }
    const place = hit.start_line + '-' + hit.end_line
    if (!file.places.includes(place) && file.places.length < PLACES_A_FILE) file.places.push(place)
  }
  return [...files].map(([path, file]) => path + ':' + file.places.join(', ') + file.what).join('\n')
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

const TAKES_A_VALUE = new Set(['-f', '-m', '-A', '-B', '-C', '-t', '-T', '-g', '--type', '--type-not', '--glob', '--iglob',
  '--include', '--exclude', '--exclude-dir', '--max-count', '--context', '--after-context', '--before-context',
  '-d', '--max-depth', '-j', '--threads', '--file', '--max-filesize', '--color', '--colour', '--sort', '--sortr',
  '-E', '--encoding', '--engine', '--pre', '--pre-glob', '--type-add', '--ignore-file', '--max-columns', '-M'])
// Flags after which printing nothing does not mean "no match": quiet, inverted, files without a match.
const SILENT_BY_DESIGN = new Set(['-q', '--quiet', '--silent', '-L', '--files-without-match', '-v', '--invert-match'])
const GREPS = new Set(['grep', 'egrep', 'fgrep', 'ggrep'])
const FIND_ACTIONS = new Set(['-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprintf', '-fls'])

function outside(path) {
  return path.startsWith('/') || path.startsWith('~') || path === '..' || path.startsWith('../') || path.includes('/../')
}

// A repository-wide search command, read as { program, query }: `program` is 'grep' (grep,
// rg, git grep: they report "no match" by their exit status) or 'find' (it does not), and
// `query` is the pattern as words to search by meaning. null when the command is anything
// else. It must be right when it answers and silent otherwise: no pipes, no redirection,
// no command lists, no substitutions, nothing aimed outside the repository, nothing whose
// silence means something other than "no match".
export function readSearch(command) {
  let text = String(command || '').trim()
  if (/[\n\r]/.test(text)) return null
  const leading = /^cd\s+("[^"]*"|'[^']*'|[^\s;&|]+)\s*&&\s*/.exec(text)
  if (leading) {
    if (outside(leading[1].replace(/^["']|["']$/g, ''))) return null
    text = text.slice(leading[0].length)
  }
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
  const patterns = []
  const targets = []
  let kind
  if (program === 'find') {
    kind = 'find'
    for (let index = 0; index < rest.length; index += 1) {
      if (FIND_ACTIONS.has(rest[index])) return null  // it does something; its silence says nothing about matches
      if ((rest[index] === '-name' || rest[index] === '-iname') && rest[index + 1] !== undefined) patterns.push(rest[index + 1])
      if (index === 0 && !rest[index].startsWith('-')) targets.push(rest[index])
    }
    if (patterns.length !== 1) return null
    patterns[0] = patterns[0].replace(/\.[A-Za-z0-9]+$/, '')  // `*handler*.py` is a question about handlers, not about `py`
  } else if (GREPS.has(program) || program === 'rg' || program === 'git grep') {
    kind = 'grep'
    let recursive = !GREPS.has(program)
    let positional = false   // after `--` every word is a pattern or a path, whatever it starts with
    let byFlag = false       // a pattern given with -e: positional words are then all paths
    let afterDashes = []
    for (let index = 0; index < rest.length; index += 1) {
      const word = rest[index]
      if (!positional && word === '--') {
        positional = true
        continue
      }
      if (!positional && word.startsWith('-') && word !== '-') {
        if (SILENT_BY_DESIGN.has(word)) return null
        if (/^-[A-Za-z]+$/.test(word) && /[qLv]/.test(word) && !(program === 'rg' && word === '-L')) return null
        if (word === '-e' || word === '--regexp') {
          if (rest[index + 1] === undefined) return null
          patterns.push(rest[index + 1])
          byFlag = true
          index += 1
          continue
        }
        if (program === 'rg' && (word === '-r' || word === '--replace' || word === '--files')) return null  // not a search for what follows
        if (GREPS.has(program) && (word === '--recursive' || word === '--dereference-recursive' || /^-[A-Za-z]*[rR]/.test(word))) recursive = true
        // `-E` is rg's --encoding (takes a value) and grep's extended regular expressions (takes none).
        if (TAKES_A_VALUE.has(word) && !(word === '-E' && program !== 'rg')) index += 1
        continue
      }
      if (positional) afterDashes.push(word)
      else if (!byFlag && !patterns.length) patterns.push(word)
      else targets.push(word)
    }
    if (afterDashes.length) {
      if (!byFlag && !patterns.length) patterns.push(afterDashes.shift())
      targets.push(...afterDashes)
      afterDashes = []
    } else if (program === 'git grep' && targets.length) {
      return null  // `git grep pattern HEAD~5`: a revision, a search of history; paths come after `--`
    }
    if (!recursive || !patterns.length) return null
    // One target that looks like a file: a search of that file, not of the repository.
    if (targets.length === 1 && /\.[A-Za-z0-9]{1,5}$/.test(targets[0]) && !targets[0].endsWith('/')) return null
  } else {
    return null
  }
  if (targets.some(outside)) return null
  // A pattern is a regular expression or a glob; the engine is asked in words.
  const asWords = patterns.join(' ')
    .replace(/\\[bBwWsSdD]/g, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/[\\^$.*+?(){}|]/g, ' ')
    .split(/\s+/)
    .filter((word) => /[A-Za-z0-9]/.test(word))
  if (!asWords.some((word) => word.replace(/[^A-Za-z0-9]/g, '').length >= 3)) return null
  return { program: kind, query: asWords.join(' ') }
}

export function searchPattern(command) {
  const read = readSearch(command)
  return read ? read.query : null
}

// Whether a Bash result is "ran, and found nothing". grep, rg and git grep say so by their
// exit status, which Claude Code reports as 'No matches found' (recorded from a real
// session; the build's type declarations were not available): printing nothing is not
// enough, a quiet search prints nothing when it matches. find exits 0 either way, so for
// a find without an action, printing nothing is the only signal there is.
export function foundNothing(result, program) {
  if (!result || result.deny || result.isError) return false
  const inner = result.result
  if (!inner || typeof inner !== 'object' || inner.interrupted) return false
  if (String(inner.stdout || '').trim() !== '' || String(inner.stderr || '').trim() !== '') return false
  return program === 'find' || inner.returnCodeInterpretation === 'No matches found'
}

export function grepNote(pattern, hits) {
  return '\n\n[aleph-zero] The command matched nothing. This is not a list of occurrences of that string: '
    + 'it is where code search by meaning, asked for "' + pattern + '", ranks related code. '
    + 'If you were checking that the string is gone, it is gone.\n' + locations(hits, 5)
}

// Whether a Bash command is a search of the repository at all (for the meter).
export function isSearchCommand(command) {
  const text = String(command || '')
  return /(^|[\s;&|(])(?:[^\s;&|()]*\/)?(grep|egrep|fgrep|ggrep|rg|ag|ack)\s/.test(text) || /(^|[\s;&|(])git\s+grep\s/.test(text)
    || /(^|[\s;&|(])(?:[^\s;&|()]*\/)?find\s[^|;]*-i?name\s/.test(text)
}
