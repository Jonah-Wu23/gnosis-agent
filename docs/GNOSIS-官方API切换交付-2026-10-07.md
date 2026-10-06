# GNOSIS 官方 API 切换交付

交付日期：2026-10-07。工作区：`E:\AI\GNOSIS Agent`。

**结论：可以执行 B1。** 模型服务已切换到 DeepSeek 官方 API，费用上限全部解除，此前全部实验结果已报废归档。当前程序版本 `73ddfc54…` 通过无模型调用的离线检查：单元检查 43 条、机制检查 97 条，宿主自测 42 条。真实 API 探测两次请求全部正常。付费实验没有启动。

## 1. 本次改动

### 1.1 模型服务

| 项 | 旧值 | 新值 |
|---|---|---|
| 服务 | Command Code GOAT 网关 | `https://api.deepseek.com` |
| 模型 | `deepseek/deepseek-v4.1-flash` | `deepseek-flash`（DeepSeek-V4.1-Flash） |
| 思考模式 | `reasoning_effort=high` | `extra_body.thinking.type=enabled`，`reasoning_effort=high` |
| 温度 | 0 | 1 |
| 种子 | 100 + 重复序号 | 不传；重复执行是独立采样。任务划分种子仍为 100 |
| 单次输出上限 | 32768 | 393216（官方最大值） |

`.env` 的三个键名不变（`MODER_PORVIDER`、`MODEL_API`、`MODER_NAME`），代码只在进程内读取。执行路径经 verifiers 发出请求，请求体同时带 `max_completion_tokens` 和 `extra_body.max_tokens`；修复器与 ACE 经 `llm_client.chat` 发出请求，默认开启思考模式。

真实 API 探测（脚本已删除，没有写入研究账本）结果如下：

| 请求 | 结果 |
|---|---|
| 第 1 轮：带工具定义，要求调用 `get_date` | `finish_reason=tool_calls`，返回 `reasoning_content`，1 次工具调用，usage 281/36 |
| 第 2 轮：回传 `reasoning_content` 与工具结果 | `finish_reason=stop`，正文 "Today's date is October 7, 2026"，usage 335/14 |

结论：思考开关生效；两种 `max_tokens` 写法同时出现不会报 400；工具调用轮次之间完整回传 `reasoning_content` 的格式被接受。verifiers 的 `from_chat_message` 本来就把 `reasoning_content` 写回 assistant 消息，不用改。

### 1.2 费用

| 项 | 处理 |
|---|---|
| 研究总额 $45、5 小时窗口 $12、单题 $0.15 | 全部解除 |
| 用途分配（学习 14 / 选择 8 / 评估 14 / 余量 4）与最终评估预留 | 删除 |
| 组学习额度 2 / 3 / 8.5 / 8.5 | 保留 |
| 等预算组单题额度 | 保留，按 GNOSIS 全部费用摊到每题 |
| 账本 | 新建 `state/research_ledger.sqlite`，`limit=None` |

组学习额度保留的原因：它属于对照设计，不承担费用保护。两个修复组在同样的学习与验证花费下比较，GNOSIS 省下的验证费用直接变成更多修复次数，这正是论文要测的量。撤掉组额度，两组都可以无限学习，比较就没有意义了。额度按已结算费用判定。输出上限 393216 的最坏预留约 $0.22，如果按预留拦截，额度内的请求会被提前挡下。

计价采用官方价目。账本的 `cost` 按闲时价折算成美元记账（7.1 元/美元：输入 1 元、缓存命中 0.02 元、输出 4 元，单位为每百万 token）。组额度与等预算比较只用 `cost`，这样高峰时段不会让某一组吃亏。`billed` 按北京时间时段报告实际扣费：工作日 9–12 点、14–18 点乘 2，法定节假日按工作日近似处理。

### 1.3 8 路并行

`ResearchEvaluator.execute` 用一个 8 路信号量控制并发，命中断点的执行不占名额。

| 阶段 | 并行方式 |
|---|---|
| B1 | 30 题并行 |
| 学习 | ACE、全局验证、GNOSIS 三组同时运行，共用 8 个名额；修复器与 ACE 的同步模型请求放进线程，不会卡住其他组 |
| 选择集 | 对照侧 12 次执行并行；候选侧受影响的执行并行，结果按原顺序落位 |
| 中途检查 | 6 题 × 早期/当前版本，共 12 次执行并行 |
| 最终留出 | 每组 30 题并行，四组依次执行 |
| 固定候选重测 | 受影响的执行并行 |
| 等预算组 | 30 题并行，每题内部的重复执行按顺序（下一次要看到上一次的过程） |

ACE 逐题更新手册，各组内部的修复步骤也都依赖上一步的反馈，所以组内保持顺序。离线检查覆盖重放路径，这条并行执行路径要到 B1 才第一次真正跑起来。

### 1.4 报废归档

全部移入 `archive/2026-10-07-void/`，说明见其中的 `VOID.md`：

| 原位置 | 内容 |
|---|---|
| `experiments/pilot/` | 先导 B1、3 轮准入、全部原始请求与响应（700MB） |
| `state/pilot_budget.sqlite` 等 | 网关账本、准备状态、旧版本准入证明 |
| `.offline_checks/` | 旧版本离线检查工作区 |
| `config/research_selection.json` | 旧组额度下的任务划分 |

这些记录永久退出学习、基线、配对与统计。`engine/research_offline.py` 仍从归档读取先导工具轨迹与响应，用作离线机制检查的真实样本（截断判定、工具重放、边界预测），这个用途不调用模型，也不产生结论。任务划分已重新生成，开发、选择、留出、中途检查四个集合的题目与旧划分完全一致。

### 1.5 B1 入口

新增 `run_research baseline`：只执行 B1 和基线门槛，打印门槛结果与账本状态。`run` 开始时按断点复用 B1，不会重复计费。

## 2. 修改文件

| 文件 | 改动 |
|---|---|
| `engine/scoped_units.py` | 策略解除上限，输出上限改为 393216，新增 `parallel_tasks=8`；账本支持 `limit=None`，新增 `billed` 列与高峰判定 |
| `engine/evolution.py` | `ResearchBudget` 重写：只保留组学习额度 |
| `engine/run_research.py` | 新账本；删除窗口等待与额度来源；三个学习组并行；各评估阶段并行；新增 `baseline` 入口；等预算组额度改为 GNOSIS 全部费用摊到每题 |
| `engine/research_evaluator.py` | 8 路信号量；采样参数改为官方思考模式 |
| `engine/research_retest.py` | 受影响的执行并行 |
| `engine/research_model.py` | 温度改为 1，不再传种子 |
| `engine/llm_client.py` | 默认开启思考模式 |
| `engine/automation_adapter.py` | 单题上限可关闭；单题额度按已结算费用判定 |
| `engine/research_offline.py` | 读取归档样本，按新账本规则检查 |
| `engine/research_plan.py` | 删除旧用途分配字段 |
| `config/runtime.json` | 计价改为官方价目（已忽略，不进 Git） |
| `README.md`、`AGENTS.md`、`docs/GNOSIS-科研计划.md`、`docs/GNOSIS-产品PRD.md` | 同步模型、费用、并行与报废规则 |

## 3. 费用预估

闲时价折算成美元后与旧网关单价几乎相同（输入 $0.141 对 $0.15，输出 $0.563 对 $0.60）。按先导的 token 用量，整套实验闲时约 250 元。

有一个变量要靠 B1 实测：先导有 9 道题在 8192 处被截断，输出上限改为 393216 后，这些题会继续推理到完成，单题输出量和费用都会上升。B1 跑完后看两件事：

1. 实际单题费用。如果明显高于先导的 $0.03，按同样比例放大组学习额度（`engine/research_plan.py` 的 `ARM_LEARNING_LIMITS`），保证每组仍能做足修复次数。这一步需要你拍板。
2. `baseline_gate.json` 中截断失败的占比。按新上限，这个值应当接近 0。

## 4. 下一步

```bash
wsl -d Ubuntu --cd '/mnt/e/AI/GNOSIS Agent' -- env PYTHONPATH=scratch/AutomationBench:. scratch/AutomationBench/.venv/bin/python -m engine.run_research baseline
```

尽量在闲时运行，即北京时间工作日 18 点以后、12–14 点、周末和节假日。B1 通过后执行 `run_research run`。
