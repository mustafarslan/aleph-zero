// Aleph Zero's steering mod. It holds no retrieval logic: everything it knows about the
// code comes from the engine, through $.mcp.call. Each rung is a setting, so a measurement
// can switch them one at a time (docs/adr/ADR-011):
//   0  the engine's tools as they are
//   1  the tools loaded up front with tuned descriptions, and a short standing instruction
//   2  ranked locations ride along with the first prompt of a session
//   3  a recursive grep that found nothing gets ranked locations appended
// The meter runs on every rung.
import {
  DESCRIPTIONS, ENGINE_TOOL, GRAPH_LINE, POINTERS, SECTION, SECTION_ID, confident, foundNothing, grepNote, hitsOf, isSearchCommand,
  grepToolFoundNothing, promptContext, readGrepTool, readSearch, resultText, worthAsking,
} from './logic.js'
import { DEFAULTS, EFFORTS, MODELS, PANE, citations, excerpts, note, plan, request, tokensOf } from './chat.js'
import {
  CONFIG_PANE, FOOTER_COLUMNS, HELP_HINT, HELP_KEYS, LABEL_COLUMNS, OPTIONS, VALUE_COLUMNS, commandLine, engineCommand, help,
  nextPoll, partLines, progressOf, progressWord, statusLine, fit, lastLine, machineState, meaning, nextValue, settingKey,
  valueOf, vectorsRow,
} from './configure.js'

const SERVER = 'aleph'
const ENGINE_MS = 400   // what the engine is given to answer
const HOOK_MS = 450     // after which the hook stops waiting, whatever the engine or its transport does
const LAST = 'aleph-zero.last-session'   // where the meter of the latest finished turn is kept

function usage() {
  return { requests: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
}

// This session, since the mod loaded.
const meter = { turns: 0, main: usage(), subagents: usage(), tools: {}, aleph: 0, grep: 0, first: null, added: { prompt: 0, grep: 0 }, listed: [] }
let prompts = 0

// The session's meter, rung 2's one chance and the side chat's conversation, kept in $.state: a changed option loads the
// mod again (its module state starts over), and what it had counted and said would be gone. Read back once a load.
const SESSION_STATE = { plugin: 'aleph-zero', key: 'session' }
let restored = false

async function restore($) {
  if (restored) return
  restored = true
  try {
    const { value } = await $.state.get(SESSION_STATE)
    if (!value || typeof value !== 'object') return
    if (value.meter && typeof value.meter === 'object') Object.assign(meter, value.meter)
    if (typeof value.prompts === 'number') prompts = value.prompts
    if (Array.isArray(value.turns) && !chat.turns.length) chat.turns.push(...value.turns)
    if (typeof value.tokens === 'number') chat.tokens = value.tokens
    if (typeof value.focused === 'string') cfg.focused = value.focused     // the settings row the ring was on
  } catch {
    // nothing kept, or $.state not there: start from nothing, as before
  }
}

async function saveSession($) {
  // A question still being answered when the mod is loaded again is not answered by the new load: kept as stopped.
  const turns = chat.turns.slice(-8).map((turn) => turn.state === 'searching' || turn.state === 'thinking'
    ? { ...turn, state: 'failed', answer: 'Stopped: the plugin was loaded again.' } : { ...turn })
  try {
    await $.state.set(SESSION_STATE, { meter, prompts, turns, tokens: chat.tokens, focused: cfg.focused })
  } catch {
    // the session goes on without it
  }
}

// The engine's server as Claude Code names it. The manifest calls it `aleph`; $.mcp.connect
// takes that name and answers with the full one (`plugin:aleph-zero:aleph`), which is what
// $.mcp.call wants. Found by trying: the short name is refused there.
let server = null
// Where the session was started, as session.start reports it.
let sessionCwd = ''

// The session's directory: session.start's, or asked for when the mod was loaded again without one (a changed
// option reloads it mid-session). Never raises.
async function cwdOf($) {
  if (!sessionCwd) sessionCwd = String((await $.session.cwd().catch(() => '')) || '')
  return sessionCwd
}

async function reach($, tool, args) {
  if (server === null) {
    const connected = await $.mcp.connect(SERVER)
    if (!connected || !connected.isConnected) return null
    server = connected.server
  }
  // The engine answers for the directory it is asked about; without one, for wherever its
  // own process was started. The session may be in a subdirectory or another worktree.
  const cwd = await cwdOf($)
  return $.mcp.call(server, tool, cwd ? { ...args, cwd } : args)
}

// One call to the engine that can never hold up its hook: time inside a mods API call does
// not count against a hook's own limit, so a transport that never answers would hold the
// prompt for ever. $.clock.sleep is the one call that does count. A refusal (the user has
// not allowed the engine's tools) and any other failure are "no answer".
async function ask($, tool, args) {
  const answered = reach($, tool, { ...args, format: 'json', timeout_ms: ENGINE_MS }).catch(() => null)
  const waited = $.clock.sleep(HOOK_MS).then(() => null)
  return Promise.race([answered, waited])
}

// ---- the side chat ---------------------------------------------------------------------------
// A conversation about the code beside the main one. Nothing here reaches the main
// conversation except an explicit insert, which only drafts text into the prompt box.

const CHAT_SETTINGS = 'aleph-zero.chat-settings'   // the pickers, kept between sessions
const CHAT_USE = 'aleph-zero.chat-use'             // how often answers were cited and inserted
const CHAT_ENGINE_MS = 2500                        // the chat may wait longer for the engine than a hook may
const SESSION = 'chat-' + Math.random().toString(16).slice(2, 14)   // names this load of the mod in what it remembers
const chat = { turns: [], busy: null, serial: 0, model: DEFAULTS.model, effort: DEFAULTS.effort, tokens: 0, loaded: false, note: '', draft: '' }

// ---- /aleph configure ------------------------------------------------------------------------
// What the pane last read from the engine. Module state: a changed plugin option loads the mod again, and the pane
// then reads it all again, which is what it should show anyway.
const cfg = { status: undefined, error: '', loading: false, again: false, busy: '', note: '', command: '', removing: null, enginePath: '', draft: '', focused: null }

// The engine's status for the session's directory, and the engine's path for the commands it copies. Never raises.
async function readConfig($) {
  if (cfg.loading) {
    cfg.again = true    // asked for after a change: what is being read may be from before it
    while (cfg.loading) await $.clock.sleep(50)
    return
  }
  cfg.loading = true
  cfg.again = false
  try {
    const cwd = await $.session.cwd()
    const [status, path] = await Promise.all([
      $.process.run(engineCommand($.plugin.root, cwd, ['status']), { timeoutMs: 20000 }),
      cfg.enginePath ? null : $.process.run(['sh', $.plugin.root + '/scripts/engine.sh', '--path'], { timeoutMs: 5000 }).catch(() => null),
    ])
    if (path && path.exitCode === 0) cfg.enginePath = path.stdout.trim()
    if (status.exitCode === 0) {
      const read = JSON.parse(status.stdout)
      // An engine from before 0.1.6 says neither the repository's choice nor what is built: the plugin was updated
      // through Claude Code and the engine was not.
      const older = !read.vectors || !('chosen' in read.vectors) || !read.memory || !('aeon_installed' in read.memory)
      cfg.status = older ? null : read
      cfg.error = older ? 'The engine is older than this plugin: `uv tool install --reinstall '
        + 'git+https://github.com/mustafarslan/aleph-zero.git` in a terminal, then /aleph configure again.' : ''
    } else {
      // A directory the engine refuses (a home directory, more than 20,000 files) is said with what is built on this
      // machine beside it.
      let refused = null
      try {
        refused = JSON.parse(status.stdout)
      } catch {
        refused = null
      }
      cfg.status = refused && refused.refused ? refused : null
      cfg.error = cfg.status ? '' : lastLine(status.stderr) || 'the engine did not answer (exit ' + status.exitCode + ')'
    }
  } catch (error) {
    cfg.status = null
    cfg.error = 'the engine could not be asked: ' + (error && error.message ? error.message : String(error))
  } finally {
    cfg.loading = false
    if (cfg.again) await readConfig($)
    $.ui.invalidate('ui.render')
  }
}

// ---- how far vectors are made ------------------------------------------------------------------
// Read from the running server (fast: it is warm, and it says what its background is doing); where the engine's
// tools are not allowed, from the engine's own command, less often. Started with an interactive session only: a
// headless run has no status line, and its measurements must not carry calls of the plugin's own.
let progress = null          // the last reading (configure.js progressOf)
let progressTimer = null
let fallbackAt = 0           // when the command was last run instead of the server
let quickUntil = 0           // after an action in the settings pane, looked at quickly for a while: the server sees a
                             // linked source only once its kept view of the sources is renewed (up to five seconds)

async function readProgress($) {
  let status = null
  try {
    const answer = await reach($, 'status', { format: 'json', timeout_ms: 2000 })
    status = answer ? JSON.parse(resultText(answer)) : null
  } catch {
    status = null
  }
  if (!status && $.clock.now() - fallbackAt >= 30000) {
    fallbackAt = $.clock.now()
    try {
      const cwd = await cwdOf($)
      const done = await $.process.run(engineCommand($.plugin.root, cwd, ['status']), { timeoutMs: 20000 })
      status = done.exitCode === 0 ? JSON.parse(done.stdout) : null
    } catch {
      status = null
    }
  }
  if (!status) return progress
  const before = progress
  progress = progressOf(status, progress, $.clock.now())
  $.ui.status(statusLine(progress))
  if (!before || before.have !== progress.have || before.active !== progress.active || before.pending !== progress.pending) {
    $.ui.invalidate('ui.render')        // the side chat's header and the settings pane say it too
  }
  return progress
}

function watchProgress($) {
  try {
    if (progressTimer) progressTimer.cancel()
    const quick = $.clock.now() < quickUntil
    progressTimer = $.clock.after(quick ? 5000 : progress ? nextPoll(progress) : 1000, async () => {
      progressTimer = null
      await readProgress($).catch(() => null)
      watchProgress($)
    })
  } catch {
    progressTimer = null      // no clock here (a test, a host without timers): the count is simply not watched
  }
}

// One of the engine's commands for this repository, then the status read again. What it printed last is the note.
async function runEngine($, args, doing) {
  if (cfg.busy) return
  cfg.busy = doing
  cfg.note = ''
  cfg.command = ''
  $.ui.invalidate('ui.render')
  try {
    const cwd = await $.session.cwd()
    const stream = $.process.spawn({ argv: engineCommand($.plugin.root, cwd, args), cwd })
    let said = ''
    let pending = ''    // a piece may end in the middle of a line
    for await (const piece of stream) {
      pending += piece.text
      const cut = pending.lastIndexOf('\n')
      const line = cut < 0 ? '' : lastLine(pending.slice(0, cut))
      if (cut >= 0) pending = pending.slice(cut + 1)
      if (line) {
        said = line
        cfg.busy = doing + ' · ' + line
        $.ui.invalidate('ui.render')
      }
    }
    said = lastLine(pending) || said
    const ended = await stream.result
    cfg.note = ended && ended.code === 0 ? said || 'done' : 'failed: ' + (said || 'exit ' + (ended && ended.code))
  } catch (error) {
    cfg.note = 'failed: ' + (error && error.message ? error.message : String(error))
  } finally {
    await readConfig($)     // still busy until the rows show what the command did
    cfg.busy = ''
    $.ui.invalidate('ui.render')
    // A linked source or a switch to search by meaning brings vectors to make: look now, not at the next slow poll.
    if (progressTimer) {
      try {
        quickUntil = $.clock.now() + 30000
      } catch {
        quickUntil = 0
      }
      await readProgress($).catch(() => null)
      watchProgress($)
    }
  }
}

// One engine call for the chat: as in `ask`, never longer than its limit, and null for any failure.
async function lookup($, tool, args) {
  const answered = reach($, tool, { ...args, format: 'json', timeout_ms: CHAT_ENGINE_MS - 500 }).catch(() => null)
  const waited = $.clock.sleep(CHAT_ENGINE_MS).then(() => null)
  return Promise.race([answered, waited])
}

function symbolsOf(answer) {
  try {
    const parsed = JSON.parse(resultText(answer))
    return Array.isArray(parsed.symbols) ? parsed.symbols : []
  } catch {
    return []
  }
}

async function count($, field) {
  // A key of its own for each counter, read just before writing: every session on the machine
  // shares the store. A count that cannot be kept must never cost the user an answer.
  try {
    const key = CHAT_USE + '.' + field
    await $.store.set(key, Number((await $.store.get(key)) || 0) + 1)
  } catch {
    // the usefulness log is best effort
  }
}

// Remember something about this repository for later sessions, through the engine. Best
// effort: a memory that cannot be kept never costs the answer or the insert it came from.
async function keep($, kind, text, cited) {
  await lookup($, 'remember', { kind, text, cites: cited.join(','), session: SESSION })
}

function finish(turn, state, answer) {
  turn.state = state
  turn.answer = answer
  if (chat.busy === turn) chat.busy = null
}

// One question: retrieval through the engine (no model), then one model call over the
// excerpts. Whatever happens, the pane is left usable: the turn ends answered or failed.
async function answerQuestion($, question) {
  const serial = chat.serial + 1
  chat.serial = serial
  const turn = { question, answer: '', cited: [], tokens: 0, state: 'searching' }
  chat.turns.push(turn)
  chat.busy = turn
  chat.note = ''
  $.ui.invalidate('ui.render')
  try {
    const earlier = chat.turns.slice(0, -1).filter((past) => past.state === 'answered')
    const wanted = plan(question, earlier.length ? earlier[earlier.length - 1].question : null)
    const [searched, ...named] = await Promise.all([
      lookup($, 'search', { query: wanted.search, k: 6 }),
      ...wanted.symbols.map((name) => lookup($, 'symbol', { name, k: 3 })),
    ])
    if (chat.serial !== serial) return  // stopped
    if (searched === null) {
      // No model call for a question the index could not even be asked: it would cost
      // tokens to be told that nothing was found, without saying why.
      finish(turn, 'failed', 'The index did not answer. It may still be starting, or its tools are not allowed for the plugin: `/aleph check` says which.')
      return
    }
    const found = excerpts(hitsOf(searched), named.flatMap(symbolsOf))
    if (!found.length) {
      finish(turn, 'failed', 'Nothing in the index matches that. Try the words the code would use, or a name.')
      return
    }
    turn.state = 'thinking'
    $.ui.invalidate('ui.render')
    const asked = request(question, found, earlier)
    let reply
    try {
      reply = await $.model.complete({
        model: chat.model === 'session' ? await $.session.model() : chat.model,
        effort: chat.effort,
        prompt: [{ text: asked.rules, cache: true }, { text: asked.body }],
        maxTokens: 1200,
        timeoutMs: 90000,
      })
    } catch (error) {
      reply = { isAnswered: false, reason: error && error.message ? error.message : String(error) }
    }
    turn.tokens = tokensOf(reply.usage)
    chat.tokens += turn.tokens   // also for a question that was stopped: it was paid for
    if (chat.serial !== serial) return
    if (reply.isAnswered) {
      turn.cited = citations(reply.text, found)
      finish(turn, 'answered', reply.text)
      $.ui.invalidate('ui.render')  // the answer shows before anything else is done with it
      await count($, 'answers')
      if (turn.cited.length) await count($, 'cited')
      await keep($, 'chat', 'Q: ' + String(question).slice(0, 2000) + '\nA: ' + reply.text, turn.cited)
    } else {
      finish(turn, 'failed', 'No answer: ' + (reply.reason || 'the model did not answer'))
    }
  } catch (error) {
    if (chat.serial === serial) finish(turn, 'failed', 'The question could not be answered: ' + (error && error.message ? error.message : String(error)))
  } finally {
    if (chat.busy === turn) chat.busy = null
    $.ui.invalidate('ui.render')
    await saveSession($)
  }
}

function add(into, counted) {
  if (!counted) return
  into.requests += 1
  into.input += counted.input_tokens || 0
  into.output += counted.output_tokens || 0
  into.cacheRead += counted.cache_read_input_tokens || 0
  into.cacheWrite += counted.cache_creation_input_tokens || 0
}

function total(counted) {
  return counted.input + counted.output + counted.cacheRead + counted.cacheWrite
}

function report(rung, meter) {
  const tools = Object.keys(meter.tools).sort().map((name) => name + ' ' + meter.tools[name]).join(', ')
  const perTurn = meter.turns ? Math.round(total(meter.main) / meter.turns) : 0
  return 'rung ' + rung + ' · turns ' + meter.turns
    + ' · tokens ' + total(meter.main) + ' (' + perTurn + ' a turn; input ' + meter.main.input + ', output ' + meter.main.output
    + ', cache read ' + meter.main.cacheRead + ', cache write ' + meter.main.cacheWrite + ') in ' + meter.main.requests + ' requests'
    + ' · subagents ' + total(meter.subagents) + ' tokens in ' + meter.subagents.requests + ' requests'
    + ' · aleph calls ' + meter.aleph + ' · grep calls ' + meter.grep
    + (meter.first ? ' · first to be used: ' + meter.first : '')
    + ' · locations added to ' + meter.added.prompt + ' prompts and ' + meter.added.grep + ' empty greps'
    + (tools ? ' · tools: ' + tools : '')
}

export function register(on, options) {
  const rung = Number(options && options.rung !== undefined ? options.rung : 1)
  // The pointer in the descriptions of Grep and Glob is part of rung 1 but has its own switch,
  // so a measurement can tell it from the rest of the rung.
  const pointer = !(options && options.grep_pointer === 'off')
  // Rung 2's lines: places only, or with the signature of each file's first result (an option, for a paid comparison).
  const signatures = Boolean(options && options.lines === 'signatures')
  // The graph tools are offered unless switched off (the manifest's default is on; the engine reads the same option).
  const graph = !(options && /^(off|0|false|no)$/i.test(String(options.graph || '').trim()))
  const section = graph ? SECTION + '\n' + GRAPH_LINE : SECTION

  on('session.start', async ($, e, next) => {
    if (e && typeof e.cwd === 'string') sessionCwd = e.cwd
    if (e && e.isInteractive) watchProgress($)
    await $.command.register({
      name: 'aleph',
      description: 'Aleph Zero: with no argument, opens the side chat about this repository. `configure` opens its settings: the plugin\'s options, this repository\'s search by meaning and linked sources, what is built on this machine. `stats` shows this session\'s tokens, tool calls and how often search was used instead of grep; `stats last` shows the previous session\'s; `check` says whether the mod can reach the engine',
      argumentHint: '[configure | stats [last] | check [model]]',
      immediate: true,
    })
    return next(e)
  })

  on('command.run', { command: 'aleph' }, async ($, e) => {
    await restore($)
    const args = String(e.args || '').trim()
    if (!args || args === 'chat') {
      // Open the side chat. Nothing is printed, and nothing enters the conversation.
      if (!chat.loaded) {
        const kept = await $.store.get(CHAT_SETTINGS)
        if (kept && typeof kept === 'object') {
          if (MODELS.some((choice) => choice.value === kept.model)) chat.model = kept.model
          if (EFFORTS.some((choice) => choice.value === kept.effort)) chat.effort = kept.effort
        }
        chat.loaded = true
      }
      const opened = await $.ui.open({ id: PANE, title: 'Aleph side chat', focus: true, closeOnEscape: true, columns: 64 })
      if (opened && opened.isPlaced === false) {
        return { text: 'The side chat could not be opened here: ' + (opened.reason || 'this surface draws no panes') }
      }
      return {}
    }
    if (args === 'configure' || args === 'config') {
      cfg.note = ''
      cfg.command = ''
      cfg.removing = null
      void readConfig($)
      const opened = await $.ui.open({ id: CONFIG_PANE, title: 'Aleph configure', focus: true, closeOnEscape: true, columns: 64 })
      if (opened && opened.isPlaced === false) {
        return { text: 'The settings could not be opened here: ' + (opened.reason || 'this surface draws no panes') }
      }
      return {}
    }
    if (!['stats', 'stats last', 'check', 'check model'].includes(args)) {
      // A question typed after the command would be answered with a report, and the row
      // would enter the conversation. Questions go into the pane.
      return { text: 'Type the question in the pane: `/aleph` opens it. Arguments are `configure`, `stats`, `stats last`, `check` and `check model`.' }
    }
    if (args === 'stats last') {
      // The session before this one (any session on this machine): a headless run cannot be asked afterwards.
      const kept = await $.store.get(LAST)
      return { text: kept ? 'last session · ' + report(kept.rung, kept.meter) : 'no session has been recorded yet' }
    }
    if (args === 'check model') {
      // One small real request in the shape the side chat sends (it costs a fraction of a cent):
      // says whether this build accepts it, without printing anything the model wrote.
      try {
        const reply = await $.model.complete({
          model: chat.model === 'session' ? await $.session.model() : chat.model,
          effort: chat.effort,
          prompt: [{ text: 'Reply with the single word ok.', cache: true }, { text: 'Question: are you there?' }],
          maxTokens: 20,
          timeoutMs: 30000,
        })
        return {
          text: reply.isAnswered
            ? 'the side chat can call the model (' + chat.model + ', effort ' + chat.effort + ', ' + tokensOf(reply.usage) + ' tokens)'
            : 'the model did not answer the side chat: ' + reply.reason,
        }
      } catch (error) {
        return { text: 'the side chat\'s request was refused: ' + (error && error.message ? error.message : String(error)) }
      }
    }
    if (args === 'check') {
      // Can the mod reach the engine at all? Says what came back, or why nothing did.
      try {
        const answer = await reach($, 'search', { query: 'check', k: 1, format: 'json', timeout_ms: 2000 })
        if (answer === null) {
          return {
            text: 'the engine is not connected: Claude Code did not start the plugin\'s MCP server. Most often the engine '
              + 'is not installed: `uv tool install git+https://github.com/mustafarslan/aleph-zero.git`, then restart '
              + 'Claude Code. `/mcp` shows the server\'s own error.',
          }
        }
        return { text: 'the engine answers the mod (' + hitsOf(answer).length + ' hits for a test query), as ' + server }
      } catch (error) {
        const why = (error && error.message ? error.message : String(error)).replace(/\.+$/, '')
        return {
          text: 'the mod cannot use the engine: ' + why + '. Rungs 2 and 3 need the engine\'s tools to be allowed: '
            + 'add "mcp__plugin_aleph-zero_aleph__*" to permissions.allow in your settings.',
        }
      }
    }
    return { text: report(rung, meter) }
  })

  // ---- the side chat's pane ------------------------------------------------------------------

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e)
    await restore($)
    const { Box, Text, Button, Input, Markdown } = $.ui.resolve(e)
    const props = e.props || {}
    // Docked beside the transcript, the pane is a conversation: exactly the window's height, the newest message at the
    // bottom of the message area (older ones clipped above it), the question field and its keys under it, as in a
    // messaging app and in Claude Code itself. Above the prompt the frame fits the tree, so it shows the last two turns.
    const docked = props.placement === 'dock' && Number(props.scroll && props.scroll.bodyRows) > 0
    const columns = Number(props.bodyColumns) || 60
    const last = chat.turns.length ? chat.turns[chat.turns.length - 1] : null
    const label = (MODELS.find((item) => item.value === chat.model) || { label: chat.model }).label
    // Chrome in the light tone, so the conversation is the brightest thing in the pane; the model and effort are on
    // their own keys below.
    const making = progressWord(progress)
    const header = Text({ key: 'header', color: 'subtle', children: ['Aleph side chat · ' + chat.tokens + ' tokens' + (making ? ' · vectors ' + making : '')] })

    const turns = chat.turns.slice(docked ? -8 : -2).flatMap((turn, index) => [
      Text({ key: 'q' + index, weight: 'semibold', children: ['> ' + turn.question] }),
      turn.state === 'searching' || turn.state === 'thinking'
        ? Text({ key: 'busy' + index, color: 'inactive', children: [turn.state === 'searching' ? '✻ Searching the index…' : '✻ Thinking…'] })
        : Box({
          key: 'a' + index, flexDirection: 'row',
          children: [Text({ color: turn.state === 'failed' ? 'warning' : 'claude', children: ['⏺'] }), Box({ flexDirection: 'column', flexShrink: 1, marginLeft: 1, children: [Markdown({ text: turn.answer })] })],
        }),
      ...(turn.state === 'answered'
        // An answer that cites nothing is unverified: said in the warning tone, so it reads apart from a cited one.
        ? [Text({ key: 'm' + index, color: turn.cited.length ? 'inactive' : 'warning', children: ['  ↳ ' + (turn.cited.length ? 'cites ' + turn.cited.join(', ') : 'no location cited') + ' · ' + turn.tokens + ' tokens'] })]
        : []),
    ])
    const empty = [
      Text({ key: 'hello', color: 'inactive', children: ['Ask about this repository: where something is handled, how it works, what its documentation says. Answers cite path:line.'] }),
      Text({ key: 'hello-keys', color: 'subtle', children: ['ctrl+x tab moves between the conversation and this pane; /aleph opens it again; /aleph configure sets Aleph up. Answers are kept for later sessions: `aleph-zero memory list`, and `forget`.'] }),
    ]
    const messages = Box({
      key: 'messages', flexDirection: 'column', rowGap: 1,
      ...(docked ? { flexGrow: 1, justifyContent: 'flex-end', overflow: 'hidden' } : {}),
      children: [...(turns.length ? turns : empty), ...(chat.note ? [Text({ key: 'note', color: 'subtle', children: [chat.note] })] : [])],
    })

    // The pane's keys, pressed while the pane (not the question field) has the keyboard: Esc leaves the field, Tab
    // walks back to it.
    const step = (list, value) => list[(list.findIndex((item) => item.value === value) + 1) % list.length].value
    const choose = async (what, value) => {
      chat[what] = value
      $.ui.invalidate('ui.render')
      await $.store.set(CHAT_SETTINGS, { model: chat.model, effort: chat.effort })
    }
    const keys = []
    const actions = {}     // the same keys, for a letter typed into the question field
    if (chat.busy) {
      keys.push(Button({
        key: 'stop', label: 'stop', hotkey: 's', plain: true,
        onPress: actions.s = () => {
          chat.serial += 1  // whatever comes back for the question in flight is dropped
          if (chat.busy) finish(chat.busy, 'failed', 'Stopped.')
          $.ui.invalidate('ui.render')
        },
      }))
    } else if (last && last.state === 'answered') {
      keys.push(Button({
        key: 'insert', label: 'insert', hotkey: 'i', plain: true,
        onPress: actions.i = async () => {
          // Drafts the note into the prompt box. Nothing reaches Claude until the user sends it.
          const filled = await $.prompt.fill({ text: note(last.answer, last.cited), mode: 'insert' })
          chat.note = filled && filled.isFilled === false ? 'The prompt box could not be filled.' : 'Drafted into the prompt box; edit or send it.'
          $.ui.invalidate('ui.render')  // the note shows at once; remembering may take a moment
          await count($, 'inserted')
          await keep($, 'note', last.answer, last.cited)
        },
      }))
    }
    keys.push(
      Button({ key: 'model-next', label: 'model: ' + label.replace(/ \(.*\)$/, ''), hotkey: 'm', plain: true, onPress: actions.m = () => choose('model', step(MODELS, chat.model)) }),
      Button({ key: 'effort-next', label: 'effort: ' + chat.effort, hotkey: 'e', plain: true, onPress: actions.e = () => choose('effort', step(EFFORTS, chat.effort)) }),
      Button({ key: 'close', label: 'close', hotkey: 'c', plain: true, onPress: actions.c = () => $.ui.close({ id: PANE }) }),
    )
    const question = Input({
      // Drawn with what was typed so far: the pane is redrawn while an answer is on its way,
      // and a field drawn empty would wipe a half-typed question.
      key: 'question', placeholder: '❯ Ask about this repository', value: chat.draft, submitLabel: 'ask', autoFocus: true,
      onInput: (value) => {
        chat.draft = String(value || '')
      },
      onSubmit: async (value) => {
        const asked = String(value || '').trim()
        if (!asked) return
        // A key typed while the field had the keyboard (seen in use: "m" was asked as a question): do what it does.
        const action = asked.length === 1 ? actions[asked.toLowerCase()] : undefined
        if (action) {
          chat.draft = ''
          await action()
          return
        }
        if (chat.busy) {
          chat.draft = asked
          chat.note = 'The last question is still being answered: Esc, then s stops it; or wait, then Enter again.'
          $.ui.invalidate('ui.render')
          return
        }
        chat.draft = ''
        await answerQuestion($, asked)
      },
    })
    return Box({
      key: 'chat', flexDirection: 'column', rowGap: docked ? 0 : 1,
      ...(docked ? { height: Number(props.scroll.bodyRows) } : {}),
      children: [
        header,
        messages,
        Text({ key: 'rule', color: 'subtle', children: ['─'.repeat(Math.max(10, columns))] }),
        question,
        Box({ key: 'footer', flexDirection: 'row', columnGap: 2, children: [Box({ key: 'footer-hint-box', flexShrink: 0, children: [Text({ key: 'footer-hint', color: 'subtle', children: ['Esc, then:'] })] }), ...keys] }),
      ],
    })
  })

  // ---- /aleph configure's pane -----------------------------------------------------------------

  // The row the focus ring is on, for the help under the rows. Only a move to another element redraws.
  on('ui.focus', async ($, e, next) => {
    // The person's moves name the element (`element`); the plugin's own $.ui.focus arrives as asked (`key`).
    const element = e.element || e.key || null
    if (e.requestId === CONFIG_PANE && element !== cfg.focused) {
      cfg.focused = element
      $.ui.invalidate('ui.render')
    }
    return next(e)
  }).catch(async ($, e, next) => next(e))      // a fault here must never keep the ring from moving

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== CONFIG_PANE) return next(e)
    await restore($)     // after an option changed: the row the ring is on, so its help stays
    const { Box, Text, Button, Input, Markdown } = $.ui.resolve(e)
    if (cfg.status === undefined && !cfg.loading) void readConfig($)   // loaded again after an option changed
    const props = e.props || {}
    const columns = Math.max(30, Number(props.bodyColumns) || 60)
    const docked = props.placement === 'dock' && Number(props.scroll && props.scroll.bodyRows) > 0
    const rows = []
    // Tones by role, theme keys only (light and dark themes, four surfaces): headings strong, chrome subtle, what a
    // row means inactive, an off value receding, a warning in its own colour.
    const heading = (key, text, first) => rows.push(Box({ key: key + '-box', ...(first ? {} : { marginTop: 1 }), children: [Text({ key, weight: 'semibold', children: [text] })] }))
    const tone = (value) => /^(off|not installed|unknown)/.test(String(value)) ? { color: 'inactive' }
      : /out of date/.test(String(value)) ? { color: 'warning' } : { weight: 'medium' }
    // One line a row: the key and name in a column of their own, the value in one too, what it means cut to what fits.
    const row = (key, control, value, detail) => rows.push(Box({
      key, flexDirection: 'row', columnGap: 1,
      children: [
        Box({ key: key + '-k', width: LABEL_COLUMNS, flexShrink: 0, children: [control] }),
        Box({ key: key + '-vb', width: VALUE_COLUMNS, flexShrink: 0, children: [Text({ key: key + '-v', wrap: 'truncate-end', ...tone(value), children: [value] })] }),
        ...(detail ? [Text({ key: key + '-d', color: 'inactive', wrap: 'truncate-end', children: [detail] })] : []),
      ],
    }))
    const say = (key, text) => rows.push(Text({ key, color: 'subtle', children: [fit(text, columns)] }))
    const tell = (key, text, color = 'subtle') => rows.push(Text({ key, color, children: [String(text)] }))   // whole, wrapped: a reason

    // The plugin's options: through Claude Code's settings, as /plugin would; they need neither the engine nor its tools.
    heading('h-plugin', 'Plugin', true)
    for (const option of OPTIONS) {
      const value = valueOf(options, option)
      row('o-' + option.key, Button({
        key: 'b-' + option.key, label: option.label, hotkey: option.hotkey, plain: true,
        onPress: async () => {
          // A changed option loads the mod again, which ends a command still running (a clone): not while one runs.
          if (cfg.busy) {
            cfg.note = 'wait for ' + cfg.busy.split(' · ')[0] + ' to finish'
            $.ui.invalidate('ui.render')
            return
          }
          const to = nextValue(option, value)
          await saveSession($)   // the change loads the mod again: what this load counted and said goes with it
          const set = await $.config.set({ key: settingKey(option), value: to }).catch((error) => ({ deny: error && error.message ? error.message : String(error) }))
          // Written, the mod is loaded again with the new value and draws this row itself; refused, it says why.
          if (set && set.deny) {
            cfg.note = option.label + ' was not changed: ' + set.deny
            $.ui.invalidate('ui.render')
          }
        },
      }), value, meaning(option, value))
    }

    // This repository: the engine's settings for it and its linked sources.
    const status = cfg.status
    heading('h-repo', 'Repository' + (status && status.root ? ' · ' + fit(String(status.root).split('/').filter(Boolean).pop() || status.root, columns - 13) : ''))
    const removable = []
    if (status === undefined || (status === null && cfg.loading)) {
      say('r-reading', 'reading the engine\'s status…')
    } else if (status === null) {
      tell('r-error', cfg.error, 'error')
    } else if (status.refused) {
      tell('r-refused', status.refused)
    } else if (status.repository === false) {
      say('r-plain', 'not a git repository: search by meaning, linked sources and git\'s cache are set in one')
    } else {
      const vectors = vectorsRow(status.vectors, status.main_language)
      row('o-dense', Button({ key: 'b-dense', label: 'search by meaning', hotkey: 'd', plain: true, onPress: () => runEngine($, ['setup', vectors.flag], 'switching search by meaning') }),
        vectors.value, progressWord(progress) || vectors.note)
      const cached = Boolean(status.untracked_cache)
      // Kept by git's configuration, the cache comes back whatever the index says: no key, and where it is set.
      if (status.untracked_cache_from === 'config') row('o-cache', Text({ key: 'b-cache-config', color: 'subtle', children: ['   untracked cache'] }), 'on', 'set in git\'s configuration')
      else row('o-cache', Button({
        key: 'b-cache', label: 'untracked cache', hotkey: 'u', plain: true,
        onPress: async () => {
          if (cfg.busy) return
          cfg.busy = 'changing git\'s untracked cache'
          $.ui.invalidate('ui.render')
          try {
            const done = await $.process.run(['git', 'update-index', cached ? '--no-untracked-cache' : '--untracked-cache'], { cwd: await $.session.cwd(), timeoutMs: 60000 })
            cfg.note = done.exitCode === 0 ? lastLine(done.stderr) || 'git\'s untracked cache is ' + (cached ? 'off' : 'on') : 'git: ' + lastLine(done.stderr)
          } catch (error) {
            cfg.note = 'git could not be run: ' + (error && error.message ? error.message : String(error))
          } finally {
            await readConfig($)
            cfg.busy = ''
            $.ui.invalidate('ui.render')
          }
        },
      }), cached ? 'on' : 'off', 'a faster look at the worktree')
      const linked = Array.isArray(status.sources) ? status.sources : []
      say('s-head', linked.length ? 'Linked sources (a digit unlinks)' : 'No linked sources')
      linked.slice(0, 9).forEach((source, index) => {
        const name = String(source.name)
        removable.push(name)
        row('src-' + index, Button({
          key: 'b-src-' + index, label: name, hotkey: String(index + 1), plain: true, dimColor: true,
          onPress: () => {
            cfg.removing = name
            $.ui.invalidate('ui.render')
          },
        }), (source.files || 0) + (source.files === 1 ? ' file' : ' files'), String(source.location || '') + (source.last_error ? ' · ' + source.last_error : ''))
      })
      if (cfg.removing && removable.includes(cfg.removing)) {
        const name = cfg.removing
        rows.push(Box({
          key: 'confirm', flexDirection: 'row', columnGap: 2,
          children: [
            Text({ key: 'confirm-q', color: 'warning', children: ['Unlink ' + name + '? Its files leave the index.'] }),
            Button({ key: 'b-yes', label: 'unlink', hotkey: 'y', plain: true, onPress: () => { cfg.removing = null; return runEngine($, ['source', 'remove', name], 'unlinking ' + name) } }),
            Button({ key: 'b-no', label: 'keep', hotkey: 'n', plain: true, onPress: () => { cfg.removing = null; $.ui.invalidate('ui.render') } }),
          ],
        }))
      }
      // The field is not focused on opening, or the keys above would be typed into it; a moves the focus onto it (Tab
      // leaves the pane for the prompt), Esc back out of it.
      const field = Input({
        key: 'source', placeholder: 'a path or a git URL; Enter links it', value: cfg.draft, submitLabel: 'link',
        onInput: (value) => { cfg.draft = String(value || '') },
        onSubmit: async (value) => {
          const location = String(value || '').trim()
          if (!location || cfg.busy) return
          cfg.draft = ''
          await runEngine($, ['source', 'add', location], 'linking ' + location + (/^[a-z]+:\/\/|^git@/.test(location) ? ' (cloned first)' : ''))
        },
      })
      rows.push(Box({
        key: 'o-link', flexDirection: 'row', columnGap: 1,
        children: [
          Box({ key: 'o-link-k', width: LABEL_COLUMNS, flexShrink: 0, children: [Button({
            key: 'b-link', label: 'link a source', hotkey: 'a', plain: true,
            onPress: () => $.ui.focus({ requestId: CONFIG_PANE, key: 'source' }).catch(() => undefined),
          })] }),
          Box({ key: 'o-link-f', flexGrow: 1, children: [field] }),
        ],
      }))
    }

    // This machine: what is built once. A build takes minutes and must not die with the pane, nor run as the model's
    // tool: its command is copied for a terminal, and shown whole.
    heading('h-machine', 'Machine')
    if (status) {
      const vectors = status.vectors || {}
      const memories = status.memory || {}
      const machine = [
        ['worker', 'w', 'embedding worker', machineState(vectors.worker_installed, vectors.worker_current), '310 MB and a build', 'setup --worker'],
        ['memory', 'm', 'memory (Aeon)', machineState(memories.aeon_installed, memories.aeon_current), 'a build of a minute', 'setup --memory'],
      ]
      for (const [key, hotkey, label, state, cost, args] of machine) {
        const wanted = state !== 'installed' && state !== 'unknown'
        row('m-' + key, Button({
          key: 'b-' + key, label, hotkey, plain: true,
          onPress: async (press) => {
            cfg.command = commandLine(cfg.enginePath, args)
            const copied = await $.ui.copy({ text: cfg.command, ...(press && press.surface ? { surface: press.surface } : {}) }).catch(() => null)
            cfg.note = copied && copied.isCopied ? 'Copied; run it in a terminal (it goes on if this pane closes):' : 'Run this in a terminal:'
            $.ui.invalidate('ui.render')
          },
        }), state, wanted ? cost : '')
      }
    } else {
      say('m-wait', status === null ? 'what is built is known once the engine answers' : 'reading…')
    }

    if (cfg.busy) rows.push(Box({ key: 'busy-box', marginTop: 1, children: [Text({ key: 'busy', color: 'inactive', children: [fit('✻ ' + cfg.busy, columns)] })] }))
    else if (cfg.note) rows.push(Box({ key: 'note-box', marginTop: 1, children: [Text({ key: 'note', color: /^failed|^git:|could not/.test(cfg.note) ? 'error' : 'subtle', children: [cfg.note] })] }))
    if (!cfg.busy && cfg.command) rows.push(Text({ key: 'command', mono: true, children: [cfg.command] }))   // whole, to select by hand too

    // The help for the row the ring is on, in one fixed place under all the rows: only its words change as the ring
    // moves. A rule with the row's name, what it does in light Markdown, its values with the current one marked, when
    // a change applies, and a word of warning where one is due.
    const about = help(cfg.focused, { options, status, sources: status && Array.isArray(status.sources) ? status.sources : [] })
    // Under "search by meaning": how far each part is, from the latest reading (the server's, fresher than the pane's).
    const parts = cfg.focused === 'b-dense' ? partLines((progress && progress.parts.length ? progress.parts : (status && status.vectors && status.vectors.parts)) || []) : []
    const width = Math.max(10, columns - 1)
    const helpRows = about ? [
      Text({ key: 'help-rule', color: 'subtle', children: [fit('── ' + about.title + ' ' + '─'.repeat(width), width)] }),
      Markdown({ key: 'help-text', dimColor: true, text: about.text }),
      ...(about.values || []).map((item, index) => Box({
        key: 'help-v' + index, flexDirection: 'row',
        children: [
          Box({ key: 'help-v' + index + '-m', width: 2, flexShrink: 0, children: [Text({ color: item.current ? 'claude' : 'subtle', children: [item.current ? '▸' : ' '] })] }),
          Box({ key: 'help-v' + index + '-n', width: 12, flexShrink: 0, children: [Text({ ...(item.current ? { weight: 'medium' } : { color: 'subtle' }), children: [item.value] })] }),
          Box({ key: 'help-v' + index + '-t', flexShrink: 1, children: [Markdown({ dimColor: !item.current, text: item.meaning })] }),
        ],
      })),
      ...parts.map((part, index) => Box({
        key: 'help-p' + index, flexDirection: 'row',
        children: [
          Box({ key: 'help-p' + index + '-n', width: 14, flexShrink: 0, children: [Text({ color: 'subtle', wrap: 'truncate-end', children: [part.name] })] }),
          Text({ color: part.done ? 'subtle' : 'text', children: [part.text] }),
        ],
      })),
      ...(progress && progress.waiting && cfg.focused === 'b-dense' ? [Text({ key: 'help-waiting', color: 'warning', children: ['Waiting: ' + (progress.reason || 'the worker is not answering')] })] : []),
      Markdown({ key: 'help-applies', dimColor: true, text: '*' + about.applies + '*' }),
      ...(about.note ? [Text({ key: 'help-note', color: about.note[1] || 'subtle', children: [about.note[0]] })] : []),
    ] : [
      Text({ key: 'help-rule', color: 'subtle', children: ['─'.repeat(width)] }),
      Markdown({ key: 'help-hint', dimColor: true, text: HELP_HINT }),
      Text({ key: 'help-keys', color: 'subtle', children: [HELP_KEYS] }),
    ]
    return Box({
      key: 'configure', flexDirection: 'column',
      ...(docked ? { height: Number(props.scroll.bodyRows) } : {}),
      children: [
        Box({ key: 'rows', flexDirection: 'column', flexShrink: 0, children: rows }),
        ...(docked ? [Box({ key: 'spacer', flexGrow: 1 })] : []),
        Box({ key: 'help', flexDirection: 'column', flexShrink: 0, marginTop: 1, children: helpRows }),
        Box({ key: 'footer-box', marginTop: 1, children: [Text({ key: 'footer', color: 'subtle', children: [fit('↑↓ explains a row · a key changes it · Esc closes', FOOTER_COLUMNS)] })] }),
      ],
    })
  })

  // ---- the meter ------------------------------------------------------------------------------

  on('turn.step', async function* ($, e, next) {
    await restore($)
    const result = yield* next(e)
    add(e.agentId ? meter.subagents : meter.main, result && result.usage)
    return result
  })

  on('turn.complete', async ($, e, next) => {
    await restore($)
    if (!e.agentId) {
      meter.turns += 1
      await $.store.set(LAST, { rung, meter })
      await saveSession($)
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    // The mod's own questions to the engine arrive here as tool calls too. They are not the
    // agent's: only calls Claude Code itself fires are counted.
    if (next.origin && next.origin.plugin !== 'engine') return next(e)
    await restore($)
    meter.tools[e.tool] = (meter.tools[e.tool] || 0) + 1
    const kind = ENGINE_TOOL.test(e.tool) ? 'aleph'
      : (e.tool === 'Grep' || e.tool === 'Glob' || (e.tool === 'Bash' && isSearchCommand(e.command))) ? 'grep' : null
    if (kind) {
      meter[kind] += 1
      if (meter.first === null) meter.first = kind
    }
    return next(e)
  }).catch(async ($, e, next) => next(e))

  // ---- L1: the tools are present and described ---------------------------------------------

  on('tool.describe', { tool: /^mcp__plugin_aleph-zero_aleph__/ }, async ($, e, next) => {
    const described = await next(e)
    if (rung < 1) return described
    const name = e.tool.slice(e.tool.lastIndexOf('__') + 2)
    // The four tools for finding code. The engine's memory tools stay on demand.
    if (!DESCRIPTIONS[name]) return described
    return { description: DESCRIPTIONS[name], isDeferred: false }
  })

  on('tool.describe', { tool: ['Grep', 'Glob'] }, async ($, e, next) => {
    const described = await next(e)
    if (rung < 1 || !pointer || !described || typeof described.description !== 'string') return described
    return { ...described, description: described.description + (POINTERS[e.tool] || '') }
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (rung < 1 || !composed || !Array.isArray(composed.sections)) return composed
    // The same text in every request of every session: it must not break the prompt cache.
    return { ...composed, sections: [...composed.sections, { id: SECTION_ID, text: section, scope: 'session' }] }
  })

  // ---- L2: ranked locations with the first prompt of a session ------------------------------

  on('prompt.submit', async ($, e, next) => {
    // The one chance goes to the first prompt that is a question: "hi" does not use it up.
    await restore($)
    if (rung < 2 || prompts > 0 || !worthAsking(e.text)) return next(e)
    prompts += 1
    // Code only, and enough hits to fill five files: a bug report's words match the issue template it was written on,
    // and documentation took two places of five (ADR-011, the reading after the run of 2026-10-13).
    const hits = hitsOf(await ask($, 'search', { query: e.text.slice(0, 2000), k: 15, kind: 'code' }))
    if (!confident(hits)) return next(e)
    meter.added.prompt += 1
    meter.listed = [...new Set(hits.map((hit) => hit.path))].slice(0, 5)  // the files of the lines, as locations() takes them
    return next({ ...e, context: [...(e.context ?? []), promptContext(hits, signatures)] })
  }).catch(async ($, e, next) => next(e))

  on('session.end', async ($, e, next) => {
    await restore($)
    prompts = 0  // /clear, /resume and /branch end one conversation and start another
    await saveSession($)
    return next(e)
  })

  // ---- L3: a recursive grep that found nothing -----------------------------------------------

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    if (rung < 3) return result
    const read = readSearch(e.command)
    if (!read || !foundNothing(result, read.program)) return result
    await restore($)
    const pattern = read.query
    const hits = hitsOf(await ask($, 'search', { query: pattern, k: 5 }))
    if (!hits.length) return result
    meter.added.grep += 1
    const note = grepNote(pattern, hits)
    return { ...result, text: (result.text || '') + note, result: { ...result.result, stdout: (result.result.stdout || '') + note } }
  }).catch(async ($, e, next) => next(e))

  // The Grep tool is the recursive search an agent uses most; its result keeps its own shape, and the note
  // reaches the model as the call's context, after the result.
  on('tool.call', { tool: 'Grep' }, async ($, e, next) => {
    const result = await next(e)
    if (rung < 3) return result
    const read = readGrepTool(e, await cwdOf($))
    if (!read || !grepToolFoundNothing(result)) return result
    await restore($)
    const hits = hitsOf(await ask($, 'search', { query: read.query, k: 5 }))
    if (!hits.length) return result
    meter.added.grep += 1
    return { ...result, context: [...(result.context ?? []), grepNote(read.query, hits, 'search').trim()] }
  }).catch(async ($, e, next) => next(e))
}
