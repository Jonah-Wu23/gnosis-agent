import { Context, Service } from 'cordis'

interface CounterConfig {
  initial: number
}

const lifecycle: string[] = []

class CounterService extends Service {
  private count: number

  constructor(ctx: Context, initial: number) {
    super(ctx, 'counter')
    this.count = initial
  }

  increment(step = 1): number {
    this.count += step
    return this.count
  }

  get value(): number {
    return this.count
  }
}

declare module 'cordis' {
  interface Context {
    counter: CounterService
  }
}

function counterPlugin(ctx: Context, config: CounterConfig) {
  new CounterService(ctx, config.initial)
  ctx.effect(() => {
    lifecycle.push('counter:setup')
    return () => {
      lifecycle.push('counter:dispose')
    }
  }, 'counter/lifecycle')
}

function check(condition: boolean, message: string): asserts condition {
  if (!condition) throw new Error(`boot self-check failed: ${message}`)
}

console.log('[boot] stage 1: 创建根 Context')
const ctx = new Context()
check(ctx.registry.size === 0, `registry 初始不为空: ${ctx.registry.size}`)

console.log('[boot] stage 2: 装载 counter 组件')
const fiber = await ctx.plugin(counterPlugin, { initial: 10 })
check(ctx.get('counter') !== undefined, '插件装载后服务不可解析')
check(lifecycle.join(',') === 'counter:setup', `setup 未执行: ${lifecycle.join(',')}`)

console.log('[boot] stage 3: 调用服务')
const a = ctx.counter.increment(2)
const b = ctx.counter.increment()
const value = ctx.counter.value
check(a === 12, `increment(2) 期望 12，实际 ${a}`)
check(b === 13, `increment() 期望 13，实际 ${b}`)
check(value === 13, `value 期望 13，实际 ${value}`)
console.log(`[boot]    counter.value = ${value}`)

console.log('[boot] stage 4: 卸载 counter 组件')
await fiber.dispose()
check(ctx.get('counter') === undefined, 'dispose 后服务仍可解析')
check(fiber.inertia === undefined, `dispose 后仍有未完成的副作用: ${String(fiber.inertia)}`)
check(
  lifecycle.join(',') === 'counter:setup,counter:dispose',
  `effect 清理未执行或顺序错误: ${lifecycle.join(',')}`,
)
console.log(`[boot]    lifecycle = ${lifecycle.join(' -> ')}`)

console.log('[boot] PASS: context 创建 / 组件装载 / 服务调用 / 组件卸载 全部通过')
