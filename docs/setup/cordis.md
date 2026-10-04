# Cordis 宿主环境

本文记录 `host/` 这一 npm 工程的搭建结果与真实校验输出，覆盖版本、运行命令、以及 cordis 类型声明在当前版本下的一个上游缺陷和应对方式。

## 一、版本

版本在 npm registry 上查证后写入 `host/package.json`，全部为精确版本，不带 `^` 或 `~`。

| 包 | 版本 | 锁定理由 |
| --- | --- | --- |
| cordis | `4.0.0-rc.10` | v4 线的最新小版本。cordis 的 README 把论文《A Programming Paradigm for Spatiotemporal Composability》(arXiv:2608.25512) 列为对应论文，论文实现取 v4 线 |
| typescript | `7.0.2` | registry `latest` |
| tsx | `4.23.15` | registry `latest`，也是 TS 源码的直接运行方式 |
| @types/node | `24.19.1` | 宿主 Node 为 v24.14.0，取 24.x 最新，避免用 26.x 声明引入宿主不存在的 API |

查证记录：`npm view cordis dist-tags` 返回 `{"next":"4.0.0-beta.5","latest":"4.0.0-rc.10"}`。cordis 4 线目前只有 `4.0.0-alpha.*`、`4.0.0-beta.*`、`4.0.0-rc.*`，没有正式版，`4.0.0-rc.10` 就是 v4 最新版本。cordis 的 `peerDependencies` 是 `@cordisjs/plugin-loader` 与 `@cordisjs/plugin-include`，两者都标了 `optional`，npm 不会自动安装；它们只在用 cordis 自带的 `cordis` CLI 加 yml 配置装载场景才需要，本工程用代码装载组件，不装这两个包。

依赖树（`npm ls --depth=0`）：`@types/node@24.19.1`、`cordis@4.0.0-rc.10`（内含 `cosmokit@1.8.1`、`@standard-schema/spec@1.1.0`）、`tsx@4.23.15`（内含 `esbuild@0.28.2`）、`typescript@7.0.2`。

## 二、运行

```bash
cd host
npm ci              # 或 npm install
npm run typecheck   # tsc --noEmit
npm run boot        # tsx src/boot.ts
```

`host/package.json` 里 `boot` 与 `typecheck` 两个脚本就对应上面两条；直接 `npx tsx src/boot.ts` 也能跑通，不需要额外参数。`package-lock.json` 已生成，`npm ci` 可复现同一套依赖（已实测：删掉 `node_modules` 后 `npm ci`，两条命令均通过）。

## 三、boot 校验的真实输出

`npm run boot` 在 2026-10-04 于本机的完整输出：

```text
> @gnosis/host@0.0.0 boot
> tsx src/boot.ts

[boot] stage 1: 创建根 Context
[boot] stage 2: 装载 counter 组件
[boot] stage 3: 调用服务
[boot]    counter.value = 13
[boot] stage 4: 卸载 counter 组件
[boot]    lifecycle = counter:setup -> counter:dispose
[boot] PASS: context 创建 / 组件装载 / 服务调用 / 组件卸载 全部通过
```

`npm run typecheck` 无输出、退出码 0。

`host/src/boot.ts` 校验的四件事，每一件都有断言，断言失败会抛错并终止：

1. `new Context()` 得到根上下文，初始 registry 为空。
2. `await ctx.plugin(counterPlugin, { initial: 10 })` 装载 `CounterService`（`Service` 子类，自动通过 `ctx.provide` 注册为 `ctx.counter`），装载后 `ctx.get('counter')` 可解析，插件的 `ctx.effect` 立即执行并记录 `counter:setup`。
3. 调用服务：`increment(2)` 返回 12，`increment()` 返回 13，`value` 为 13。
4. `await fiber.dispose()` 之后，`ctx.get('counter')` 返回 `undefined`（服务注销），`fiber.inertia` 为空（无未完成副作用），effect 的 disposer 记录 `counter:dispose`。

第 2 步与第 4 步对同一个表达式 `ctx.get('counter')` 分别断言“可解析”与“不可解析”，两个方向都通过，说明这两条断言确实在检查服务注册表的真实状态，不是恒真。

cordis v4 在本工程中用到的 API 面：`Context` 构造函数、`Service` 抽象类的 `super(ctx, name)` 自动注册、`ctx.plugin(plugin, config)`（返回可 await 的 `Fiber`）、`ctx.effect(execute, label)`、`ctx.get(name, strict?)`、`fiber.dispose()`。装载没有单独的启动步骤，`await ctx.plugin(...)` 返回时插件已进入活动状态。

## 四、注意事项

### 4.1 cordis 4.0.0-rc.10 的类型声明在 NodeNext 下不可用

`node_modules/cordis/lib/*.d.ts` 里所有相对导入都省略了扩展名（`export * from './context'`、`import { Fiber } from './fiber'`、`import type { Context, Service } from '.'`）。NodeNext 模块解析不接受这种写法，TypeScript 报 `TS2834: Relative import paths need explicit file extensions in ECMAScript imports` 26 处，另有 `utils.d.ts` 的 `import type { Context, Service } from '.'` 报 1 处 `TS2307`，合计落在 8 个声明文件里（`lib/index.d.ts` 自身还有 7 处同类问题，类型入口改走 `types/` 之后这个文件不再进入解析路径）。

后果有两层：`lib/index.d.ts` 的 `export *` 全部解析失败，从 `cordis` 导入 `Context`、`Service` 时前者只剩类型、后者直接报 `TS2305: Module '"cordis"' has no exported member 'Service'`；各文件里 `declare module './context'` 的 `Context` 增强块同样解析不到目标模块，`ctx.plugin`、`ctx.get`、`ctx.effect` 这类方法在类型上全部消失。运行时不受影响，`lib/index.js` 是单文件打包产物。

`host/types/` 下的三个文件承担类型补齐，都不改动任何运行时行为：

| 文件 | 作用 |
| --- | --- |
| `types/cordis.d.ts` | `tsconfig.json` 的 `paths` 把 `cordis` 指向它，TypeScript 与 tsx 都会加载它，内容只有一行 `export * from './cordis-types.js'` |
| `types/cordis-types.d.ts` | 类型面：用显式 `.js` 扩展名重新导出 cordis 的 8 个声明文件，并把 `Context`、`Intercept` 的声明合并块照抄一遍（模块名换成可解析的相对路径），恢复增强 |
| `types/cordis-types.js` | 运行时面：转出 cordis 的真实打包产物 `lib/index.js` |

分工的原因：`paths` 的替换目标按字面路径解析，不做 `.js` 到 `.d.ts` 的替换，所以单个文件无法同时满足两个工具。TypeScript 走 `cordis.d.ts` → `cordis-types.js` 的 `.d.ts` 孪生文件拿到类型；tsx 走同样的相对说明符，但在运行时命中 `cordis-types.js` 这个真实 JS 文件。tsx 会读 `tsconfig.json` 的 `paths`，如果 `paths` 直接指向 `.d.ts`，运行时就会去加载声明文件并失败，因此这个入口文件不能省。

`types/cordis-types.d.ts` 里增强块的成员签名与上游逐字一致。cordis 升级后需要对照 `node_modules/cordis/lib/*.d.ts` 核对；上游一旦发布 NodeNext 可解析的声明，删掉 `types/` 整个目录与 `tsconfig.json` 的 `paths` 配置即可，其余配置不受影响。

### 4.2 模块与类型配置

`host/tsconfig.json` 用 `module: NodeNext` 加 `moduleResolution: NodeNext`，配合 `package.json` 的 `"type": "module"`，工程内相对导入必须带 `.js` 扩展名。`strict` 之外额外开了 `noUncheckedIndexedAccess`、`exactOptionalPropertyTypes`、`noImplicitOverride`、`noFallthroughCasesInSwitch`、`verbatimModuleSyntax`、`isolatedModules`。`target` 与 `lib` 为 `ES2023`，`types` 限定为 `["node"]`，`noEmit` 为真（运行交给 tsx，类型检查交给 tsc）。

### 4.3 cordis v4 处于预发布状态

`4.0.0-rc.10` 是 release candidate，官方 README 写着 “Cordis is under active development. The API is not yet stable and may change without notice.”。四类可替换组件（工作流、工具包装、上下文组织、技能）铺开前，应先按本文第三节的方式把每类组件的最小装载与卸载跑通，确认生命周期行为符合预期再往上叠。v3 线最后一版是 `3.18.1`，与 v4 同属 cordis 包的不同大版本，本工程按论文实现取 v4，不混用两线的 API。

### 4.4 工程边界

`host/` 是独立 npm 工程，`host/node_modules/` 由仓库根的 `.gitignore` 覆盖。本文写作时未执行任何 git 提交。
