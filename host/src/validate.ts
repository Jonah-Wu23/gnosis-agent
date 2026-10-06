// 组件校验：权限扫描 → 语法检查 → 临时装载 → 导出与形态检查 → 假数据冒烟。
// 校验结果是给模型的反馈，因此聚合全部问题、每条都带位置与可行动描述，不遇到第一个就抛。
import ts from 'typescript-legacy'
import { createHash } from 'node:crypto'
import { scanPermissions } from './permissions.js'
import { loadModuleIsolated } from './loader.js'

export type ComponentKey = 'W' | 'T' | 'C'

export interface ValidateResult {
  ok: boolean
  errors: string[]
  exports: Record<ComponentKey, string[]>
}

const COMPONENT_KEYS: readonly ComponentKey[] = ['W', 'T', 'C']

// 契约 §2 指定的占位符：少一个模板就渲染不出完整指令
const REQUIRED_PLACEHOLDERS = [
  '{{ instruction }}',
  '{{ main_user.first_name }}',
  '{{ main_user.last_name }}',
  '{{ main_user.email }}',
  '{{ main_user.phone_number }}',
  '{{ app_descriptions }}',
]

// Python 侧 text_to_messages 的角色切分模式（re.IGNORECASE）：标记后紧跟换行即被当成消息起点
const ROLE_MARKER = /(USER|ASSISTANT|SYSTEM):\r?\n/gi
const ROLE_LINE = /^(USER|ASSISTANT|SYSTEM):\r?\n/i

const FAKE_WORKFLOW_STATE = {
  step: 1,
  taskInstruction: '校验用假任务',
  lastCode: 'print(1)',
  lastObservation: 'Output:\n```\n1\n```\n\n',
  lastError: '',
  usage: { costUsd: 0.01, outputTokens: 16 },
  historyLength: 3,
}

const FAKE_CONTEXT_INPUT = {
  task: { id: 'validate-smoke', instruction: '校验用假任务' },
  messages: [
    { role: 'user', content: 'system instruction' },
    { role: 'assistant', content: 'print(1)' },
    { role: 'user', content: 'Output:\n```\n1\n```\n\n' },
  ],
  step: 1,
  budget: { maxSteps: 10, remainingSteps: 9, remainingCostUsd: 1.5, maxOutputTokensPerTask: 100000 },
}

function errorText(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return String(error)
}

function show(value: unknown): string {
  if (typeof value === 'string') return JSON.stringify(value.length > 120 ? value.slice(0, 120) + '…' : value)
  try {
    const text = JSON.stringify(value)
    if (text === undefined) return String(value)
    return text.length > 120 ? text.slice(0, 120) + '…' : text
  } catch {
    return Object.prototype.toString.call(value)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function syntaxErrors(source: string, fileName: string): string[] {
  const emitted = ts.transpileModule(source, {
    fileName,
    reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext },
  })
  return (emitted.diagnostics ?? []).map((diagnostic) => {
    const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, ' ')
    if (diagnostic.file !== undefined && diagnostic.start !== undefined) {
      const { line, character } = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start)
      return `${line + 1}:${character + 1} ${message}`
    }
    return message
  })
}

function checkTemplate(template: string): string[] {
  const problems: string[] = []
  for (const placeholder of REQUIRED_PLACEHOLDERS) {
    if (!template.includes(placeholder)) {
      problems.push(`instructionTemplate 缺少占位符 ${placeholder}`)
    }
  }
  if (!ROLE_LINE.test(template)) {
    problems.push('instructionTemplate 必须以行首角色标记（USER:/ASSISTANT:/SYSTEM:）开头')
  }
  // 行内出现角色标记会被 Python 侧切分器当成新消息起点，与“标记位于行首”的约定冲突
  ROLE_MARKER.lastIndex = 0
  for (const match of template.matchAll(ROLE_MARKER)) {
    const index = match.index ?? 0
    const atLineStart = index === 0 || template[index - 1] === '\n'
    if (!atLineStart) {
      const line = template.slice(0, index).split('\n').length
      problems.push(`instructionTemplate 第 ${line} 行的角色标记 ${match[1]}: 不在行首`)
    }
  }
  const lines = template.endsWith('\n') ? template.slice(0, -1).split('\n') : template.split('\n')
  if (lines[lines.length - 1] !== 'Task: {{ instruction }}') {
    problems.push('instructionTemplate 末尾 USER: 段必须以行 `Task: {{ instruction }}` 收尾')
  }
  return problems
}

function checkDirective(value: unknown): string[] {
  if (!isRecord(value)) return [`beforeStep 必须返回 Directive 对象，实际 ${show(value)}`]
  const action = value.action
  if (action === 'continue') return []
  if (action === 'stop') {
    return typeof value.reason === 'string' ? [] : ['beforeStep 返回 stop 时必须带字符串 reason']
  }
  if (action === 'inject') {
    return typeof value.message === 'string' ? [] : ['beforeStep 返回 inject 时必须带字符串 message']
  }
  return [`beforeStep 返回的 action 非法：${show(action)}`]
}

function checkMessages(value: unknown): string[] {
  if (!Array.isArray(value)) return [`messages 必须是数组，实际 ${show(value)}`]
  const problems: string[] = []
  value.forEach((message, index) => {
    if (!isRecord(message)) {
      problems.push(`messages[${index}] 不是对象：${show(message)}`)
      return
    }
    if (!['system', 'user', 'assistant'].includes(message.role as string)) {
      problems.push(`messages[${index}].role 非法：${show(message.role)}`)
    }
    if (typeof message.content !== 'string') {
      problems.push(`messages[${index}].content 必须是字符串：${show(message.content)}`)
    }
  })
  return problems
}

function checkBuildContextResult(value: unknown): string[] {
  if (!isRecord(value)) return [`buildContext 必须返回 BuildContextResult，实际 ${show(value)}`]
  if ('messages' in value) {
    const problems = checkMessages(value.messages)
    if (value.trace !== undefined && !isRecord(value.trace)) problems.push('trace 必须是对象')
    return problems
  }
  if ('lmRequest' in value) {
    const request = value.lmRequest
    if (!isRecord(request)) return [`lmRequest 必须是对象：${show(request)}`]
    const problems: string[] = []
    if (typeof request.prompt !== 'string') problems.push(`lmRequest.prompt 必须是字符串：${show(request.prompt)}`)
    if (typeof request.purpose !== 'string') problems.push(`lmRequest.purpose 必须是字符串：${show(request.purpose)}`)
    if (request.maxTokens !== undefined && typeof request.maxTokens !== 'number') {
      problems.push(`lmRequest.maxTokens 必须是数字：${show(request.maxTokens)}`)
    }
    return problems
  }
  return [`buildContext 返回值既无 messages 也无 lmRequest：${show(value)}`]
}

function checkExports(key: ComponentKey, module: Record<string, unknown>): string[] {
  const problems: string[] = []
  if (key === 'W') {
    if (typeof module.instructionTemplate !== 'string') {
      problems.push(`必须导出字符串 instructionTemplate，实际 ${typeof module.instructionTemplate}`)
    } else {
      problems.push(...checkTemplate(module.instructionTemplate))
    }
    if (typeof module.beforeStep !== 'function') {
      problems.push(`必须导出函数 beforeStep，实际 ${typeof module.beforeStep}`)
    }
    return problems
  }
  if (key === 'T') {
    if (typeof module.formatObservation !== 'function') {
      problems.push(`必须导出函数 formatObservation，实际 ${typeof module.formatObservation}`)
    }
    return problems
  }
  if (typeof module.buildContext !== 'function') {
    problems.push(`必须导出函数 buildContext，实际 ${typeof module.buildContext}`)
  }
  return problems
}

function runSmoke(key: ComponentKey, module: Record<string, unknown>): string[] {
  if (key === 'W') {
    const beforeStep = module.beforeStep as (state: unknown) => unknown
    let directive: unknown
    try {
      directive = beforeStep(FAKE_WORKFLOW_STATE)
    } catch (error) {
      return [`beforeStep 冒烟调用抛出异常：${errorText(error)}`]
    }
    return checkDirective(directive)
  }
  if (key === 'T') {
    const formatObservation = module.formatObservation as (input: unknown) => unknown
    let result: unknown
    try {
      result = formatObservation({ raw: 'hello', step: 0, code: '' })
    } catch (error) {
      return [`formatObservation 冒烟调用抛出异常：${errorText(error)}`]
    }
    if (!isRecord(result)) return [`formatObservation 必须返回对象，实际 ${show(result)}`]
    const problems: string[] = []
    if (typeof result.text !== 'string') problems.push(`formatObservation 返回值 text 必须是字符串：${show(result.text)}`)
    if (result.trace !== undefined && !isRecord(result.trace)) problems.push('trace 必须是对象')
    return problems
  }
  const buildContext = module.buildContext as (input: unknown) => unknown
  let result: unknown
  try {
    result = buildContext(FAKE_CONTEXT_INPUT)
  } catch (error) {
    return [`buildContext 冒烟调用抛出异常：${errorText(error)}`]
  }
  return checkBuildContextResult(result)
}

async function validateComponent(
  key: ComponentKey,
  source: string,
  errors: string[],
  exportNames: Record<ComponentKey, string[]>,
): Promise<void> {
  const prefix = `${key}: `
  for (const violation of scanPermissions(source, `${key}.ts`)) {
    errors.push(`${prefix}权限违规 ${violation.line}:${violation.column} ${violation.detail}`)
  }
  const syntax = syntaxErrors(source, `${key}.ts`)
  for (const message of syntax) errors.push(`${prefix}语法错误 ${message}`)
  // 语法不通过时装载必然失败，后续检查只会产生噪音，就此打住
  if (syntax.length > 0) return

  const hash = createHash('sha256').update(source, 'utf8').digest('hex').slice(0, 12)

  let module: Record<string, unknown>
  try {
    // 与正式装载同一隔离通道（vm 上下文），校验结果才代表真实装载行为
    module = loadModuleIsolated(source, `${key}-validate-${hash}`)
  } catch (error) {
    errors.push(`${prefix}装载失败 ${errorText(error)}`)
    return
  }

  exportNames[key] = Object.keys(module).sort()
  const shape = checkExports(key, module)
  for (const problem of shape) errors.push(prefix + problem)
  if (shape.length > 0) return
  for (const problem of runSmoke(key, module)) errors.push(prefix + problem)
}

export async function validateSources(
  sources: Partial<Record<ComponentKey, string>>,
): Promise<ValidateResult> {
  const errors: string[] = []
  const exportNames: Record<ComponentKey, string[]> = { W: [], T: [], C: [] }
  for (const key of COMPONENT_KEYS) {
    const source = sources[key]
    if (source === undefined) continue
    await validateComponent(key, source, errors, exportNames)
  }
  return { ok: errors.length === 0, errors, exports: exportNames }
}
