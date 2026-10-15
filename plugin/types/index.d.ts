// What the mod keeps for the session in $.state: Claude Code loads the mod again when one of its options changes,
// and a reload must not wipe the side chat's conversation or the session's meter.
export type ChatTurn = { question: string; answer: string; cited: string[]; tokens: number; state: string }
export type Usage = { requests: number; input: number; output: number; cacheRead: number; cacheWrite: number }
export type Meter = {
  turns: number; main: Usage; subagents: Usage; tools: Record<string, number>; aleph: number; grep: number
  first: string | null; added: { prompt: number; grep: number }; listed: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'aleph-zero': { session: { meter: Meter; prompts: number; turns: ChatTurn[]; tokens: number; focused: string | null } }
  }
}
