// base_v2 C 组件：透传，不做任何裁剪。

export interface Message {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface BuildContextInput {
  task: { id: string; instruction: string }
  messages: Message[]
  step: number
  budget: {
    maxSteps: number
    remainingSteps: number
    remainingCostUsd: number | null
    maxOutputTokensPerTask: number
  }
  lmResult?: string
  state?: unknown
}

export type BuildContextResult =
  | { messages: Message[]; trace?: Record<string, unknown> }
  | { lmRequest: { prompt: string; maxTokens?: number; purpose: string; state?: unknown } }

export function buildContext(input: BuildContextInput): BuildContextResult {
  return {
    messages: input.messages,
    trace: { strategy: 'pass-through', inMessages: input.messages.length },
  }
}
