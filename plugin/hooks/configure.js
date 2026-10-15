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
  { key: 'graph', hotkey: 'g', label: 'graph tools', values: ['off', 'on'], default: 'off' },
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
export function vectorsRow(vectors) {
  const v = vectors || {}
  const helps = Boolean(v.verdict && v.verdict.helps)
  const chosen = typeof v.chosen === 'boolean' ? v.chosen : null
  const on = chosen === null ? helps : chosen
  const value = chosen === null ? (helps ? 'on' : 'off') + ', as measured' : (chosen ? 'on' : 'off') + ', chosen'
  const notes = []
  if (on && v.worker_installed === false) notes.push('needs the embedding worker: w')
  else if (on && Number(v.chunks) > 0) notes.push(Number(v.with_vector || 0).toLocaleString('en-US') + ' of ' + Number(v.chunks).toLocaleString('en-US') + ' chunks have one')
  const next = chosen === null ? 'dense' : chosen ? 'no-dense' : 'auto-dense'
  return { value, note: notes.join(''), flag: '--' + next }
}

export function machineState(installed, current) {
  if (installed === null || installed === undefined) return 'unknown'
  if (!installed) return 'not installed'
  return current === false ? 'installed, out of date' : 'installed'
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
