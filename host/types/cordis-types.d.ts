// cordis@4.0.0-rc.10 打包出的 lib/*.d.ts 全部使用无扩展名的相对导入
// （export * from './context'、import { Fiber } from './fiber' 等）。
// NodeNext 模块解析拒绝这种写法（TS2834），于是 lib/index.d.ts 的导出被全部置空，
// 各文件里 declare module './context' 的 Context 增强块也因无法解析而失效。
//
// 本文件只补类型面，不改变任何运行时行为：
// 1. 用显式 .js 扩展名重新导出 cordis 的 8 个声明文件，恢复导出面；
// 2. 把这些文件中对 Context / Intercept 的声明合并照抄一遍，把模块名换成可解析的路径，恢复增强。
// 第 2 步的成员签名与上游逐字一致，cordis 升级后需要对照 node_modules/cordis/lib/*.d.ts 校对。
// 运行时由同目录的 cordis-types.js 承担，入口是 cordis.d.ts；上游发布 NodeNext 可解析的声明后，
// 删除 types/ 整个目录与 tsconfig.json 的 paths 配置。

import type { Dict, Promisify } from '../node_modules/cosmokit/lib/index.js'

import type { Context } from '../node_modules/cordis/lib/context.js'
import type { EventOptions, Events, Parameters, ReturnType, ThisType } from '../node_modules/cordis/lib/events.js'
import type { Fiber } from '../node_modules/cordis/lib/fiber.js'
import type { LoggerService } from '../node_modules/cordis/lib/logger.js'
import type { Property } from '../node_modules/cordis/lib/reflect.js'
import type { Inject, Plugin } from '../node_modules/cordis/lib/registry.js'

export * from '../node_modules/cordis/lib/context.js'
export * from '../node_modules/cordis/lib/events.js'
export * from '../node_modules/cordis/lib/fiber.js'
export * from '../node_modules/cordis/lib/logger.js'
export * from '../node_modules/cordis/lib/reflect.js'
export * from '../node_modules/cordis/lib/registry.js'
export * from '../node_modules/cordis/lib/service.js'
export * from '../node_modules/cordis/lib/utils.js'

type Spread<T> = undefined extends T ? [config?: T] : [config: T]
type GetPluginParameters<P> = P extends (ctx: Context, ...args: infer R) => any
  ? R
  : P extends new (ctx: Context, ...args: infer R) => any
    ? R
    : P extends { apply(ctx: Context, ...args: infer R): any }
      ? R
      : never
type GetPluginConfig<P> = P extends Plugin.Transform<infer S, any> ? S : GetPluginParameters<P>[0]

declare module '../node_modules/cordis/lib/context.js' {
  interface Context extends Pick<Fiber, 'effect'> {
    fiber: Fiber
    inject(deps: Inject, callback: Plugin.Function<void>): Fiber & PromiseLike<Fiber>
    plugin<P extends Plugin>(plugin: P, ...args: Spread<GetPluginConfig<P>>): Fiber & PromiseLike<Fiber>
    get<K extends string & keyof this>(name: K, strict?: boolean): undefined | this[K]
    get(name: string, strict?: boolean): any
    set<K extends string & keyof this>(name: K, value: undefined | this[K]): void
    set(name: string, value: any): void
    provide<K extends string & keyof this>(name: K, value: undefined | this[K]): () => void
    provide(name: string, value?: any): () => void
    accessor(name: string, options: Omit<Property.Accessor, 'type'>): void
    mixin<K extends string & keyof this>(name: K, mixins: (keyof this & keyof this[K])[] | Dict<string>): void
    mixin<T extends {}>(source: T, mixins: (keyof this & keyof T)[] | Dict<string>): void
    parallel<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): Promise<void>
    parallel<K extends keyof Events>(thisArg: NoInfer<ThisType<Events[K]>>, name: K, ...args: Parameters<Events[K]>): Promise<void>
    emit<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): void
    emit<K extends keyof Events>(thisArg: NoInfer<ThisType<Events[K]>>, name: K, ...args: Parameters<Events[K]>): void
    serial<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): Promisify<ReturnType<Events[K]>>
    serial<K extends keyof Events>(thisArg: NoInfer<ThisType<Events[K]>>, name: K, ...args: Parameters<Events[K]>): Promisify<ReturnType<Events[K]>>
    bail<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): ReturnType<Events[K]>
    bail<K extends keyof Events>(thisArg: NoInfer<ThisType<Events[K]>>, name: K, ...args: Parameters<Events[K]>): ReturnType<Events[K]>
    waterfall<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): ReturnType<Events[K]>
    waterfall<K extends keyof Events>(thisArg: NoInfer<ThisType<Events[K]>>, name: K, ...args: Parameters<Events[K]>): ReturnType<Events[K]>
    on<K extends keyof Events>(name: K, listener: Events[K], options?: boolean | EventOptions): () => boolean
    once<K extends keyof Events>(name: K, listener: Events[K], options?: boolean | EventOptions): () => boolean
  }

  interface Intercept {
    logger: LoggerService.Intercept
  }
}
