// base_v2 T 组件：与 engine/appworld_vendor/react_code_agent.py 的观测包装逐字节等价。

export interface FormatObservationInput {
  raw: string
  step: number
  code: string
}

export interface FormatObservationResult {
  text: string
  trace?: Record<string, unknown>
}

export function formatObservation(input: FormatObservationInput): FormatObservationResult {
  const raw = input.raw
  const text = 'Output:\n```\n' + raw + (raw.endsWith('\n') ? '' : '\n') + '```\n\n'
  return {
    text,
    trace: {
      bytesIn: Buffer.byteLength(raw, 'utf8'),
      bytesOut: Buffer.byteLength(text, 'utf8'),
    },
  }
}
