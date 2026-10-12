// Aleph Zero's steering mod. It holds no retrieval logic: everything it knows about
// the code comes from the engine, through $.mcp.call. Each rung is a setting, so a
// measurement can switch them one at a time (docs/adr/ADR-011).

// Counts for this session, shown by /aleph stats.
const meter = { turns: 0, steps: 0, tokens: 0, tools: {}, aleph: 0, grep: 0 }

export function register(on, options) {
  const rung = Number(options && options.rung !== undefined ? options.rung : 1)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'aleph',
      description: 'Aleph Zero: `stats` shows tokens, tool calls and how often search was used instead of grep',
      argumentHint: 'stats',
    })
    return next(e)
  })

  on('command.run', { command: 'aleph' }, async () => {
    const tools = Object.keys(meter.tools).sort().map((name) => name + ' ' + meter.tools[name]).join(', ')
    return {
      text: 'rung ' + rung + ' · turns ' + meter.turns + ' · model requests ' + meter.steps
        + ' · tokens ' + meter.tokens + ' · aleph calls ' + meter.aleph + ' · grep calls ' + meter.grep
        + (tools ? ' · tools: ' + tools : ''),
    }
  })
}
