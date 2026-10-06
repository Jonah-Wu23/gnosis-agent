// 受测组件只在 QuickJS 中运行；宿主对象不会传入解释器。
import { getQuickJS } from 'quickjs-emscripten'
import { Context, Service } from 'cordis'
import ts from 'typescript-legacy'
import { createMessageConnection, ResponseError, StreamMessageReader, StreamMessageWriter } from 'vscode-jsonrpc/lib/node/main.js'

const QuickJS = await getQuickJS()
const connection = createMessageConnection(new StreamMessageReader(process.stdin), new StreamMessageWriter(process.stdout))
let loaded
let deadline = 0
const root = new Context()
let fiber

class RuntimePlugin extends Service {
  constructor(ctx, candidate) { super(ctx, 'components.' + candidate); this.candidate = candidate }
  invoke(params, method, input) { return invokeIsolated(params, method, input) }
}

function scopedPlugin(ctx, config) {
  new RuntimePlugin(ctx, config.candidate)
  ctx.effect(() => () => release())
}

function reject(component, candidate, message, denied = false) {
  throw new ResponseError(-32000, message, {
    candidate_error: true, candidate_id: candidate, component, permission_denied: denied,
  })
}

function unwrap(result, component, candidate) {
  if (result.error) {
    const error = loaded.context.dump(result.error)
    result.error.dispose()
    reject(component, candidate, `${error.name}: ${error.message}`, error.name === 'PermissionDenied')
  }
  return result.value
}

function release() {
  if (!loaded) return
  loaded.exports?.dispose()
  loaded.context.dispose()
  loaded.runtime.dispose()
  loaded = undefined
}

connection.onRequest('load', async ({ candidate_id, component, source }) => {
  if (loaded) throw new ResponseError(-32001, '一个隔离进程只能装载一个组件')
  const output = ts.transpileModule(source, {
    fileName: `${component}.ts`, reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  })
  if (output.diagnostics.length) {
    reject(component, candidate_id, output.diagnostics.map(d => `${d.code}: ${ts.flattenDiagnosticMessageText(d.messageText, ' ')}`).join('\n'))
  }
  const runtime = QuickJS.newRuntime()
  runtime.setMemoryLimit(64 * 1024 * 1024)
  runtime.setMaxStackSize(1024 * 1024)
  runtime.setInterruptHandler(() => Date.now() > deadline)
  runtime.setModuleLoader(name => ({ error: Object.assign(new Error(`禁止加载外部模块 ${name}`), { name: 'PermissionDenied' }) }))
  const context = runtime.newContext()
  loaded = { candidate_id, component, runtime, context }
  deadline = Date.now() + 2000
  const setup = context.evalCode(`
    class PermissionDenied extends Error { constructor(name) { super('禁止访问能力 ' + name); this.name = 'PermissionDenied'; } }
    for (const name of ['process', 'require', 'fetch', 'Buffer', 'TextEncoder', 'Deno', 'Bun', '__dirname', '__filename']) {
      Object.defineProperty(globalThis, name, { get() { throw new PermissionDenied(name); }, configurable: false });
    }
  `)
  unwrap(setup, component, candidate_id).dispose()
  const consoleHandle = context.newObject()
  for (const name of ['log', 'info', 'warn', 'error', 'debug']) {
    const log = context.newFunction(name, (...args) => {
      connection.sendNotification('component.log', { unit: candidate_id, level: name, values: args.map(value => context.dump(value)) })
    })
    context.setProp(consoleHandle, name, log)
    log.dispose()
  }
  context.setProp(context.global, 'console', consoleHandle)
  consoleHandle.dispose()
  loaded.exports = unwrap(context.evalCode(output.outputText, `${component}.js`, { type: 'module' }), component, candidate_id)
  const expected = { T: 'formatObservation', C: 'buildContext', W: 'beforeStep' }[component]
  const fn = context.getProp(loaded.exports, expected)
  const valid = context.typeof(fn) === 'function'
  fn.dispose()
  if (!valid) reject(component, candidate_id, `缺少导出函数 ${expected}`)
  fiber = await root.plugin(scopedPlugin, { candidate: candidate_id })
  const plugin = 'components.' + candidate_id
  if (!root.get(plugin)) throw new ResponseError(-32001, 'Cordis 插件注册失败')
  return { ok: true, exports: [expected], plugin, interpreter: 'QuickJS' }
})

function invokeIsolated(params, method, input) {
  if (!loaded || loaded.candidate_id !== params.candidate_id) throw new ResponseError(-32001, '当前组件未装载')
  const { context, component, candidate_id } = loaded
  deadline = Date.now() + 2000
  const argument = unwrap(context.evalCode(`JSON.parse(${JSON.stringify(JSON.stringify(input))})`), component, candidate_id)
  const fn = context.getProp(loaded.exports, method)
  try {
    const result = unwrap(context.callFunction(fn, context.undefined, argument), component, candidate_id)
    try {
      const value = context.dump(result)
      if (!value || typeof value !== 'object' || Array.isArray(value)) reject(component, candidate_id, '组件必须返回 JSON 对象')
      return value
    } finally {
      result.dispose()
    }
  } finally {
    argument.dispose()
    fn.dispose()
  }
}

function invoke(params, method, input) {
  const service = root.get('components.' + params.candidate_id)
  if (!service) throw new ResponseError(-32001, 'Cordis 插件未注册')
  return service.invoke(params, method, input)
}

connection.onRequest('t.formatObservation', p => invoke(p, 'formatObservation', { raw: p.raw, step: p.step, code: p.code }))
connection.onRequest('c.buildContext', p => invoke(p, 'buildContext', p.input))
connection.onRequest('w.beforeStep', p => invoke(p, 'beforeStep', p.state))
connection.onRequest('unload', async () => { if (fiber) await fiber.dispose(); else release(); return { ok: true } })
connection.onRequest('shutdown', async () => { if (fiber) await fiber.dispose(); else release(); setTimeout(() => process.exit(0), 20); return { ok: true } })
connection.listen()
