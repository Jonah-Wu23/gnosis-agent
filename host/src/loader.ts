// Cordis 组件宿主：每候选一个 plugin，装载后注册服务 components.<candidateId>。
// 装载顺序（契约 §3）：权限扫描 → 导出检查 → Cordis 装载；失败不留半装载状态。
import { Context, Service } from 'cordis'
import type { Fiber } from 'cordis'
import { existsSync, readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import vm from 'node:vm'
import ts from 'typescript-legacy'
import { scanPermissions } from './permissions.js'

export type ComponentKey = 'W' | 'T' | 'C'

export interface Message {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface WorkflowState {
  step: number
  taskInstruction: string
  lastCode: string
  lastObservation: string
  lastError: string
  usage: { costUsd: number; outputTokens: number }
  historyLength: number
}

export type Directive =
  | { action: 'continue' }
  | { action: 'stop'; reason: string }
  | { action: 'inject'; message: string }

export interface FormatObservationInput {
  raw: string
  step: number
  code: string
}

export interface FormatObservationResult {
  text: string
  trace?: Record<string, unknown>
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

export interface ComponentFiles {
  W: string
  T: string
  C: string
}

export interface LoadResult {
  ok: true
  exports: Record<ComponentKey, string[]>
}

export type ComponentModules = Record<ComponentKey, Record<string, unknown>>

// 宿主自身的协议/装载问题：基础设施故障，按批次中断处理
export class ProtocolError extends Error {
  readonly data: Record<string, unknown>

  constructor(message: string, data: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ProtocolError'
    this.data = data
  }
}

// 候选组件真实失败：该任务记 0 分，不是基础设施故障
export class CandidateError extends Error {
  readonly component: ComponentKey
  readonly candidateId: string

  constructor(component: ComponentKey, candidateId: string, message: string) {
    super(message)
    this.name = 'CandidateError'
    this.component = component
    this.candidateId = candidateId
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function errorText(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return String(error)
}

function show(value: unknown): string {
  if (typeof value === 'string') return JSON.stringify(value)
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return Object.prototype.toString.call(value)
  }
}

function isDirective(value: unknown): value is Directive {
  if (!isRecord(value)) return false
  if (value.action === 'continue') return true
  if (value.action === 'stop') return typeof value.reason === 'string'
  if (value.action === 'inject') return typeof value.message === 'string'
  return false
}

function isMessages(value: unknown): value is Message[] {
  return (
    Array.isArray(value) &&
    value.every(
      (message) =>
        isRecord(message) &&
        ['system', 'user', 'assistant'].includes(message.role as string) &&
        typeof message.content === 'string',
    )
  )
}

// 组件的返回值要经 JSON-RPC 出去；形态不对就当场按候选失败处理，别把序列化炸在协议层
export function assertSerializable(value: unknown, component: ComponentKey, candidateId: string): void {
  try {
    JSON.stringify(value)
  } catch (error) {
    throw new CandidateError(component, candidateId, `组件返回值无法序列化：${errorText(error)}`)
  }
}

export class ComponentService extends Service {
  private modules: ComponentModules | null

  constructor(
    ctx: Context,
    readonly candidateId: string,
    modules: ComponentModules,
  ) {
    super(ctx, 'components.' + candidateId)
    this.modules = modules
  }

  // 由 ctx.effect 的清理函数调用：卸载后残留引用继续调用要当场失败
  release(): void {
    this.modules = null
  }

  private loaded(): ComponentModules {
    if (this.modules === null) {
      throw new ProtocolError(`候选 ${this.candidateId} 已卸载，组件不可再调用`)
    }
    return this.modules
  }

  exportNames(): Record<ComponentKey, string[]> {
    const modules = this.loaded()
    return {
      W: Object.keys(modules.W).sort(),
      T: Object.keys(modules.T).sort(),
      C: Object.keys(modules.C).sort(),
    }
  }

  template(): string {
    const value = this.loaded().W.instructionTemplate
    if (typeof value !== 'string') {
      throw new CandidateError('W', this.candidateId, `instructionTemplate 不是字符串：${typeof value}`)
    }
    return value
  }

  beforeStep(state: WorkflowState): Directive {
    const beforeStep = this.loaded().W.beforeStep
    if (typeof beforeStep !== 'function') {
      throw new CandidateError('W', this.candidateId, `beforeStep 不是函数：${typeof beforeStep}`)
    }
    const directive: unknown = (beforeStep as (state: WorkflowState) => unknown)(state)
    if (!isDirective(directive)) {
      throw new CandidateError('W', this.candidateId, `beforeStep 返回形态非法：${show(directive)}`)
    }
    return directive
  }

  formatObservation(input: FormatObservationInput): FormatObservationResult {
    const formatObservation = this.loaded().T.formatObservation
    if (typeof formatObservation !== 'function') {
      throw new CandidateError('T', this.candidateId, `formatObservation 不是函数：${typeof formatObservation}`)
    }
    const result: unknown = (formatObservation as (input: FormatObservationInput) => unknown)(input)
    if (!isRecord(result) || typeof result.text !== 'string') {
      throw new CandidateError('T', this.candidateId, `formatObservation 返回形态非法：${show(result)}`)
    }
    return result as unknown as FormatObservationResult
  }

  buildContext(input: BuildContextInput): BuildContextResult {
    const buildContext = this.loaded().C.buildContext
    if (typeof buildContext !== 'function') {
      throw new CandidateError('C', this.candidateId, `buildContext 不是函数：${typeof buildContext}`)
    }
    const result: unknown = (buildContext as (input: BuildContextInput) => unknown)(input)
    if (!isRecord(result)) {
      throw new CandidateError('C', this.candidateId, `buildContext 返回形态非法：${show(result)}`)
    }
    if ('messages' in result) {
      if (!isMessages(result.messages)) {
        throw new CandidateError('C', this.candidateId, 'buildContext 返回的 messages 不是 {role, content} 数组')
      }
      const trace = result.trace
      if (trace !== undefined && !isRecord(trace)) {
        throw new CandidateError('C', this.candidateId, `buildContext 返回的 trace 不是对象：${show(trace)}`)
      }
      return trace === undefined ? { messages: result.messages } : { messages: result.messages, trace }
    }
    if ('lmRequest' in result) {
      const request = result.lmRequest
      if (!isRecord(request) || typeof request.prompt !== 'string' || typeof request.purpose !== 'string') {
        throw new CandidateError('C', this.candidateId, `buildContext 返回的 lmRequest 形态非法：${show(request)}`)
      }
      return { lmRequest: request as unknown as { prompt: string; purpose: string } }
    }
    throw new CandidateError('C', this.candidateId, `buildContext 返回值既无 messages 也无 lmRequest：${show(result)}`)
  }
}

interface ComponentPluginConfig {
  candidateId: string
  modules: ComponentModules
}

function componentPlugin(ctx: Context, config: ComponentPluginConfig): void {
  const service = new ComponentService(ctx, config.candidateId, config.modules)
  ctx.effect(() => {
    return () => {
      service.release()
    }
  }, `components/${config.candidateId}`)
}

function checkExports(key: ComponentKey, module: Record<string, unknown>, candidateId: string): void {
  const expectations: Record<ComponentKey, [string, string][]> = {
    W: [
      ['instructionTemplate', 'string'],
      ['beforeStep', 'function'],
    ],
    T: [['formatObservation', 'function']],
    C: [['buildContext', 'function']],
  }
  for (const [name, kind] of expectations[key]) {
    if (typeof module[name] !== kind) {
      throw new ProtocolError(
        `候选 ${candidateId} 的 ${key} 缺少导出 ${name}（${kind}），实际 ${typeof module[name]}`,
        { candidate_id: candidateId, component: key },
      )
    }
  }
}

// 组件在独立 vm 上下文求值：全局里只有 console（改道 stderr），没有 process/require/fetch。
// 组件内构造器链（({}).constructor.constructor）拿到的是该上下文的 Function，
// 其全局对象里同样没有 process——静态扫描之外的运行期兜底。
export function loadModuleIsolated(source: string, label: string): Record<string, unknown> {
  const transpiled = ts.transpileModule(source, {
    fileName: label,
    reportDiagnostics: false,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const stderrConsole = {
    log: (...args: unknown[]) => process.stderr.write(args.map(String).join(' ') + '\n'),
    info: (...args: unknown[]) => process.stderr.write(args.map(String).join(' ') + '\n'),
    warn: (...args: unknown[]) => process.stderr.write(args.map(String).join(' ') + '\n'),
    error: (...args: unknown[]) => process.stderr.write(args.map(String).join(' ') + '\n'),
    debug: (...args: unknown[]) => process.stderr.write(args.map(String).join(' ') + '\n'),
  }
  const context = vm.createContext(
    // Buffer/TextEncoder 只做字节计算，碰不到文件与进程；其余 Node 全局一律不进上下文
    { console: stderrConsole, Buffer, TextEncoder },
    { name: `gnosis-component:${label}`, codeGeneration: { strings: false, wasm: false } },
  )
  const exportsObject: Record<string, unknown> = {}
  const moduleObject = { exports: exportsObject }
  // codeGeneration.strings=false 在上下文内禁 eval/Function；wrapper 只注入 exports/module 两个局部
  const wrapped = `(function (exports, module) {\n${transpiled}\n})`
  const factory = vm.runInContext(wrapped, context, { filename: label }) as (
    exports: Record<string, unknown>,
    module: { exports: Record<string, unknown> },
  ) => void
  factory(exportsObject, moduleObject)
  return moduleObject.exports
}

export class ComponentHost {
  private readonly ctx = new Context()
  private readonly fibers = new Map<string, Fiber>()

  async load(candidateId: string, files: ComponentFiles): Promise<LoadResult> {
    if (this.fibers.has(candidateId)) {
      throw new ProtocolError(`候选已装载，不能重复 load：${candidateId}`)
    }
    const modules = { W: {}, T: {}, C: {} } as ComponentModules
    for (const key of ['W', 'T', 'C'] as const) {
      const path = files[key]
      if (typeof path !== 'string' || path.length === 0) {
        throw new ProtocolError(`候选 ${candidateId} 的 ${key} 文件路径缺失`)
      }
      if (!existsSync(path)) {
        throw new ProtocolError(`候选 ${candidateId} 的 ${key} 文件不存在：${path}`, {
          candidate_id: candidateId,
          component: key,
        })
      }
      const bytes = readFileSync(path)
      const source = bytes.toString('utf8')
      const violations = scanPermissions(source, `${key}.ts`)
      if (violations.length > 0) {
        const detail = violations.map((v) => `${v.line}:${v.column} ${v.detail}`).join('; ')
        throw new ProtocolError(`候选 ${candidateId} 的 ${key} 权限违规：${detail}`, {
          candidate_id: candidateId,
          component: key,
        })
      }
      const hash = createHash('sha256').update(bytes).digest('hex').slice(0, 16)
      const module = loadModuleIsolated(source, `${key}-${hash}`)
      checkExports(key, module, candidateId)
      modules[key] = module
    }

    let fiber: Fiber
    try {
      fiber = await this.ctx.plugin(componentPlugin, { candidateId, modules })
    } catch (error) {
      throw new ProtocolError(`候选 ${candidateId} Cordis 装载失败：${errorText(error)}`, {
        candidate_id: candidateId,
      })
    }
    this.fibers.set(candidateId, fiber)
    return { ok: true, exports: this.resolve(candidateId).exportNames() }
  }

  async unload(candidateId: string): Promise<void> {
    const fiber = this.fibers.get(candidateId)
    if (fiber === undefined) {
      throw new ProtocolError(`候选未装载，无法 unload：${candidateId}`)
    }
    await fiber.dispose()
    this.fibers.delete(candidateId)
    if (this.ctx.get('components.' + candidateId) !== undefined) {
      throw new Error(`unload 后服务仍在 Cordis 注册表：components.${candidateId}`)
    }
  }

  async unloadAll(): Promise<void> {
    for (const candidateId of [...this.fibers.keys()]) {
      await this.unload(candidateId)
    }
  }

  resolve(candidateId: string): ComponentService {
    if (!this.fibers.has(candidateId)) {
      throw new ProtocolError(`候选未装载：${candidateId}`)
    }
    const service = this.ctx.get('components.' + candidateId) as ComponentService | undefined
    if (service === undefined) {
      throw new ProtocolError(`候选 ${candidateId} 已装载但 Cordis 注册表里没有服务`)
    }
    return service
  }
}
