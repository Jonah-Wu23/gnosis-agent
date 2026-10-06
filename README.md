# GNOSIS Agent

当前状态：前期准备完毕，等待用户明确开始实验。`experiments/pilot/` 为空，当前知识库没有已装载候选，AppWorld 输出目录也已清空。

## 当前目录

| 位置 | 用途 |
|---|---|
| `engine/` | 执行、提议与配对门禁程序 |
| `host/` | Cordis 插件宿主与 QuickJS 隔离运行库 |
| `candidates/base_v2/` | 冻结的 AppWorld 基线实现 |
| `config/runtime.json` | 当前运行配置，沿用已验证的服务与费率 |
| `config/pilot_selection.json` | 固定的 30 道 AutomationBench 先导任务与授权服务 |
| `config/appworld_selection.json` | 固定的 40 道 AppWorld test-challenge 任务 |
| `state/` | 费用控制与准备状态，受测 AI 无权访问 |
| `experiments/pilot/` | 下一次实验的输出，目前为空 |
| `archive/2026-10-06/` | 所有历史实验及中断材料 |
| `scratch/AutomationBench/`、`scratch/ace/` | 固定版本的原装环境与 ACE 提示词 |

[历史实验索引](archive/2026-10-06/INDEX.md)列出了已做实验及中断状态，并链接到完整原始结果。

## 已固定的实验规则

| 项目 | 规则 |
|---|---|
| API 断流 | 最多重试 10 次，指数退避 2→60 秒并遵守 Retry-After；失败尝试按未计费释放预留；单题重试用完即记该题失败，跑完整批后补跑，不中止整批 |
| AI 输出格式错误 | 提议器保留失败、最多 1 次格式重试、每次单独保存与计费；执行器重试 1 次仍出错，该题记失败并继续下一题 |
| AI 代码修复 | 最多 3 次；组件类型由受测 AI 自选，提示词里写明 lmRequest 单次约 $0.0059、单题上限 $0.15 |
| 连续学习 | 来源题每轮轮换（干净基线的失败题按固定顺序取），每轮新执行结果进入下一轮反思；正式实验每道失败的流任务触发一次提议 |
| 晋升 | 两段式。阶段一准入：源任务由败转胜，且影响面内没有确认退化（输掉的配对重跑一次仍输才算确认）；影响面为 0 时凭源任务翻转准入。阶段末：原版 SAGE 二元配对门禁（α=0.05、惩罚系数 1、退化率上限 0）对“每题只留一个、取最后一次运行”的配对整体检验；只有确认退化才撤回在那道题上被注入过的单元，仅仅不显著则保留单元 |
| 预算 | 先导上限 $5，研究总预算 $45，5 小时窗口上限 $12，单题上限 $0.15，每次请求先预留费用；先导轮数由余额决定，每轮开跑前检查是否够 19 题的最坏费用 |
| 权限 | 仅提供当前任务授权工具、允许学习的记录及组件自身状态；QuickJS 与操作系统隔离同时执行；越界请求被拒绝并把拒绝信息返回受测 AI，其作答继续到测试结束 |
| 单元取代 | 新单元用 `replaces` 声明取代哪些旧单元，准入后旧单元自动退役；收窄与合并都走这一个入口 |
| evolve 续跑 | 按 `evolution_rounds.jsonl` 从断点续跑，不重复提议 |

知识内容、使用范围、预期效果与组件类型都由受测 AI 决定。程序保存、检查与加载候选，控制计费并执行门禁。

关键前提：历史费用继续计入既有先导预算。全部 VOID 归档永久退出学习、基线、配对和统计。当前版本 `fa98b99a95e18ac1162cd189dbe9773d7f2293208a1f529fa2a644c181aad946` 的干净 B1 已完成并核验，30 题严格通过 15 题。先导完成3轮准入检查，接受单元0个，以 `budget_stop` 正常结束，阶段末检验、汇总与统计已完成。完整证据见 [先导报告](experiments/pilot/pilot_final_report_2026-10-06.md)。先导结束，未开始正式实验。

先导结束时累计费用 $4.662190704，未结算预留 $0，先导剩余 $0.337809296。其中历史费用 $2.626853208，有效 B1 $0.893400138，本次先导费用 $1.141937358。历史 5 次请求（合计 $0.106533450）按预留全额计入支出结清，`calls.note` 保留判定；服务失败按既定规则释放预留。

### 预算口径

受测模型 deepseek/deepseek-v4.1-flash，思考强度 high。单价按 GOAT 套餐（[Command Code.md](docs/Command Code.md)，2026-10-04 抓取）：输入 $0.15/Mtoken、输出 $0.60/Mtoken（推理 token 计入输出）、缓存读 $0.003/Mtoken。套餐额度为 5 小时 $14、7 天 $35、自然月 $70，该模型的自然月额度 $60。

| 层 | 上限 | 现状（2026-10-06） |
|---|---|---|
| 研究总预算 | $45 | 累计已用 $4.662191 |
| 当前阶段（先导） | $5 | 剩余 $0.337809 |
| 5 小时窗口 | $12 | 正常退出时窗口内 $1.668868，余 $10.331132；实际值随窗口移动 |
| 单题 | $0.15 | AutomationBench 路径逐题累加，超过即停该题 |

三层在 `engine/scoped_units.py` 的 `Budget` 中同时生效：任一层“已计费或已预留”加本次请求上界越界即抛出 `BudgetExceeded` 并停止本次运行。窗口按滑动 18000 秒计算，账本逐次记录请求时间戳；本次改动之前的历史请求没有时间戳，记 0，落在任何窗口之外。$45 是硬顶，阶段上限只能在其之内调整，且必须留下授权理由（`budget_changes` 表）。窗口上限留出 $2 给研究者在同一窗口内的 CLI 操作。单题上限由 `MeteredClient` 累加 `gnosis_cost_usd` 判定，归档单题最大 $0.0803，上限取其约两倍。

实测 B1 单题平均费用 \(c=0.0297800046\)。先导首次启动要求剩余 \(\ge2(20c+0.06)\)，当时剩余 $1.479746654，高于要求 $1.311200184。主机资源中断后沿有效断点继续，每轮开跑前仍按程序的 \(20c+0.06\) 检查余额。预算保持原有上限。

经济性的真正约束是总耗时：单题平均 242 秒（中位 210 秒），串行时每小时约 $0.40；主实验约 1,500 次执行，串行约 100 小时，同时跑 3 题降到约 35 小时、每 5 小时约 $6。7 天窗口由网关执行，排期按每天约 $9.6 估算。

执行顺序：修改代码并冻结版本，执行并核验 B1，随后按授权运行先导及阶段末检验。当前先导已结束，正式实验需要另行授权。AppWorld 40 题基线移到第二环境阶段，不进先导阻塞项。当前账本状态用 `python -m engine.run_pilot budget` 打印。

## 不启动实验的准备检查

在 WSL 中执行：

```bash
cd '/mnt/e/AI/GNOSIS Agent'
export PYTHONPATH='/mnt/e/AI/GNOSIS Agent/scratch/AutomationBench:/mnt/e/AI/GNOSIS Agent'
'scratch/AutomationBench/.venv/bin/python' -m engine.run_pilot readiness
```

此命令检查配置、归档与依赖，读取费用账本，并将准备状态写入 `state/preparation.json`。它不调用模型，不执行题目。

AutomationBench 使用其独立 Python 3.13 环境；AppWorld 使用 `/home/jonahwu/.venvs/gnosis/` 的 Python 3.12 环境。

## 用户明确开始后的入口

先执行本版本的30题干净基线，再核验记录、费用及先导启动条件：

```bash
'scratch/AutomationBench/.venv/bin/python' -m engine.run_pilot readiness
'scratch/AutomationBench/.venv/bin/python' -m engine.run_pilot automation
```

核验通过后启动先导，受测 AI 从干净基线的失败题中轮换来源题：

```bash
'scratch/AutomationBench/.venv/bin/python' -m engine.run_pilot readiness
'scratch/AutomationBench/.venv/bin/python' -m engine.run_pilot evolve
```

原始提示与响应写入新的 `experiments/pilot/io/`，执行结果写入带版本号的 `runs/`，候选写入 `units/`。预算账本继续记录历史费用与新费用。新的 AppWorld 输出使用独立实验名称。

当前版本已完成真实模型运行、服务重试及连续先导迭代。完整结果保存在上述先导报告与原始记录中。

科研方法见 [科研计划](docs/GNOSIS-科研计划.md)，产品边界见 [产品 PRD](docs/GNOSIS-产品PRD.md)。
