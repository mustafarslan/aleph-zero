// Aleph Zero's steering mod. It holds no retrieval logic: everything it knows about the
// code comes from the engine, through $.mcp.call. Each rung is a setting, so a measurement
// can switch them one at a time (docs/adr/ADR-011):
//   0  the engine's tools as they are
//   1  the tools loaded up front with tuned descriptions, and a short standing instruction
//   2  ranked locations ride along with the first prompt of a session
//   3  a recursive grep that found nothing gets ranked locations appended
// The meter runs on every rung.
import {
  DESCRIPTIONS, ENGINE_TOOL, GREP_NOTE, SECTION, SECTION_ID, confident, foundNothing, grepNote, hitsOf, isSearchCommand,
  promptContext, searchPattern, worthAsking,
} from './logic.js'

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

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'aleph',
      description: 'Aleph Zero: `stats` shows this session\'s tokens, tool calls and how often search was used instead of grep; `stats last` shows the previous session\'s; `check` says whether the mod can reach the engine',
      argumentHint: 'stats [last] | check',
    })
    return next(e)
  })

  on('command.run', { command: 'aleph' }, async ($, e) => {
    if (/\blast\b/.test(String(e.args || ''))) {
      // The session before this one (any session on this machine): a headless run cannot be asked afterwards.
      const kept = await $.store.get(LAST)
      return { text: kept ? 'last session · ' + report(kept.rung, kept.meter) : 'no session has been recorded yet' }
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
    if (rung < 1 || !described || typeof described.description !== 'string') return described
    return { ...described, description: described.description + GREP_NOTE }
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (rung < 1 || !composed || !Array.isArray(composed.sections)) return composed
    // The same text in every request of every session: it must not break the prompt cache.
    return { ...composed, sections: [...composed.sections, { id: SECTION_ID, text: SECTION, scope: 'session' }] }
  })

  // ---- L2: ranked locations with the first prompt of a session ------------------------------

  on('prompt.submit', async ($, e, next) => {
    prompts += 1
    if (rung < 2 || prompts !== 1 || !worthAsking(e.text)) return next(e)
    const hits = hitsOf(await ask($, 'search', { query: e.text.slice(0, 2000), k: 5 }))
    if (!confident(hits)) return next(e)
    meter.added.prompt += 1
    return next({ ...e, context: [...(e.context ?? []), promptContext(hits)] })
  }).catch(async ($, e, next) => next(e))

  // ---- L3: a recursive grep that found nothing -----------------------------------------------

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    if (rung < 3 || !foundNothing(result)) return result
    const pattern = searchPattern(e.command)
    if (!pattern) return result
    const hits = hitsOf(await ask($, 'search', { query: pattern, k: 5 }))
    if (!hits.length) return result
    meter.added.grep += 1
    const note = grepNote(pattern, hits)
    return { ...result, text: (result.text || '') + note, result: { ...result.result, stdout: (result.result.stdout || '') + note } }
  }).catch(async ($, e, next) => next(e))
}
