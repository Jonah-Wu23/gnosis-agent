# 阶段一交付：环境搭建（供下一阶段基线测量使用）

交付日期 2026-10-04。对应 git 提交 `272db0b`。本文档是下一阶段（科研计划 §12.2：官方训练任务基线测量）的开工依据，列出可用资产、已验证链路、硬性约束与开工前必须冻结的事项。

## 1. 拍板：reflection_lm 不装 litellm，走自有 GnosisLM

结论：**不安装 litellm**。下一阶段实现一个 `GnosisLM` 类放进 `engine/`，直接传给 `gepa.optimize(reflection_lm=...)`。

证据链（全部为实测，非推测）：

1. GEPA 的 `reflection_lm` 形参类型为 `LanguageModel | str | None`（gepa/api.py:54）。`LanguageModel` 是 Protocol：`__call__(prompt: str | list[dict]) -> str`（gepa/proposer/reflective_mutation/base.py:27）。
2. litellm 只在传字符串模型名时才被 import（gepa/lm.py 的 `LM.__call__` 内部延迟导入）。传 callable 的执行路径不碰 litellm。
3. `pip install litellm --dry-run` 实测：litellm 1.104.0 会把 pydantic 升到 2.13.5、openai 降到 2.54.0。AppWorld 的 SQLModel 0.0.10 要求 pydantic 1.x，升级即破坏主任务环境。
4. 裸 callable 会被 GEPA 包上 `TrackingLM`：按 4 字符=1 token 估算，`total_cost` 恒为 0。但 gepa/api.py:245 的判定是 `TrackingLM(reflection_lm) if not hasattr(reflection_lm, "total_cost")`——**callable 自带 `total_cost` 属性即跳过估算包装**。

`GnosisLM` 的接口契约（下一阶段实现时遵守）：

- 内部调用 `engine/llm_client.py` 的 `chat()`，思考强度永远 high，调用方无法降档。
- 实现 `__call__(prompt: str | list[dict]) -> str`，返回 `choices[0].message.content`。
- 暴露三个 property：`total_cost`、`total_tokens_in`、`total_tokens_out`，数值全部来自真实 API 的 usage 字段，不做字符估算。
- usage 中 `completion_tokens_details.reasoning_tokens` 单独累计；思考 token 计入费用（科研计划 §8.2 要求思考消耗按官方计费纳入）。
- `total_cost` 按调用时 Command Code 公示费率折算；费率表在正式实验开工时抓取冻结（见第 5 节）。
- 同一个 `GnosisLM` 实例同时服务任务执行与反思（科研计划 §7.1：任务执行、候选提议、归因、联合修复统一同一模型配置）。

## 2. 环境清单

| 侧 | 位置 | 用途 | 状态 |
|---|---|---|---|
| WSL2 venv | `~/.venvs/gnosis`（Ubuntu 24.04，Python 3.12.3） | 正式实验唯一运行环境：AppWorld、GEPA、引擎 | 已验证 |
| Windows venv | 仓库根 `.venv/`（Python 3.11.4） | 仅用于快速冒烟，不承担实验 | 已验证 |
| TS 宿主 | `host/`（Node v24.14.0） | Cordis 组件宿主 | 已验证 |

精确版本：

| 包 | 版本 | 所在侧 |
|---|---|---|
| appworld | 0.1.3.post1 | WSL2 |
| gepa | 0.1.4 | WSL2 |
| openai | 3.24.0 | 两侧一致 |
| python-dotenv | 1.2.4 | 两侧一致 |
| cordis | 4.0.0-rc.10 | host/ |
| typescript | 7.0.2 | host/ |
| tsx | 4.23.15 | host/ |

固定依据写在 `engine/requirements.txt`、`host/package.json`，细节见 `docs/setup/appworld-gepa.md` 与 `docs/setup/cordis.md`；模型接入的历史记录见 [llm-smoke.md](../../archive/2026-10-06/documents/setup/llm-smoke.md)。

## 3. 已验证链路（附证据强度）

| 链路 | 验证方式 | 结果 |
|---|---|---|
| 模型调用 | 真实 API 调用，Windows 与 WSL2 各一次以上 | 响应 model 字段为 `deepseek/deepseek-v4.1-flash`；`reasoning_effort="high"` 被网关校验并透传，reasoning_tokens 实测 108–879（off 时为 0），思考内容在 `choices[0].message.reasoning_content` |
| 官方评分 | 零模型 token：train 任务 `82e2fac_1` 初始化 → 执行 golden 解答 → `world.evaluate()` | success=True；`appworld verify tests` 1553 项、`verify tasks` 147/147 全过 |
| GEPA 扩展面 | 源码核查 | `evaluate`、`make_reflective_dataset` 接口存在；`reflection_lm` 接受 callable；`use_merge`、`max_merge_invocations` 等合并参数在 `optimize` 签名中 |
| Cordis 生命周期 | `npm run boot` 六条断言 | 组件装载、服务调用、卸载后注册表清理双向断言全过；`npm ci` 重装可复现 |

## 4. 硬性约束（违反即破坏环境，无例外）

1. **pydantic 不得升到 2.x**。AppWorld 依赖链钉死 1.10.26。装任何新包先 dry-run 看 pydantic 是否被翻动。
2. **模型调用必须走 openai SDK**。裸 HTTP 直连被 Cloudflare 按 UA 拦 403（error code 1010），现象像鉴权失败，实际不是。
3. **思考强度不可降档**。`llm_client` 已把默认值锁为 high，任何调用方不得传更低档位。
4. **`.env` 值不进代码、不进文档、不进 git**。只用键名 MODER_PORVIDER / MODEL_API / MODER_NAME。
5. **AppWorld 验证的判据是函数返回值**，不是 CLI 输出：`appworld verify tasks` 结尾 `os._exit(0)` 会丢缓冲输出且退出码恒为 0，须直接调 `verify_tasks()`。
6. **cordis 4.0.0-rc.10 类型声明在 NodeNext 下不可用**，`host/types/` 的类型补丁不许删，上游修复后再撤。
7. WSL2 无 sudo 权限，venv 引导走的是 `--without-pip` + get-pip.py；别再试图 apt 装 python3.12-venv。

## 5. 下一阶段开工前必须冻结的事项

1. **费率表**：抓 Command Code 对 `deepseek/deepseek-v4.1-flash` 的公示每百万 token 价格（含缓存命中价），写进实验配置。`GnosisLM.total_cost` 以此折算。
2. **采样参数**：温度、max_tokens 等固定一组 API 参数，全实验一致（科研计划 §7.1）。
3. **任务清单版本**：AppWorld 固定版本 0.1.3.post1 的 train/dev/test-normal/test-challenge 实际任务清单，按 scenario 分组，同一 scenario 不分跨分区（科研计划 §7.1）。
4. **基线 Agent 形态**：确认 AppWorld 原始 Agent 在自有客户端下的接入点，使组件改动（工作流、工具包装、上下文组织、技能四类）确实出现在模型输入或工具调用中——这是科研计划 §9 失败表第二行的前置检查。

## 6. 下一阶段第一个动作

在官方 train 任务上测基线：跑原始 Agent，记录逐项成功率与全口径单次费用。这个费用实测值直接决定 PRD §6.4 的先导硬费用门槛判定（>$0.15 触发缩减规则），优先拿到。
