# appworld_vendor：官方 simplified agent 接入说明

来源：github.com/StonyBrookNLP/appworld 仓库 main 分支 `experiments/code/simplified/`，2026-10-04 下载原文。
上游 blob SHA 与本地文件 SHA256 见 `archive/2026-10-06/experiments/stage2/config.json` 的 `vendor_sources`。

## 为什么不是 pip 包

官方 baseline agent 不在 `appworld` pip 包里，只随 GitHub 仓库的 `experiments/` 分发；
`appworld run` 命令要求仓库完整克隆并引入 `appworld_agents` 伴生包（其 `language_model.py` 34KB，
依赖多家 LLM SDK）。本目录只取基线所需的两个文件，把模型客户端换成本仓库的 `GnosisLM`。

## 逐行改动清单

`agent.py`（上游 `experiments/code/simplified/agent.py`）：

1. import 替换：`appworld_agents.code.common.logger/usage_tracker` → `engine.agent_runtime`
   的 `Logger/Usage/UsageTracker`；`appworld_agents.code.simplified.language_model.LanguageModel`
   → `engine.gnosis_lm.GnosisLM`；删除 `fill_model_server_url`。
2. import 适配 pip 版 0.1.3.post1：`appworld.common.collections.chunk_and_return`
   → `engine.agent_runtime.chunk_and_return`（从上游 main 逐字搬运，pip 版无此模块）；
   `appworld.common.io.write_file` → `appworld.common.utils.write_file`（pip 版无 io 模块）；
   `appworld.common.types.FromDict` → `appworld.common.utils.FromDict`（pip 版在 utils）。
3. `base_url` 处理：官方会经 `fill_model_server_url` 补全，本仓库端点固定从 `.env` 读，
   传入 base_url 直接抛错，防止静默走错端点。
4. `world.batch_execute([...])` → 逐个 `world.execute(...)`：pip 版无 `batch_execute`
   （main 新增）。ReAct agent 每步只有一个执行输入，逐个执行与批量执行在此等价。
5. `solve_tasks` 中的 `AppWorld.initializer(...)` 在 pip 版不存在（对应物为
   `AppWorld.init_defaults.update(...)`）。本仓库不走 `solve_tasks`，由
   `engine/run_experiment.py` 直接逐任务调 `solve_task` 并接管初始化、断点续跑与
   基础设施失败计数；`solve_tasks` 保留原文但不使用。

`react_code_agent.py`（上游同名文件）：

1. import 同步上述替换。
2. `prompt_file_path` 变为可选，新增 `prompt_text` 直传：候选目录装配后的工作流文本从这里进，
   官方文件路径方式保留可用。
3. 执行结果回投格式从硬编码改为 `observation_format` 参数（候选组件 T），
   默认值与官方硬编码逐字节一致：`"Output:\n```\n" + content + (补换行) + "```\n\n"`。

除以上外无任何行为改动；ReAct 循环、代码块抽取、历史裁剪逻辑保持上游原文。

## 版本漂移风险

上游 main 与 pip 0.1.3.post1 存在模块布局差异（`common.io`/`common.collections` 为新增）。
本目录代码钉死 pip 版运行；升级 appworld 包时必须重新核对上述 import。
