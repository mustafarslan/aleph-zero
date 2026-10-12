// Aleph Zero's steering mod. It holds no retrieval logic: everything it knows about the
// code comes from the engine, through $.mcp.call. Each rung is a setting, so a measurement
// can switch them one at a time (docs/adr/ADR-011):
//   0  the engine's tools as they are
//   1  the tools loaded up front with tuned descriptions, and a short standing instruction
//   2  ranked locations ride along with the first prompt of a session
//   3  a recursive grep that found nothing gets ranked locations appended
// The meter runs on every rung.
import {
  DESCRIPTIONS, ENGINE_TOOL, POINTERS, SECTION, SECTION_ID, confident, foundNothing, grepNote, hitsOf, isSearchCommand,
  promptContext, readSearch, resultText, worthAsking,
} from './logic.js'
import { DEFAULTS, EFFORTS, MODELS, PANE, citations, excerpts, note, plan, request, tokensOf } from './chat.js'

const SERVER = 'aleph'
const ENGINE_MS = 400   // what the engine is given to answer
const HOOK_MS = 450     // after which the hook stops waiting, whatever the engine or its transport does
const LAST = 'aleph-zero.last-session'   // where the meter of the latest finished turn is kept

function usage() {
  return { requests: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
}

// This session, since the mod loaded.
const meter = { turns: 0, main: usage(), subagents: usage(), tools: {}, aleph: 0, grep: 0, first: null, added: { prompt: 0, grep: 0 } }
let prompts = 0

// The engine's server as Claude Code names it. The manifest calls it `aleph`; $.mcp.connect
// takes that name and answers with the full one (`plugin:aleph-zero:aleph`), which is what
// $.mcp.call wants. Found by trying: the short name is refused there.
let server = null

async function reach($, tool, args) {
  if (server === null) {
    const connected = await $.mcp.connect(SERVER)
    if (!connected || !connected.isConnected) return null
    server = connected.server
  }
  return $.mcp.call(server, tool, args)
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
const chat = { turns: [], busy: null, serial: 0, model: DEFAULTS.model, effort: DEFAULTS.effort, tokens: 0, loaded: false, note: '' }

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
  // Read just before writing: every session on the machine shares the store.
  const kept = (await $.store.get(CHAT_USE)) || {}
  await $.store.set(CHAT_USE, { ...kept, [field]: (kept[field] || 0) + 1 })
}

// One question: retrieval through the engine (no model), then one model call over the excerpts.
async function answerQuestion($, question) {
  const serial = chat.serial + 1
  chat.serial = serial
  const turn = { question, answer: '', cited: [], tokens: 0, state: 'searching', seconds: 0 }
  chat.turns.push(turn)
  chat.busy = turn
  chat.note = ''
  $.ui.invalidate('ui.render')
  const previous = chat.turns.length > 1 ? chat.turns[chat.turns.length - 2].question : null
  const wanted = plan(question, previous)
  const [searched, ...named] = await Promise.all([
    lookup($, 'search', { query: wanted.search, k: 6, detail: 'full' }),
    ...wanted.symbols.map((name) => lookup($, 'symbol', { name, k: 3 })),
  ])
  if (chat.serial !== serial) return  // stopped, or a newer question took over
  const found = excerpts(hitsOf(searched), named.flatMap(symbolsOf))
  turn.state = 'thinking'
  $.ui.invalidate('ui.render')
  const asked = request(question, found, chat.turns.slice(0, -1).filter((earlier) => earlier.state === 'answered'))
  const model = chat.model === 'session' ? await $.session.model() : chat.model
  let reply
  try {
    reply = await $.model.complete({
      model,
      effort: chat.effort,
      prompt: [{ text: asked.rules, cache: true }, { text: asked.body }],
      maxTokens: 1200,
      timeoutMs: 90000,
    })
  } catch (error) {
    reply = { isAnswered: false, reason: error && error.message ? error.message : String(error) }
  }
  if (chat.serial !== serial) return
  turn.tokens = tokensOf(reply.usage)
  chat.tokens += turn.tokens
  if (reply.isAnswered) {
    turn.answer = reply.text
    turn.cited = citations(reply.text, found)
    turn.state = 'answered'
    await count($, 'answers')
    if (turn.cited.length) await count($, 'cited')
  } else {
    turn.answer = 'No answer: ' + (reply.reason || 'the model did not answer') + (searched === null ? ' (the engine did not answer either; `/aleph check` says why)' : '')
    turn.state = 'failed'
  }
  chat.busy = null
  $.ui.invalidate('ui.render')
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

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'aleph',
      description: 'Aleph Zero: with no argument, opens the side chat about this repository. `stats` shows this session\'s tokens, tool calls and how often search was used instead of grep; `stats last` shows the previous session\'s; `check` says whether the mod can reach the engine',
      argumentHint: '[stats [last] | check [model]]',
      immediate: true,
    })
    return next(e)
  })

  on('command.run', { command: 'aleph' }, async ($, e) => {
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
      await $.ui.open({ id: PANE, title: 'Aleph side chat', focus: true, closeOnEscape: true, columns: 64 })
      return {}
    }
    if (/\blast\b/.test(String(e.args || ''))) {
      // The session before this one (any session on this machine): a headless run cannot be asked afterwards.
      const kept = await $.store.get(LAST)
      return { text: kept ? 'last session · ' + report(kept.rung, kept.meter) : 'no session has been recorded yet' }
    }
    if (/\bcheck\s+model\b/.test(args)) {
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
    if (/\bcheck\b/.test(String(e.args || ''))) {
      // Can the mod reach the engine at all? Says what came back, or why nothing did.
      try {
        const answer = await reach($, 'search', { query: 'check', k: 1, format: 'json', timeout_ms: 2000 })
        if (answer === null) return { text: 'the engine is not connected: Claude Code did not start the plugin\'s MCP server' }
        return { text: 'the engine answers the mod (' + hitsOf(answer).length + ' hits for a test query), as ' + server }
      } catch (error) {
        const why = error && error.message ? error.message : String(error)
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
    const { Box, Text, Button, Input, Select, Markdown } = $.ui.resolve(e)
    const last = chat.turns.length ? chat.turns[chat.turns.length - 1] : null
    const header = Box({
      flexDirection: 'row',
      columnGap: 2,
      children: [
        Select({
          key: 'model', label: 'model', options: MODELS, value: chat.model,
          onSelect: async (value) => {
            chat.model = value
            $.ui.invalidate('ui.render')
            await $.store.set(CHAT_SETTINGS, { model: chat.model, effort: chat.effort })
          },
        }),
        Select({
          key: 'effort', label: 'effort', options: EFFORTS, value: chat.effort,
          onSelect: async (value) => {
            chat.effort = value
            $.ui.invalidate('ui.render')
            await $.store.set(CHAT_SETTINGS, { model: chat.model, effort: chat.effort })
          },
        }),
        Text({ dimColor: true, children: [chat.tokens + ' tokens this session'] }),
      ],
    })
    const turns = chat.turns.slice(-4).flatMap((turn) => [
      Text({ bold: true, children: ['> ' + turn.question] }),
      turn.state === 'searching' ? Text({ dimColor: true, children: ['searching the index…'] })
        : turn.state === 'thinking' ? Text({ dimColor: true, children: ['thinking…'] })
          : Markdown({ text: turn.answer }),
      ...(turn.state === 'answered'
        ? [Text({ dimColor: true, children: [turn.tokens + ' tokens · ' + (turn.cited.length ? 'cites ' + turn.cited.join(', ') : 'no location cited')] })]
        : []),
    ])
    const controls = []
    if (chat.busy) {
      controls.push(Button({
        key: 'stop', label: 'Stop', hotkey: 's', plain: true,
        onPress: () => {
          chat.serial += 1  // whatever comes back for the question in flight is dropped
          chat.busy.state = 'failed'
          chat.busy.answer = 'Stopped.'
          chat.busy = null
          $.ui.invalidate('ui.render')
        },
      }))
    } else if (last && last.state === 'answered') {
      controls.push(Button({
        key: 'insert', label: 'Insert into prompt', hotkey: 'i', plain: true,
        onPress: async () => {
          // Drafts the note into the prompt box. Nothing reaches Claude until the user sends it.
          const filled = await $.prompt.fill({ text: note(last.answer, last.cited), mode: 'insert' })
          chat.note = filled && filled.isFilled === false ? 'The prompt box could not be filled.' : 'Drafted into the prompt box; edit or send it.'
          await count($, 'inserted')
          $.ui.invalidate('ui.render')
        },
      }))
    }
    return Box({
      flexDirection: 'column',
      rowGap: 1,
      children: [
        header,
        ...turns,
        ...(controls.length ? [Box({ flexDirection: 'row', columnGap: 3, children: controls })] : []),
        ...(chat.note ? [Text({ dimColor: true, children: [chat.note] })] : []),
        Input({
          key: 'question', label: 'Ask', placeholder: 'about this repository, then Enter', value: '', submitLabel: 'ask', autoFocus: true,
          onSubmit: async (value) => {
            const question = String(value || '').trim()
            if (!question || chat.busy) return
            await answerQuestion($, question)
          },
        }),
      ],
    })
  })

  // ---- the meter ------------------------------------------------------------------------------

  on('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    add(e.agentId ? meter.subagents : meter.main, result && result.usage)
    return result
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      meter.turns += 1
      await $.store.set(LAST, { rung, meter })
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    // The mod's own questions to the engine arrive here as tool calls too. They are not the
    // agent's: only calls Claude Code itself fires are counted.
    if (next.origin && next.origin.plugin !== 'engine') return next(e)
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
    return { description: DESCRIPTIONS[name] || described.description, isDeferred: false }
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
    return { ...composed, sections: [...composed.sections, { id: SECTION_ID, text: SECTION, scope: 'session' }] }
  })

  // ---- L2: ranked locations with the first prompt of a session ------------------------------

  on('prompt.submit', async ($, e, next) => {
    // The one chance goes to the first prompt that is a question: "hi" does not use it up.
    if (rung < 2 || prompts > 0 || !worthAsking(e.text)) return next(e)
    prompts += 1
    const hits = hitsOf(await ask($, 'search', { query: e.text.slice(0, 2000), k: 5 }))
    if (!confident(hits)) return next(e)
    meter.added.prompt += 1
    return next({ ...e, context: [...(e.context ?? []), promptContext(hits)] })
  }).catch(async ($, e, next) => next(e))

  on('session.end', async ($, e, next) => {
    prompts = 0  // /clear, /resume and /branch end one conversation and start another
    return next(e)
  })

  // ---- L3: a recursive grep that found nothing -----------------------------------------------

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    if (rung < 3) return result
    const read = readSearch(e.command)
    if (!read || !foundNothing(result, read.program)) return result
    const pattern = read.query
    const hits = hitsOf(await ask($, 'search', { query: pattern, k: 5 }))
    if (!hits.length) return result
    meter.added.grep += 1
    const note = grepNote(pattern, hits)
    return { ...result, text: (result.text || '') + note, result: { ...result.result, stdout: (result.result.stdout || '') + note } }
  }).catch(async ($, e, next) => next(e))
}
