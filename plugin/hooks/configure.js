// /aleph configure's rules, as plain functions: the rows it shows, what each key changes, the command each change
// runs. Nothing here touches Claude Code (plugin/tests/configure.test.ts).
//   Plugin      the plugin's own options, through Claude Code's settings ($.config.set): they apply at once, the
//               mod being loaded again with them; the graph tools from the next session (the engine reads it at start).
//   Repository  the engine's settings for this repository, through its own commands (engine.sh), and git's cache.
//   Machine     what is built once per machine. Builds take minutes: the pane copies the command for a terminal.

export const CONFIG_PANE = 'aleph-config'
export const FOOTER_COLUMNS = 60
export const LABEL_COLUMNS = 22   // a row's key and name, so that values line up

// The plugin's options as the manifest names them, each with its values in the order a key steps through them.
export const OPTIONS = [
  { key: 'rung', hotkey: 's', label: 'steering', values: ['0', '1', '2', '3'], default: '1' },
  { key: 'grep_pointer', hotkey: 'p', label: 'pointer', values: ['on', 'off'], default: 'on' },
  { key: 'lines', hotkey: 'l', label: 'rung-2 lines', values: ['places', 'signatures'], default: 'places' },
  { key: 'graph', hotkey: 'g', label: 'graph tools', values: ['on', 'off'], default: 'on' },
]

// Short enough for one row of the pane beside the label and the value.
const MEANING = {
  rung: {
    0: 'the search tools only',
    1: 'tools up front, a rule, a pointer',
    2: '+ locations with the first prompt',
    3: '+ locations after an empty search',
  },
  grep_pointer: { on: 'Grep and Glob point to search', off: 'Grep and Glob as they are' },
  lines: { places: 'rung 2 lists each file\'s places', signatures: '+ each file\'s first signature' },
  graph: { off: 'not offered', on: 'callers, facts, decisions' },
}

// An option's value as the mod was loaded with it; anything else is the manifest's default.
export function valueOf(options, option) {
  const raw = options && options[option.key] !== undefined ? String(options[option.key]).trim().toLowerCase() : ''
  return option.values.includes(raw) ? raw : option.default
}

export function nextValue(option, value) {
  return option.values[(option.values.indexOf(value) + 1) % option.values.length]
}

// What a value means, in one line. The graph option reaches the engine when Claude Code starts it.
export function meaning(option, value) {
  return (MEANING[option.key] && MEANING[option.key][value] || '') + (option.key === 'graph' ? ' · next session' : '')
}

// The key Claude Code's settings know a plugin option by.
export function settingKey(option) {
  return 'aleph-zero.' + option.key
}

// Search by meaning in this repository: the repository's own choice if it made one, else the benchmark's verdict.
// The key steps verdict → on → off → verdict.
export function vectorsRow(vectors, language) {
  const v = vectors || {}
  const helps = Boolean(v.verdict && v.verdict.helps)
  const chosen = typeof v.chosen === 'boolean' ? v.chosen : null
  const on = chosen === null ? helps : chosen
  // Before a first index the main language is not known, nor its verdict: off for now, not "measured off".
  const value = chosen === null ? (language ? (helps ? 'on' : 'off') + ', as measured' : 'off until indexed') : (chosen ? 'on' : 'off') + ', chosen'
  const notes = []
  if (on && v.worker_installed === false) notes.push('needs the embedding worker: w')
  else if (on && Number(v.chunks) > 0) notes.push(Number(v.with_vector || 0).toLocaleString('en-US') + ' of ' + Number(v.chunks).toLocaleString('en-US') + ' chunks have one')
  const next = chosen === null ? 'dense' : chosen ? 'no-dense' : 'auto-dense'
  return { value, note: notes.join(''), flag: '--' + next }
}

export function machineState(installed, current) {
  if (installed === null || installed === undefined) return 'unknown'
  if (!installed) return 'not installed'
  return current === false ? 'out of date' : 'installed'
}

// A build for a terminal: the engine by its path (Claude Code started from the Dock, and a person's shell, may not
// have it on PATH), quoted when it must be. Not drafted into the prompt box: a line filled in there that starts
// with ! is sent to the model as a message, not run by the shell (seen in Claude Code 2.1.300).
export function commandLine(enginePath, args) {
  const path = String(enginePath || 'aleph-zero')
  const quoted = /^[A-Za-z0-9_./~+-]+$/.test(path) ? path : '\'' + path.replace(/'/g, '\'\\\'\'') + '\''
  return quoted + ' ' + args
}

// The engine's own commands for this repository, through the plugin's launcher.
export function engineCommand(root, cwd, args) {
  return ['sh', root + '/scripts/engine.sh', '--cwd', cwd, ...args]
}

// The last line a command printed: what the pane says it did, or why it could not.
export function lastLine(text) {
  const lines = String(text || '').split('\n').map((line) => line.trim()).filter(Boolean)
  return lines.length ? lines[lines.length - 1] : ''
}

export function fit(text, columns) {
  const s = String(text || '')
  return s.length <= columns ? s : s.slice(0, Math.max(1, columns - 1)) + '…'
}

// ---- the help under the rows ----------------------------------------------------------------
// One entry for the row the focus ring is on (↑↓, Tab or a click), drawn in a fixed place under all the rows, so the
// rows never move. Each says the same four things, briefly: what it does (Markdown, light), its values with the
// current one marked, when a change applies, and what was measured or not (frame-features-plainly).

export const VALUE_COLUMNS = 12

const VALUES = {
  rung: [
    ['0', 'the search tools only'],
    ['1', 'tools up front, one standing rule, a Grep pointer'],
    ['2', 'adds locations to a session\'s first prompt'],
    ['3', 'adds locations after a search that found nothing'],
  ],
  grep_pointer: [['on', 'one sentence in Grep\'s and Glob\'s descriptions points to `search`'], ['off', 'Grep and Glob as Claude Code has them']],
  lines: [['places', 'each file with its line ranges'], ['signatures', 'also each file\'s first signature']],
  graph: [['on', '`references`, `facts` and `decisions` offered to the agent'], ['off', 'the search tools alone']],
}

const OPTION_HELP = {
  rung: { text: 'How far the plugin steers Claude Code towards Aleph\'s search before it reaches for a recursive grep.', applies: 'Applies at once.', note: ['No rung has yet been shown to save tokens or tool calls (ADR-011).', 'warning'] },
  grep_pointer: { text: 'A pointer in the descriptions of Claude Code\'s own **Grep** and **Glob** tools, part of rung 1.', applies: 'Applies at once.' },
  lines: { text: 'What rung 2 shows for each file it adds to a session\'s first prompt. Used at rung 2 and above only.', applies: 'Applies at once.' },
  graph: { text: 'Who calls what (`references`), the routes, models and environment names a repository states (`facts`), and the decision records behind a change (`decisions`).', applies: 'From the next session: the engine reads it when Claude Code starts it.', note: ['Not yet measured to help an agent.', 'warning'] },
}

// The help for the element `key` (a Button's or the field's key); null for one without help.
export function help(key, context) {
  const { options, status, sources = [] } = context || {}
  const option = OPTIONS.find((item) => 'b-' + item.key === key)
  if (option) {
    const current = valueOf(options, option)
    const about = OPTION_HELP[option.key]
    return { title: option.label, text: about.text, applies: about.applies, note: about.note,
      values: VALUES[option.key].map(([value, meaning]) => ({ value, meaning, current: value === current })) }
  }
  const vectors = (status && status.vectors) || {}
  const language = status && status.main_language
  if (key === 'b-dense') {
    const helps = Boolean(vectors.verdict && vectors.verdict.helps)
    const chosen = typeof vectors.chosen === 'boolean' ? vectors.chosen : null
    return {
      title: 'search by meaning',
      text: 'Vectors beside words and names: a question in plain words finds code that uses other words for it. Left alone, it follows the benchmark for the repository\'s main language' + (language ? ' (**' + language + '**: ' + (helps ? 'on' : 'off') + ')' : '') + '.',
      values: [
        { value: 'measured', meaning: 'as the benchmark says for the language', current: chosen === null },
        { value: 'on', meaning: 'vectors for this repository, made in the background', current: chosen === true },
        { value: 'off', meaning: 'words and names only', current: chosen === false },
      ],
      applies: 'Applies at the next search.',
      note: vectors.worker_installed === false ? ['Needs the embedding worker: w copies its build.', 'warning'] : undefined,
    }
  }
  if (key === 'b-cache' || key === 'b-cache-config') {
    const fromConfig = status && status.untracked_cache_from === 'config'
    return {
      title: 'untracked cache',
      text: 'Git keeps a note of the untracked files, so the look at the worktree that every search starts with is faster on a large repository.',
      values: [{ value: 'on', meaning: 'kept in ' + (fromConfig ? 'git\'s configuration' : 'this repository\'s index'), current: Boolean(status && status.untracked_cache) },
        { value: 'off', meaning: 'git reads the whole worktree each time', current: !(status && status.untracked_cache) }],
      applies: fromConfig ? 'Set by `core.untrackedCache`: `git config --unset core.untrackedCache` turns it off.' : 'Applies at once; changes no configuration.',
    }
  }
  const linked = /^b-src-(\d+)$/.exec(String(key || ''))
  if (linked) {
    const source = sources[Number(linked[1])] || {}
    return { title: String(source.name || 'source'), text: '`' + String(source.location || '') + '`, ' + (source.files || 0) + ' files, searched beside this repository' + (source.kind === 'url' ? '; fetched again by `aleph update`.' : '; read where it is.'), applies: 'Its digit unlinks it after a yes; its files leave the index.' }
  }
  if (key === 'b-link' || key === 'source') {
    return { title: 'link a source', text: 'Documentation or code from elsewhere, searched beside this repository. A **path** is read where it is; a **git URL** is cloned into Aleph\'s cache.', applies: 'Enter links it; a URL uses the network.' }
  }
  if (key === 'b-worker') {
    return { title: 'embedding worker', text: 'llama.cpp and a 310 MB model, built once for this machine: they make the vectors that search by meaning uses.', applies: 'w copies `setup --worker` for a terminal: a download and a few minutes.' }
  }
  if (key === 'b-memory') {
    return { title: 'memory (Aeon)', text: 'Recalls earlier side-chat answers by meaning. Without it they are recalled by words and recency.', applies: 'm copies `setup --memory` for a terminal: under a minute; needs Xcode\'s command-line tools.' }
  }
  return null
}

// What the help area says while no row has the focus.
export const HELP_HINT = '**Plugin** options apply in Claude Code at once · **Repository** settings are this worktree\'s · **Machine** builds are copied for a terminal.'
export const HELP_KEYS = '↑↓ moves over the rows; the one under the ring is explained here.'
