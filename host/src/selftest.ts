// 端到端自测：spawn 真实 main.ts 子进程，用 vscode-jsonrpc 客户端逐条演练契约 §3 的方法。
import { spawn } from 'node:child_process'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  createMessageConnection,
  ResponseError,
  StreamMessageReader,
  StreamMessageWriter,
} from 'vscode-jsonrpc/lib/node/main.js'

const HOST_DIR = fileURLToPath(new URL('..', import.meta.url))
const REPO_ROOT = dirname(HOST_DIR)
const TMP_DIR = join(HOST_DIR, '.tmp_selftest')
const BASE_V2_DIR = join(REPO_ROOT, 'candidates', 'base_v2')
const BASE_V1_TEMPLATE = join(REPO_ROOT, 'candidates', 'base', 'W', 'instructions.txt')

const COMPONENT_FILE = { W: 'workflow.ts', T: 'wrapper.ts', C: 'context.ts' } as const
type Key = keyof typeof COMPONENT_FILE
const KEYS: readonly Key[] = ['W', 'T', 'C']

let checks = 0

function check(condition: boolean, message: string): void {
  checks += 1
  if (!condition) throw new Error(`断言失败：${message}`)
}

function log(message: string): void {
  process.stderr.write(`[selftest] ${message}\n`)
}

interface ValidateResult {
  ok: boolean
  errors: string[]
  exports: Record<Key, string[]>
}

interface LoadResult {
  ok: boolean
  exports: Record<Key, string[]>
}

interface ErrorPayload {
  candidate_error?: boolean
  component?: string
  candidate_id?: string
}

const child = spawn(join(HOST_DIR, 'node_modules', '.bin', 'tsx'), ['src/main.ts'], {
  cwd: HOST_DIR,
  env: { ...process.env, PATH: `${dirname(process.execPath)}:${process.env.PATH ?? ''}` },
  // 宿主 stderr 继承过来：Node 侧真实报错必须可见
  stdio: ['pipe', 'pipe', 'inherit'],
})

const connection = createMessageConnection(
  new StreamMessageReader(child.stdout!),
  new StreamMessageWriter(child.stdin!),
  { error: log, warn: log, info: log, log },
)
connection.listen()

async function request<T>(method: string, params: unknown): Promise<T> {
  return (await connection.sendRequest<T>(method, params)) as T
}

async function requestError(method: string, params: unknown): Promise<ResponseError<ErrorPayload>> {
  try {
    await request(method, params)
  } catch (error) {
    if (error instanceof ResponseError) return error
    throw error
  }
  throw new Error(`断言失败：${method} 期望返回 JSON-RPC error，实际成功`)
}

function candidateFiles(dir: string): Record<Key, string> {
  return {
    W: join(dir, 'W', COMPONENT_FILE.W),
    T: join(dir, 'T', COMPONENT_FILE.T),
    C: join(dir, 'C', COMPONENT_FILE.C),
  }
}

// 造一个临时候选目录：未覆盖的组件直接复制 base_v2 的真实源码
function writeCandidate(name: string, overrides: Partial<Record<Key, string>>): string {
  const dir = join(TMP_DIR, name)
  for (const key of KEYS) {
    const target = join(dir, key)
    mkdirSync(target, { recursive: true })
    const source = overrides[key] ?? readFileSync(candidateFiles(BASE_V2_DIR)[key], 'utf8')
    writeFileSync(join(target, COMPONENT_FILE[key]), source, 'utf8')
  }
  return dir
}

// cand_half：C 把消息数砍半，用来验证不同候选真的各走各的组件
const HALF_C = `export interface Message { role: 'system' | 'user' | 'assistant'; content: string }

export function buildContext(input: { messages: Message[] }): {
  messages: Message[]
  trace?: Record<string, unknown>
} {
  const half = input.messages.slice(0, Math.ceil(input.messages.length / 2))
  return { messages: half, trace: { strategy: 'half', inMessages: input.messages.length } }
}
`

// cand_throw：beforeStep 主动抛异常，用来验证候选真实失败的错误形态
const THROW_W = `export const instructionTemplate = 'USER:\\nTask: {{ instruction }}'

export function beforeStep(): { action: string } {
  throw new Error('selftest 主动抛出')
}
`

const CONTEXT_INPUT = {
  task: { id: 'selftest', instruction: '自测任务' },
  messages: [
    { role: 'user', content: 'instruction' },
    { role: 'assistant', content: 'print(1)' },
    { role: 'user', content: 'Output:\n```\n1\n```\n\n' },
    { role: 'assistant', content: 'print(2)' },
  ],
  step: 2,
  budget: { maxSteps: 10, remainingSteps: 8, remainingCostUsd: 1.25, maxOutputTokensPerTask: 100000 },
}

const WORKFLOW_STATE = {
  step: 1,
  taskInstruction: '自测任务',
  lastCode: 'print(1)',
  lastObservation: 'Output:\n```\n1\n```\n\n',
  lastError: '',
  usage: { costUsd: 0.01, outputTokens: 8 },
  historyLength: 3,
}

try {
  // ---- a. validate 三份 base_v2 源码 ----
  const baseSources: Record<Key, string> = {
    W: readFileSync(candidateFiles(BASE_V2_DIR).W, 'utf8'),
    T: readFileSync(candidateFiles(BASE_V2_DIR).T, 'utf8'),
    C: readFileSync(candidateFiles(BASE_V2_DIR).C, 'utf8'),
  }
  const validated = await request<ValidateResult>('validate', { sources: baseSources })
  check(validated.ok, `base_v2 validate 应通过，实际 errors=${JSON.stringify(validated.errors)}`)
  check(validated.errors.length === 0, 'base_v2 validate 不应有错误')
  check(
    JSON.stringify(validated.exports.W) === JSON.stringify(['beforeStep', 'instructionTemplate']),
    `W 导出清单不对：${JSON.stringify(validated.exports.W)}`,
  )
  check(
    JSON.stringify(validated.exports.T) === JSON.stringify(['formatObservation']),
    `T 导出清单不对：${JSON.stringify(validated.exports.T)}`,
  )
  check(
    JSON.stringify(validated.exports.C) === JSON.stringify(['buildContext']),
    `C 导出清单不对：${JSON.stringify(validated.exports.C)}`,
  )

  // ---- b. normalize 的等价性 ----
  const hashOf = async (source: string): Promise<string> => {
    const result = await request<{ sha256: string }>('normalize', { component: 'W', source })
    return result.sha256
  }
  const hashOnce = await hashOf(baseSources.W)
  const hashTwice = await hashOf(baseSources.W)
  check(hashOnce === hashTwice, `同一源码两次 normalize 哈希不同：${hashOnce} vs ${hashTwice}`)
  const hashCommented = await hashOf(`// 顶部注释\n${baseSources.W.replace(
    'export function beforeStep',
    'export function beforeStep /* 行内注释 */',
  )}`)
  check(hashOnce === hashCommented, '仅注释差异的源码应得到同一哈希')
  const hashRenamed = await hashOf(baseSources.W.replace('function beforeStep', 'function beforeStepX'))
  check(hashOnce !== hashRenamed, '标识符改动必须改变归一化哈希')

  // ---- c. 装载 base_v2 并验证逐字节等价 ----
  const baseFiles = candidateFiles(BASE_V2_DIR)
  const loaded = await request<LoadResult>('load', { candidate_id: 'base_v2', files: baseFiles })
  check(loaded.ok, 'load base_v2 应返回 ok')
  check(
    JSON.stringify(loaded.exports.W) === JSON.stringify(['beforeStep', 'instructionTemplate']),
    `load 返回的 W 导出清单不对：${JSON.stringify(loaded.exports.W)}`,
  )
  const templateResult = await request<{ template: string }>('w.template', { candidate_id: 'base_v2' })
  const referenceBytes = readFileSync(BASE_V1_TEMPLATE)
  check(
    Buffer.from(templateResult.template, 'utf8').equals(referenceBytes),
    'w.template 必须与 candidates/base/W/instructions.txt 逐字节一致',
  )
  const formatted = await request<{ text: string; trace?: Record<string, unknown> }>(
    't.formatObservation',
    { candidate_id: 'base_v2', raw: 'x', step: 0, code: '' },
  )
  check(formatted.text === 'Output:\n```\nx\n```\n\n', `formatObservation('x') 输出不对：${JSON.stringify(formatted.text)}`)
  check(
    formatted.trace?.bytesIn === 1 && formatted.trace?.bytesOut === 19,
    `formatObservation trace 不对：${JSON.stringify(formatted.trace)}`,
  )
  const formattedWithNewline = await request<{ text: string }>('t.formatObservation', {
    candidate_id: 'base_v2',
    raw: 'a\n',
    step: 0,
    code: 'print(1)',
  })
  check(
    formattedWithNewline.text === 'Output:\n```\na\n```\n\n',
    `raw 以换行结尾时不应再补换行：${JSON.stringify(formattedWithNewline.text)}`,
  )
  const passed = await request<{ messages: unknown[]; trace?: Record<string, unknown> }>('c.buildContext', {
    candidate_id: 'base_v2',
    input: CONTEXT_INPUT,
  })
  check(
    JSON.stringify(passed.messages) === JSON.stringify(CONTEXT_INPUT.messages),
    'base_v2 的 buildContext 必须原样透传消息',
  )
  check(
    passed.trace?.strategy === 'pass-through' && passed.trace?.inMessages === 4,
    `base_v2 的 buildContext trace 不对：${JSON.stringify(passed.trace)}`,
  )

  // ---- d. 版本切换与卸载 ----
  const halfDir = writeCandidate('cand_half', { C: HALF_C })
  await request<LoadResult>('load', { candidate_id: 'cand_half', files: candidateFiles(halfDir) })
  const halved = await request<{ messages: unknown[]; trace?: Record<string, unknown> }>('c.buildContext', {
    candidate_id: 'cand_half',
    input: CONTEXT_INPUT,
  })
  check(halved.messages.length === 2, `cand_half 应返回一半消息，实际 ${halved.messages.length}`)
  check(halved.trace?.strategy === 'half', `cand_half trace 不对：${JSON.stringify(halved.trace)}`)
  const stillPassing = await request<{ messages: unknown[] }>('c.buildContext', {
    candidate_id: 'base_v2',
    input: CONTEXT_INPUT,
  })
  check(stillPassing.messages.length === 4, '两个候选并存时 base_v2 不应受影响')

  await request<{ ok: boolean }>('unload', { candidate_id: 'base_v2' })
  const unloadedError = await requestError('w.template', { candidate_id: 'base_v2' })
  check(
    unloadedError.data?.candidate_error !== true,
    '卸载后调用应报候选未装载（协议错误），不能标成 candidate_error',
  )
  check(
    unloadedError.message.includes('未装载'),
    `卸载后调用的错误信息应说明未装载：${unloadedError.message}`,
  )
  const halfAfterUnload = await request<{ messages: unknown[] }>('c.buildContext', {
    candidate_id: 'cand_half',
    input: CONTEXT_INPUT,
  })
  check(halfAfterUnload.messages.length === 2, '卸载 base_v2 不应影响 cand_half')
  const reloaded = await request<LoadResult>('load', { candidate_id: 'base_v2', files: baseFiles })
  check(reloaded.ok, '卸载后可重新 load 同一 candidate_id（注册表不留半装载状态）')
  const duplicateError = await requestError('load', { candidate_id: 'base_v2', files: baseFiles })
  check(duplicateError.data?.candidate_error !== true, '重复 load 必须是协议错误')
  await request<{ ok: boolean }>('unload', { candidate_id: 'cand_half' })
  const unloadUnknown = await requestError('unload', { candidate_id: 'cand_half' })
  check(unloadUnknown.message.includes('未装载'), `重复 unload 应报未装载：${unloadUnknown.message}`)

  // ---- e. 权限拦截 ----
  const importSource = `import fs from 'node:fs'\n\nexport function buildContext(input: { messages: unknown[] }): { messages: unknown[] } {\n  return { messages: fs ? input.messages : input.messages }\n}\n`
  const importResult = await request<ValidateResult>('validate', { sources: { C: importSource } })
  check(!importResult.ok, '含 import 的源码必须 validate 失败')
  check(
    importResult.errors.some((message) => message.includes('禁止 import')),
    `错误信息应指明 import：${JSON.stringify(importResult.errors)}`,
  )
  check(importResult.errors.some((message) => message.startsWith('C: ')), '错误信息应带组件前缀')
  const escapeSource = `export function buildContext(input: { messages: unknown[] }): { messages: unknown[] } {
  const runtime = process.env.HOME
  const loader = eval('1')
  const dynamic = import('node:fs')
  const built = new Function('return 1')
  return { messages: input.messages, runtime, loader, dynamic, built } as never
}
`
  const escapeResult = await request<ValidateResult>('validate', { sources: { C: escapeSource } })
  check(!escapeResult.ok, '含 process/eval/import()/Function 的源码必须 validate 失败')
  check(
    ['process', 'eval', 'import()', 'Function'].every((token) =>
      escapeResult.errors.some((message) => message.includes(token)),
    ),
    `逃逸面错误信息不全：${JSON.stringify(escapeResult.errors)}`,
  )
  const syntaxResult = await request<ValidateResult>('validate', {
    sources: { T: 'export function formatObservation(input { return }\n' },
  })
  check(!syntaxResult.ok, '语法错误必须 validate 失败')
  check(
    syntaxResult.errors.some((message) => message.includes('语法错误')),
    `语法错误信息缺失：${JSON.stringify(syntaxResult.errors)}`,
  )
  const shapeResult = await request<ValidateResult>('validate', {
    sources: { W: 'export const instructionTemplate = 1\nexport const beforeStep = 2\n' },
  })
  check(!shapeResult.ok, '导出形态不对必须 validate 失败')
  check(
    shapeResult.errors.some((message) => message.includes('instructionTemplate')) &&
      shapeResult.errors.some((message) => message.includes('beforeStep')),
    `导出形态错误信息不全：${JSON.stringify(shapeResult.errors)}`,
  )

  // ---- f. 组件抛异常 ----
  const throwDir = writeCandidate('cand_throw', { W: THROW_W })
  await request<LoadResult>('load', { candidate_id: 'cand_throw', files: candidateFiles(throwDir) })
  const candidateError = await requestError('w.beforeStep', {
    candidate_id: 'cand_throw',
    state: WORKFLOW_STATE,
  })
  check(candidateError.data?.candidate_error === true, '组件抛异常必须标成 candidate_error')
  check(candidateError.data?.component === 'W', `component 应为 W：${String(candidateError.data?.component)}`)
  check(candidateError.data?.candidate_id === 'cand_throw', 'candidate_id 应回填')
  check(candidateError.code === -32000, `候选失败的错误码应为 -32000：${candidateError.code}`)
  check(
    candidateError.message.includes('selftest 主动抛出'),
    `错误信息应保留原始异常文本：${candidateError.message}`,
  )
  const ghost = await requestError('w.beforeStep', { candidate_id: 'no_such_candidate', state: WORKFLOW_STATE })
  check(ghost.data?.candidate_error !== true, '未装载候选必须按协议错误处理')
  await request<{ ok: boolean }>('unload', { candidate_id: 'cand_throw' })

  // ---- g. shutdown ----
  const shutdown = await request<{ ok: boolean }>('shutdown', {})
  check(shutdown.ok === true, 'shutdown 应返回 ok')
  const exitCode = await Promise.race([
    new Promise<number | null>((resolve) => {
      child.once('exit', (code) => resolve(code))
    }),
    new Promise<number>((_resolve, reject) => {
      setTimeout(() => reject(new Error('断言失败：宿主未在 10 秒内退出')), 10000)
    }),
  ])
  check(exitCode === 0, `宿主退出码应为 0，实际 ${String(exitCode)}`)
} finally {
  child.kill()
  rmSync(TMP_DIR, { recursive: true, force: true })
}

console.log(`[selftest] PASS 断言 ${checks} 条`)
