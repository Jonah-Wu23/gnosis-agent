# GNOSIS Agent

GNOSIS 研究 AI Agent 如何把执行经验写入持久组件，以及自动评价如何影响后续学习。

GNOSIS studies how AI agents turn execution experience into persistent components and how automated evaluation shapes subsequent learning.

## 论文 / Paper

**When Evaluation Prevents Learning: Functional Accumulation in Self-Evolving Agents**

Zonghe Wu · University College Dublin & Chang'an University

论文准备提交至 arXiv cs.AI。更新日期：2026-10-17。

The manuscript is prepared for submission to arXiv cs.AI. Updated: 2026-10-17.

| 文件 / File | 用途 / Contents |
|---|---|
| [gnosis.pdf](docs/arxiv/gnosis.pdf) | 论文正文与附录 / Paper and appendix |
| [gnosis.tex](docs/arxiv/gnosis.tex) · [appendix.tex](docs/arxiv/appendix.tex) | 最终 LaTeX 源码 / Final LaTeX sources |
| [gnosis-arxiv.tar.gz](docs/arxiv/gnosis-arxiv.tar.gz) | arXiv 上传包 / Source bundle for arXiv |
| [references.bib](docs/arxiv/references.bib) | 可编辑参考文献；正文直接读取 `gnosis.bbl` / Editable bibliography; the manuscript reads `gnosis.bbl` directly |

研究覆盖 2,468 次执行。在一项 HR 任务中，Agent 遗漏了应有的延期记录，仍通过全部九条测试。在 sales 任务中，过时的测试促使 Agent 采用违反最新业务指令的公告格式。评价中的错误会影响 Agent 发现问题，也会进入其后续沿用的经验。

The study covers 2,468 executions. In an HR task, the agent omitted a required deferral record and still passed all nine tests. In a sales task, outdated tests encouraged the agent to adopt an announcement format that violated the latest business instruction. Evaluator errors affect which problems an agent recognizes and can become part of the experience it reuses.

执行记录还显示，继承组件累计调用 27,500 次，实际效果为零。整理后的反馈没有提高新任务上的成功率。将前次轨迹与评价反馈用于同一道任务的下一次尝试，满足业务要求的执行次数由 41/72 增至 49/72。

The execution records also show 27,500 invocations of an inherited component with zero effects. Organized feedback did not improve success on new tasks. Giving the agent its previous trajectory and evaluator feedback for another attempt at the same task raised confirmed business success from 41/72 to 49/72.

## 目录 / Repository layout

| 位置 / Path | 内容 / Contents |
|---|---|
| `docs/arxiv/` | 最终投稿文件 / Final submission files |
| `docs/ecai2027/` | 排版工作文件与可编辑图片 / Working manuscript files and editable figures |
| `engine/` | Agent 执行、持久修复与论文分析 / Agent execution, persistent repair, and paper analysis |
| `host/` | Cordis 组件宿主与 QuickJS 隔离运行库 / Cordis component host and QuickJS isolation runtime |
| `config/research_selection.json` | 固定任务划分与实验条件 / Fixed task splits and experimental conditions |
| `candidates/` | 基础组件 / Base components |

本地 `experiments/` 保存轨迹与分析产物，`state/` 保存费用账本和运行状态。离线重算需要这些原始材料。仓库收录论文与分析代码；运行数据保存在本地，并由 `.gitignore` 排除。下载的参考论文和编译缓存也保存在本地。

Local `experiments/` contains trajectories and analysis outputs; `state/` contains billing ledgers and execution state. Offline recalculation requires these original materials. The repository contains the paper and analysis code. Runtime data, downloaded reference papers, and build caches stay local through `.gitignore`.

## 编译论文 / Build the paper

在 `docs/arxiv/` 中使用 pdfLaTeX 连续编译两次，输出为 `.build/gnosis.pdf`。上传包包含正文读取的参考文献与全部图片，可独立编译。

Run pdfLaTeX twice from `docs/arxiv/`. The output is `.build/gnosis.pdf`. The upload bundle includes the bibliography and every figure needed to compile independently.

```bash
mkdir -p .build
pdflatex -interaction=nonstopmode -halt-on-error -output-directory=.build gnosis.tex
pdflatex -interaction=nonstopmode -halt-on-error -output-directory=.build gnosis.tex
```

## 重算论文分析 / Recalculate the analysis

在已有完整实验材料的 WSL 环境中，从仓库根目录执行：

From the repository root, in the WSL environment containing the complete experimental materials:

```bash
export PYTHONPATH=scratch/AutomationBench:.
GNOSIS_DISABLE_MODEL_CALLS=1 scratch/AutomationBench/.venv/bin/python -m engine.negative_result_offline --all
```

命令先检查保存的官方重放，再重算业务结果与统计表，输出到 `experiments/negative_result_offline/`，模型调用数为零。

The command checks the saved official replay, recalculates business outcomes and statistical tables, and writes to `experiments/negative_result_offline/` with zero model calls.

| 模块 / Module | 职责 / Purpose |
|---|---|
| `negative_result_offline.py` | 分析总入口 / Analysis entry point |
| `negative_result_replay.py` | 官方环境动作重放 / Action replay in the official environment |
| `negative_result_business.py` · `negative_result_evaluation.py` | 业务要求与评价错误分析 / Business obligations and evaluator defects |
| `negative_result_history.py` · `negative_result_requirements.py` | 编辑谱系与要求完成路径 / Edit lineage and obligation completion paths |
| `negative_result_components.py` | 组件调用与实际效果 / Component invocations and effects |
| `negative_result_transfer.py` · `negative_result_completion.py` | 迁移分析与统计整合 / Transfer analysis and statistical integration |
| `negative_result_figures.py` | 分析图表 / Analysis figures |

表中模块均位于 `engine/`。研究过程与证据入口见 [2026-10-16 证据记录](docs/GNOSIS-负结果论文证据稿-2026-10-16.md)。

All listed modules are in `engine/`. Research notes and evidence locations are recorded in the [2026-10-16 evidence notes](docs/GNOSIS-负结果论文证据稿-2026-10-16.md) in Chinese.

## Agent 实验 / Agent experiments

系统通过可执行组件保存经验，按组件首次影响执行的位置确定重测范围。实验使用 AutomationBench 的 API 工具集，每题最多 50 步，沿用基准原有的测试。模型使用 DeepSeek 官方 API 的 `deepseek-flash`，开启思考模式，`reasoning_effort=high`，温度为 1。

The system stores experience in executable components and determines retest scope from the first point at which a component affects execution. Experiments use AutomationBench's API toolset, a maximum of 50 steps per task, and the benchmark's original tests. The model is `deepseek-flash` through the official DeepSeek API, with thinking enabled, `reasoning_effort=high`, and temperature 1.

上游固定版本由 `engine/research_upstream.py` 校验。Python 补充依赖见 `engine/automation_requirements.txt`；AutomationBench 自身依赖由上游 `uv.lock` 固定。真实模型调用读取本地私有配置与凭据。

`engine/research_upstream.py` checks the pinned upstream revisions. Additional Python dependencies are listed in `engine/automation_requirements.txt`; AutomationBench's own dependencies are pinned by its upstream `uv.lock`. Live model calls read local private configuration and credentials.

| 上游 / Upstream | 提交 / Revision | 用途 / Purpose |
|---|---|---|
| [AutomationBench](https://github.com/zapier/AutomationBench) | `4a8e106` | 任务环境与评分器 / Task environments and evaluator |
| [StarHarness](https://github.com/ServiceNow/StarHarness) | `a56d8f6` | Adapter 契约与发布审查 / Adapter contract and promotion review |
| [ACE](https://github.com/ace-agent/ace) | `82709de` | 官方 Reflector 与 Curator / Official Reflector and Curator |

在配置完成的 WSL 环境中运行：

Run in the configured WSL environment:

```bash
export PYTHONPATH=scratch/AutomationBench:.
PY=scratch/AutomationBench/.venv/bin/python
```

| 命令 / Command | 用途 / Purpose | 模型调用 / Model calls |
|---|---|---|
| `$PY -m engine.run_research prepare` | 核对固定任务划分 / Check fixed task splits | 否 / No |
| `GNOSIS_DISABLE_MODEL_CALLS=1 $PY -m engine.run_research offline` | 检查执行机制 / Check execution mechanisms | 否 / No |
| `$PY -m engine.run_research baseline` | 基线执行 / Baseline execution | 是 / Yes |
| `$PY -m engine.run_research run` | 按断点运行原始实验 / Run the original experiment with checkpoint resume | 是 / Yes |
| `$PY -m engine.evo_path extract` | 提取历史演化谱系 / Extract historical evolution lineage | 否 / No |
| `$PY -m engine.evo_path_analysis analyze` | 分析历史演化谱系 / Analyze historical evolution lineage | 否 / No |

宿主检查在 `host/` 中执行 `npm run typecheck` 和 `npm run selftest`。研究设计见 [科研计划](docs/GNOSIS-科研计划.md)，产品范围见 [产品 PRD](docs/GNOSIS-产品PRD.md)。这些研究过程文档保留中文原文。

Run `npm run typecheck` and `npm run selftest` in `host/` to check the component host. The [research plan](docs/GNOSIS-科研计划.md) and [product PRD](docs/GNOSIS-产品PRD.md) describe the study design and product scope. These research process documents retain their Chinese originals.

## 作者与许可 / Author and license

Zonghe Wu（吴宗河）· University College Dublin & Chang'an University

[zonghe.wu@ucdconnect.ie](mailto:zonghe.wu@ucdconnect.ie) · [个人主页 / Website](https://jonah-wu23.github.io/)

本项目采用 [MIT License](LICENSE)。上游组件与论文模板保留各自许可。

This project uses the [MIT License](LICENSE). Upstream components and manuscript templates retain their respective licenses.
