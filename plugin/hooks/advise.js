// What Aleph Zero suggests about its own setup, from what this session did and what the engine reports: plain functions
// (plugin/tests/advise.test.ts). Nothing here changes anything. A suggestion is applied only by the person, with
// `/aleph advise apply N`; the agent is told only that suggestions exist, never a command (agy's review, 2026-11-05).
//
// Each suggestion says the figure that triggered it and whether what it promises was measured (an ADR) or not.
// Never suggested: anything that reaches outside the machine or the repository (linked sources), the switches kept for
// measurements (grep_pointer, lines, grep_assist), anything that turns a part of Aleph down, and allow_reads (a person
// who switched it off chose to allow each tool by hand; refused tools are told by their own hint).
import { figures } from './meter.js'

// The plugin options a suggestion may set, and the values it may set them to.
export const SETTABLE = { rung: ['2', '3'], subagents: ['on'] }

function percentile(values, share) {
  if (!values.length) return null
  const sorted = [...values].sort((x, y) => x - y)
  return sorted[Math.min(sorted.length - 1, Math.floor(share * sorted.length))]
}

function share(part, whole) {
  return whole ? Math.round((100 * part) / whole) + '%' : 'n/a'
}

const option = (key, value) => ({ kind: 'option', key, value })
const engine = (...args) => ({ kind: 'engine', args })        // run for this repository, from /aleph advise apply
const machine = (command) => ({ kind: 'machine', command })   // a build: copied for a terminal, as /aleph configure does

// The suggestions for this setup, most useful first. `meter` is the session's (meter.js, combined over conversations),
// `status` the engine's status of the repository (null when not known), `options` the plugin's options as loaded.
export function suggest(meter, status, options = {}, rung = 1) {
  const out = []
  const { all, subagents } = figures(meter)
  const engineUse = meter.engine || {}
  const vectors = (status && status.vectors) || {}
  const memory = (status && status.memory) || {}
  const off = (key) => /^(off|0|false|no)$/i.test(String(options[key] ?? '').trim())

  // Subagents located code mostly by grep.
  const subLocates = subagents.locate.aleph + subagents.locate.grep
  const subAdoption = subLocates ? subagents.locate.aleph / subLocates : 1
  if (subagents.runs >= 5 && subLocates >= 20 && subAdoption < 0.25) {
    const why = subagents.runs + ' subagents located code with Aleph in ' + share(subagents.locate.aleph, subLocates)
      + ' of ' + subLocates + ' locating calls'
    if (off('subagents')) {
      out.push({ id: 'subagents', scope: 'plugin', title: 'Steer subagents too', why, evidence: 'not measured yet (ADR-011, DEFERRED 0-sub)',
        action: option('subagents', 'on'), cost: 'about 80 tokens added to each subagent\'s task' })
    } else if (rung < 2) {
      out.push({ id: 'rung-2', scope: 'plugin', title: 'Give subagents ranked locations with their task (rung 2)', why,
        evidence: 'not measured yet for subagents; rung 2 on first prompts was not shown to save tokens (ADR-011)',
        action: option('rung', '2'), cost: 'one engine search a subagent, up to five file lines in its task' })
    }
  }

  // Greps that found nothing: rung 3 answers them with ranked locations.
  const empty = all.emptyGrep || 0
  if (rung < 3 && empty >= 8 && all.locate.grep && empty / all.locate.grep >= 0.2) {
    out.push({ id: 'rung-3', scope: 'plugin', title: 'Answer greps that found nothing (rung 3)',
      why: empty + ' of ' + all.locate.grep + ' locating greps found nothing (' + share(empty, all.locate.grep) + ')',
      evidence: 'not measured yet (ADR-011)', action: option('rung', '3'), cost: 'one engine search for each such grep' })
  }

  // Search by meaning, where the benchmark for the repository's language says it helps.
  const aleph = all.locate.aleph
  const missed = all.grepAfterAleph
  const verdict = vectors.verdict || {}
  if (status && vectors.enabled === false && verdict.helps) {
    if (vectors.worker_installed === false) {
      out.push({ id: 'worker', scope: 'machine', title: 'Build the embedding worker (search by meaning)',
        why: 'search by meaning helps for ' + (status.main_language || 'this language') + ', and the worker that makes it is not built',
        evidence: 'measured: ' + String(verdict.why || 'ADR-002'), action: machine('aleph-zero setup --worker'), cost: 'a build of about a minute, 310 MB' })
    } else if (aleph >= 15 && missed / aleph >= 0.25) {
      // The person turned it off (or it is off for another reason): suggested again only on this evidence, and said so.
      out.push({ id: 'vectors', scope: 'repository', title: 'Turn on search by meaning for this repository',
        why: 'a grep followed Aleph\'s answer within two calls in ' + missed + ' of ' + aleph + ' cases (' + share(missed, aleph)
          + '): its answers by words and names were often not enough' + (vectors.chosen === false ? '; you turned it off before' : ''),
        evidence: 'measured: ' + String(verdict.why || 'ADR-002'),
        action: engine('setup', '--dense'),
        cost: 'vectors for ' + Number(vectors.chunks || 0).toLocaleString('en') + ' chunks, a few minutes of GPU, in the background' })
    }
  }

  // The git index's untracked cache, where looking at the worktree is what makes answers slow.
  const settled = (engineUse.answered || 0) - (engineUse.stale || 0)
  const p50 = percentile(engineUse.ms || [], 0.5)
  if (status && status.untracked_cache === false && Number(status.files_in_view || 0) >= 3000 && settled >= 10 && p50 > 50) {
    out.push({ id: 'untracked-cache', scope: 'repository', title: 'Turn on git\'s untracked cache here',
      why: 'the engine took ' + p50 + ' ms at the median on ' + Number(status.files_in_view).toLocaleString('en') + ' files',
      evidence: 'measured: usually two to three times faster on a large worktree (aleph-zero doctor)',
      action: engine('setup', '--untracked-cache'), cost: 'none; it changes no git configuration' })
  }

  // Builds older than this Aleph pins.
  if (status && (vectors.worker_installed && vectors.worker_current === false || memory.aeon_installed && memory.aeon_current === false)) {
    out.push({ id: 'update', scope: 'machine', title: 'Rebuild what is out of date',
      why: [vectors.worker_installed && vectors.worker_current === false ? 'the embedding worker' : '',
        memory.aeon_installed && memory.aeon_current === false ? 'memory by meaning (Aeon)' : ''].filter(Boolean).join(' and ')
        + ' is built from an older version than this Aleph pins',
      evidence: 'a maintenance step', action: machine('aleph-zero update'), cost: 'a build of under a minute' })
  }

  // Memory by meaning, where memory is actually used.
  const remembered = (all.features.memory || 0) + ((meter.chat && meter.chat.answers) || 0)
  if (status && memory.aeon_installed === false && remembered >= 5) {
    out.push({ id: 'memory', scope: 'machine', title: 'Recall earlier answers by meaning (Aeon)',
      why: 'memory was used ' + remembered + ' times this session (recall, remember, side-chat answers), by words and recency only',
      evidence: 'not measured', action: machine('aleph-zero setup --memory'), cost: 'a build of under a minute, uses the network' })
  }

  return out
}

// The suggestions not dismissed, at most four. A dismissal is kept by id and repository for a repository's settings, by
// id alone for the machine's builds and the plugin's options (they are in the user's settings, for every repository).
export function dismissalKey(suggestion, root) {
  return suggestion.scope === 'repository' ? suggestion.id + '@' + String(root || '') : suggestion.id
}

export function open(suggestions, dismissed = [], root = '') {
  return suggestions.filter((suggestion) => !dismissed.includes(dismissalKey(suggestion, root))).slice(0, 4)
}

export function actionText(action) {
  if (action.kind === 'option') return 'set the plugin option ' + action.key + ' to ' + action.value
  if (action.kind === 'engine') return 'run `aleph-zero ' + action.args.join(' ') + '` for this repository'
  return 'run `' + action.command + '` in a terminal'
}

// /aleph advise: the list, numbered, each with why, the evidence and what applying it does.
export function adviceText(suggestions) {
  if (!suggestions.length) {
    return 'Aleph Zero has no suggestion for its setup from this session\'s figures. It suggests a change only when the figures '
      + 'clear a minimum (for example 20 locating calls by subagents, or 8 greps that found nothing).'
  }
  const lines = ['Aleph Zero suggests, from this session\'s figures (nothing is changed until you apply one):', '']
  suggestions.forEach((suggestion, index) => {
    lines.push((index + 1) + '. ' + suggestion.title)
    lines.push('   why: ' + suggestion.why)
    lines.push('   evidence: ' + suggestion.evidence)
    lines.push('   does: ' + actionText(suggestion.action) + ' · cost: ' + suggestion.cost)
  })
  lines.push('')
  lines.push('`/aleph advise apply N` applies one; `/aleph advise dismiss N` stops suggesting it.')
  return lines.join('\n')
}

// What the agent is told when a new suggestion appears: that it exists, never a command or a setting.
export function agentNote(count) {
  return '[aleph-zero] Aleph Zero has ' + count + ' suggestion' + (count === 1 ? '' : 's') + ' about its own setup, from this '
    + 'session\'s usage. Mention once, in one sentence, that the user can review ' + (count === 1 ? 'it' : 'them') + ' with '
    + '`/aleph advise`. Do not run any command or change any setting for this.'
}
