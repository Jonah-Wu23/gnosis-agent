// GNOSIS 组件宿主入口：stdio JSON-RPC 服务端，实现契约 §3 的全部方法。
// stdout 只承载协议帧：组件代码里的 console.log 等 stdout 写入一律改道 stderr，
// 否则一帧日志就能冲坏 JSON-RPC 流，整个分片按基础设施故障中断。
import {
  createMessageConnection,
  ResponseError,
  StreamMessageReader,
  StreamMessageWriter,
} from 'vscode-jsonrpc/lib/node/main.js'
import {
  assertSerializable,
  CandidateError,
  ComponentHost,
  ProtocolError,
  type BuildContextInput,
  type ComponentFiles,
  type ComponentKey,
  type WorkflowState,
} from './loader.js'
import { validateSources } from './validate.js'
import { normalizeSha256 } from './normalize.js'

const CANDIDATE_ERROR_CODE = -32000
const HOST_ERROR_CODE = -32001
const INTERNAL_ERROR_CODE = -32603

const realStdout = process.stdout
const rawWrite = realStdout.write.bind(realStdout)
const stderrWrite = process.stderr.write.bind(process.stderr)

// 协议帧走原始终端写入口，其余 stdout 写入改道 stderr
const protocolStream = new Proxy(realStdout, {
  get(target, property) {
    if (property === 'write') return rawWrite
    return Reflect.get(target, property, target)
  },
}) as unknown as NodeJS.WritableStream

realStdout.write = ((chunk: unknown, encoding?: unknown, callback?: unknown) =>
  stderrWrite(chunk as string, encoding as BufferEncoding, callback as () => void)) as typeof realStdout.write

const log = (message: string): void => {
  stderrWrite(`[host] ${message}\n`)
}

function errorText(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return String(error)
}

const connection = createMessageConnection(
  new StreamMessageReader(process.stdin),
  new StreamMessageWriter(protocolStream),
  { error: log, warn: log, info: log, log },
)

const host = new ComponentHost()

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ProtocolError(`${what} 必须是对象`)
  }
  return value as Record<string, unknown>
}

function requireString(object: Record<string, unknown>, key: string, what: string): string {
  const value = object[key]
  if (typeof value !== 'string') throw new ProtocolError(`${what} 的 ${key} 必须是字符串`)
  return value
}

function requirePath(object: Record<string, unknown>, key: string, what: string): string {
  const value = requireString(object, key, what)
  if (value.length === 0) throw new ProtocolError(`${what} 的 ${key} 不能为空`)
  return value
}

function requireRecord(object: Record<string, unknown>, key: string, what: string): Record<string, unknown> {
  const value = object[key]
  if (value === undefined) throw new ProtocolError(`${what} 缺少字段 ${key}`)
  return asRecord(value, `${what}.${key}`)
}

function requireNumber(object: Record<string, unknown>, key: string, what: string): number {
  const value = object[key]
  if (typeof value !== 'number' || Number.isNaN(value)) {
    throw new ProtocolError(`${what} 的 ${key} 必须是数字`)
  }
  return value
}

function candidateIdOf(params: unknown, what: string): string {
  const object = asRecord(params, `${what} params`)
  const candidateId = requirePath(object, 'candidate_id', `${what} params`)
  return candidateId
}

// 组件调用统一出口：组件抛异常或返回形态不对都算候选失败，宿主自身的问题照原样上抛
function invoke<T>(component: ComponentKey, candidateId: string, call: () => T): T {
  let value: T
  try {
    value = call()
  } catch (error) {
    if (error instanceof CandidateError || error instanceof ProtocolError) throw error
    throw new CandidateError(component, candidateId, errorText(error))
  }
  assertSerializable(value, component, candidateId)
  return value
}

function toResponseError(error: unknown): ResponseError<Record<string, unknown>> {
  if (error instanceof CandidateError) {
    return new ResponseError(CANDIDATE_ERROR_CODE, error.message, {
      candidate_error: true,
      component: error.component,
      candidate_id: error.candidateId,
    })
  }
  if (error instanceof ProtocolError) {
    return new ResponseError(HOST_ERROR_CODE, error.message, { candidate_error: false, ...error.data })
  }
  return new ResponseError(INTERNAL_ERROR_CODE, `宿主内部错误：${errorText(error)}`, {
    candidate_error: false,
    phase: 'internal',
  })
}

function register(name: string, handler: (params: unknown) => unknown): void {
  connection.onRequest(name, async (params: unknown) => {
    try {
      return await handler(params)
    } catch (error) {
      const response = toResponseError(error)
      log(`${name} 失败：${response.message}`)
      return response
    }
  })
}

register('load', async (params) => {
  const object = asRecord(params, 'load params')
  const candidateId = requirePath(object, 'candidate_id', 'load params')
  const files = requireRecord(object, 'files', 'load params')
  const paths: ComponentFiles = {
    W: requirePath(files, 'W', 'load params.files'),
    T: requirePath(files, 'T', 'load params.files'),
    C: requirePath(files, 'C', 'load params.files'),
  }
  const result = await host.load(candidateId, paths)
  log(`load ${candidateId} 完成：W=${paths.W}`)
  return result
})

register('unload', async (params) => {
  const candidateId = candidateIdOf(params, 'unload')
  await host.unload(candidateId)
  log(`unload ${candidateId} 完成`)
  return { ok: true }
})

register('validate', async (params) => {
  const object = asRecord(params, 'validate params')
  const sources = requireRecord(object, 'sources', 'validate params')
  const picked: Partial<Record<ComponentKey, string>> = {}
  for (const key of ['W', 'T', 'C'] as const) {
    const source = sources[key]
    if (source === undefined) continue
    if (typeof source !== 'string') throw new ProtocolError(`validate params.sources.${key} 必须是字符串`)
    picked[key] = source
  }
  return validateSources(picked)
})

register('normalize', (params) => {
  const object = asRecord(params, 'normalize params')
  const component = requireString(object, 'component', 'normalize params')
  if (component !== 'W' && component !== 'T' && component !== 'C') {
    throw new ProtocolError(`normalize params.component 非法：${component}`)
  }
  const source = object.source
  if (typeof source !== 'string') throw new ProtocolError('normalize params.source 必须是字符串')
  return { sha256: normalizeSha256(source, `${component}.ts`) }
})

register('w.template', (params) => {
  const candidateId = candidateIdOf(params, 'w.template')
  const service = host.resolve(candidateId)
  return invoke('W', candidateId, () => ({ template: service.template() }))
})

register('w.beforeStep', (params) => {
  const object = asRecord(params, 'w.beforeStep params')
  const candidateId = requirePath(object, 'candidate_id', 'w.beforeStep params')
  const state = requireRecord(object, 'state', 'w.beforeStep params')
  const service = host.resolve(candidateId)
  return invoke('W', candidateId, () => service.beforeStep(state as unknown as WorkflowState))
})

register('t.formatObservation', (params) => {
  const object = asRecord(params, 't.formatObservation params')
  const candidateId = requirePath(object, 'candidate_id', 't.formatObservation params')
  const raw = requireString(object, 'raw', 't.formatObservation params')
  const code = requireString(object, 'code', 't.formatObservation params')
  const step = requireNumber(object, 'step', 't.formatObservation params')
  const service = host.resolve(candidateId)
  return invoke('T', candidateId, () => service.formatObservation({ raw, step, code }))
})

register('c.buildContext', (params) => {
  const object = asRecord(params, 'c.buildContext params')
  const candidateId = requirePath(object, 'candidate_id', 'c.buildContext params')
  const input = requireRecord(object, 'input', 'c.buildContext params')
  const service = host.resolve(candidateId)
  return invoke('C', candidateId, () => service.buildContext(input as unknown as BuildContextInput))
})

register('shutdown', async () => {
  try {
    await host.unloadAll()
  } catch (error) {
    // 进程马上退出，卸载失败无处修复，如实写 stderr 后照常退出，不阻断引擎回收
    log(`shutdown 卸载失败：${errorText(error)}`)
  }
  log('shutdown 完成，退出')
  setTimeout(() => process.exit(0), 100)
  return { ok: true }
})

connection.listen()
