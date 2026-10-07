# GNOSIS Agent

**当前状态（2026-10-07）**：模型服务切换到 DeepSeek 官方 API（`deepseek-flash`），费用上限全部解除，此前全部实验结果报废归档到 `archive/2026-10-07-void/`。当前程序版本 `0d4c2efb…` 已通过 103 项无模型调用的离线检查（`state/research_offline_check.json`），完成 4 组 5 次留出重复评估设计、逐条证据复用、任务级配对置换统计契约与账本全口径汇总修复，B1 基线（30 题通过率 70%）与历史执行断点无损复用。断点按完整消息校验，修复提议显式允许动态计费字段变化；严格续跑缺少响应时保留账本并报错。交付记录见 [docs/GNOSIS-官方API切换交付-2026-10-07.md](docs/GNOSIS-官方API切换交付-2026-10-07.md)，科研计划见 [docs/GNOSIS-科研计划.md](docs/GNOSIS-科研计划.md)，产品边界见 [docs/GNOSIS-产品PRD.md](docs/GNOSIS-产品PRD.md)，改造依据见 [docs/GNOSIS-先导后计划.md](docs/GNOSIS-先导后计划.md)。

## 一句话

GNOSIS 让 Agent 连续修复、组合经验：经验由可执行组件承载，组件第一次实际影响执行的扩展位置决定哪些历史执行必须重跑，省下的验证费用投入继续修复。

## 目录

| 位置 | 用途 |
|---|---|
| `engine/` | 执行、自主修复、激活边界、发布门禁与研究编排 |
| `host/` | Cordis 插件宿主与 QuickJS 组件隔离运行库 |
| `config/research_selection.json` | 固定任务划分（30 开发 / 6 独立选择 / 30 最终留出 / 6 中途检查）、组额度与上游提交 |
| `config/runtime.json` | 私有运行配置（已忽略） |
| `state/` | 研究账本 `research_ledger.sqlite` 与离线准入证明，受测 AI 无权访问 |
| `experiments/research/` | 正式实验输出，首次 `baseline` 或 `run` 时创建 |
| `.offline_checks/` | 离线检查的临时工作区 |
| `archive/2026-10-07-void/` | 报废的先导与旧账本；先导轨迹只作离线机制检查样本 |
| `scratch/AutomationBench/`、`scratch/ace/`、`scratch/StarHarness/` | 固定版本的上游代码 |

| 模块 | 职责 |
|---|---|
| `engine/activation.py` | 作用域互斥、扩展位置登记、边界轨迹、验证范围与证据复用 |
| `engine/extension_protocol.py` | 可执行组件 E 的输出协议 |
| `engine/automation_adapter.py` | AutomationBench 接入层：成功、错误、搜索与组件调用统一经过扩展位置 |
| `engine/evolution.py` | 工作/发布/冻结三版本、成熟条件、对称配对比较、研究预算 |
| `engine/run_research.py` | 统一入口、修复控制器、实验编排、费用汇总 |
| `engine/research_evaluator.py` | 原装任务与评分器上的执行记录 |
| `engine/research_model.py` | 修复请求断点与官方 ACE 传输 |
| `engine/research_retest.py` | 同一批冻结候选上的全局重测与按影响范围重测 |
| `engine/research_offline.py` | 无模型调用的离线检查 |
| `engine/research_plan.py`、`engine/research_upstream.py` | 任务划分、组额度、上游固定提交 |

## 入口

全部命令在 WSL 中执行：

```bash
cd '/mnt/e/AI/GNOSIS Agent'
export PYTHONPATH=scratch/AutomationBench:.
PY=scratch/AutomationBench/.venv/bin/python
```

| 命令 | 作用 | 调用模型 |
|---|---|---|
| `$PY -m engine.run_research prepare` | 生成并核对固定任务划分 | 否 |
| `GNOSIS_DISABLE_MODEL_CALLS=1 $PY -m engine.run_research unittests` | 秒级单元检查（43 条） | 否 |
| `GNOSIS_DISABLE_MODEL_CALLS=1 $PY -m engine.run_research offline` | 机制检查（97 条），写准入证明 | 否 |
| `GNOSIS_DISABLE_MODEL_CALLS=1 $PY -m engine.run_research offline --replay` | 追加 30 题先导轨迹全量重放 | 否 |
| `$PY -m engine.run_research baseline` | 只跑 B1（30 道开发题）与基线门槛，打印结果与账本 | 是 |
| `$PY -m engine.run_research run` | 完整付费实验，B1 按断点复用 | 是 |
| `$PY -m engine.run_research retest` | 单独重跑固定候选重测比较（`run` 已包含） | 是 |

`run` 启动时核对准入证明与当前程序版本一致，代码一改就要重跑 `offline`。中断后再次执行 `run`，已完成的执行、修复操作和请求都按断点复用，不重复计费。

## 实验编排

`run` 按顺序执行：

1. B1：固定 Agent 在 30 道开发题上取修正基线，结果写入 `baseline_gate.json`。截断占失败一半及以上时停止，学习不开跑。
2. 官方 ACE、全局验证组与完整 GNOSIS 三个学习组同时运行。ACE 逐题更新手册；两个修复组各自连续修复。来源题完整成功后才能发起成熟版本比较；学习支出过组额度一半时执行中途检查。
3. 四组在 30 道最终留出题上评估冻结版本。
4. 固定候选重测：每组最后一次成熟版本比较的两个版本，分别做全局重测与按影响范围重测，结果写入 `retest.json`。
5. 等美元重复执行／修订组：每道留出题的额度为 GNOSIS 全部费用（学习、验证、中途检查、留出）摊到每题。
6. 汇总写入 `summary.json`：各组通过率、费用明细、有效进化次数、验证执行与复用数、终止原因、盈亏平衡曲线。

| 实验组 | 承担的比较 | 学习与验证合计额度 |
|---|---|---|
| 固定 Agent | 修正执行条件后的真实起点 | 2.0 |
| 官方 ACE | 可执行经验相对文本记忆的价值 | 3.0 |
| 全局验证 | 修复器本身带来的收益 | 8.5 |
| 完整 GNOSIS | 限定影响范围后学习收益与验证费用的变化 | 8.5 |
| 等美元重复执行／修订 | 额外费用用于持续学习是否值得 | 按 GNOSIS 全部费用摊到每题 |

全局验证组与 GNOSIS 只差验证范围。两组的学习与验证共用一个组额度，GNOSIS 省下的验证费用直接变成更多修复次数，这正是要测量的量。

## 固定条件

- 基准：AutomationBench 提交 `4a8e106`，`--toolset api`，最多 50 步，严格断言评分。
- 模型：DeepSeek 官方 API `https://api.deepseek.com`，`deepseek-flash`（DeepSeek-V4.1-Flash），思考模式开启（`extra_body.thinking.type=enabled`），`reasoning_effort=high`，温度 1，单次输出上限 393216（官方最大值，上下文 1M）。工具调用轮次之间完整回传 `reasoning_content`。
- 终止状态分为截断、正常完成、工具执行失败三类。
- 任务执行最多 8 路并行，三个学习组共用名额；组内修复步骤按顺序。
- 服务断流最多重试 10 次，指数退避 2→60 秒；AI 格式错误重试 1 次。
- 修复器输入使用精简证据：完整作答轨迹、断言结果、扩展位置序列与干预记录；逐步重复提示的 `wire_trace` 只留在磁盘。

## 费用

费用不设上限：总额、时间窗口、单题都不拦截，账本 `state/research_ledger.sqlite` 只记账。唯一保留的额度是上表的组学习额度（USD 参照价），它是对照条件，用完即结束该组学习并冻结发布版本。

| deepseek-flash 每百万 token | 闲时 | 高峰（北京时间工作日 9–12、14–18 点） |
|---|---|---|
| 输入（缓存命中） | 0.02 元 | 0.04 元 |
| 输入（缓存未命中） | 1 元 | 2 元 |
| 输出（含思维链） | 4 元 | 8 元 |

账本 `cost` 按闲时价折成美元（7.1 元/美元），组额度与等预算比较只用它；`billed` 按实际时段报告扣费。整套实验按先导 token 用量估算闲时约 250 元，安排在闲时运行。余额耗尽时 API 拒绝请求，进程退出、状态保留，充值后再次运行按断点续跑。

## 先导结论（已报废，只作改造依据）

先导版本 `fa98b99a…`（Command Code 网关，温度 0，输出上限 8192）的干净基线在 30 题上严格通过 15 题，3 轮准入提出 3 个候选，接受 0 个。

| 先导证据 | 改造 |
|---|---|
| 15 道失败题中 9 道以 `finish_reason=length` 结束 | 输出上限 8192 → 393216，基线门槛检查截断占比 |
| 来源题失败后仍跑独立检查，占先导费用 68.6% | 来源题完整成功后才进入验证 |
| 修复只覆盖编译错误，没有语义修复循环 | 连续自主修复，失败反馈返回同一修复过程 |
| 231 次组件调用只有 1 次实际新增提示 | 可执行组件 E：操作函数、批量调用、观测改写，记录干预后的实际动作 |
| `api_fetch` 错误被跳过，`api_search` 不进干预路径 | 所有工具观测统一经过 `after_call` |
| 3 个候选全部 `GLOBAL` | `GLOBAL` 与局部作用域互斥，作用域越广验证越贵 |

## 开源复用

| 上游 | 提交 | 用途 |
|---|---|---|
| [AutomationBench](https://github.com/zapier/AutomationBench)（MIT） | `4a8e106` | 环境、工具、断言、runner |
| [StarHarness](https://github.com/ServiceNow/StarHarness)（MIT） | `a56d8f6` | `BenchmarkAdapter` 契约、任务集校验、`decide()` 与发布审查；修复主循环由本项目实现 |
| [ACE](https://github.com/ace-agent/ace)（Apache-2.0） | `82709de` | 官方 `Reflector`、`Curator`，请求经本项目计费 |
| [SAGE](https://arxiv.org/abs/2609.36043) | — | 成熟版本配对统计 |
