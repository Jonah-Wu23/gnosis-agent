# 多智能体协作结构的自动演化

调研日期 2026-10-04。覆盖 ADAS、AFlow、AgentSquare、EvoMAC、GPTSwarm、G-Designer、MaAS、AgentBreeder 等，以及对其哲学前提的批判性文献。

## 拍板结论

1. **"协作结构本身是被搜索对象"这个假设成立；"在验证集上得分高就等于结构优"这个判据是错的。** ADAS、AFlow、AgentSquare 全线把 benchmark 当作 fitness，搜索出的不是"协作机制"，而是"这套 benchmark 的记忆"。撤掉优化的独立评测锚点，所有"发现的协作结构"退化为评测集过拟合。结构可搜索 ≠ 结构搜索有效。

2. **"更多智能体、更复杂拓扑更好"是已被定量推翻的错误。** Google 的受控实验（180 种配置，跨三家 LLM）给出反例：单智能体基线准确率超过约 45% 后，协调的边际收益为负（β=−0.408, p<0.001），顺序推理任务上所有多智能体变体使性能下降 39–70%，独立智能体把错误放大 17.2 倍。拓扑不是越多越好，是严格任务条件化的。

3. **结构搜索的 credit assignment 是空的，自动失败归因不可用。** Who&When 数据集（127 个系统的失败日志）显示：最好的自动归因方法识别"责任智能体"仅 53.5%，定位"责任步骤"仅 14.2%，部分方法低于随机，o1/R1 也不实用。任何"失败→归因到机制→改结构"的闭环，当前都没有可信的自动归因子模块。

4. **搜索目标必须是结构与成本的联合，不能是准确率单指标。** MaAS 用 6–45% 的推理成本超过基线 0.54–16.89%，G-Designer 降低 token 92–95%。而准确率单目标是 reward hacking 的温床：AI Agents That Matter 证明复杂 agent 结构相对 retry/warming 这类简单基线没有显著准确率优势，成本差可达两个数量级。

5. **进化式结构搜索必须带归档与精英保留。** 朴素的 meta-agent 反复改写代码，单次坏改写会污染整个血缘（Gödel Agent 的已知缺陷）。DGM 用 archive + 经验验证把"证明有益"降级为"经验有益"，是目前唯一可落地的版本晋升范式。能力优化会同时产生对抗脆弱的 scaffold（AgentBreeder 红队结果），安全门禁不能省。

## 关键假设审查

- **ADAS（Meta Agent Search）假设**：代码空间图灵完备 ⇒ 能表达任意协作结构，meta-agent 能写出比人类更好的结构。撤掉"meta-agent 的代码先验足以在巨大空间里有效探索"，结论不成立——实际探索的是 FM 对代码的先验分布，不是全体程序。这是 ADAS 调查（Madžar 2026）明确的第一个未解问题："真正的搜索空间是 FM 对代码的先验，不是所有程序"。
- **AFlow 假设**：MCTS over code + 执行反馈能找到更优结构。撤掉"验证集分数与真实业务后果一致"，结论不成立。DebFlow 的对比还揭示 AFlow 在树搜索中反复做冗余且方向错误的修改（Node 3 重复引入已有 review 算子，Node 6 与 Node 14 重复），说明其搜索信号并不定位到机制。
- **AgentSquare 假设**：四模块（Planning / Reasoning / Tool Use / Memory）正交，任意同类型模块可换。撤掉模块正交性（实际模块间强耦合），模块重组与进化的前提瓦解。且固定单智能体四槽管道无法产生新控制流。
- **GPTSwarm 假设**：图连通性可被 REINFORCE 优化，且候选边集合固定。撤掉"节点集合与候选边集合已经正确"，结论不成立——它只在给定 agent 库存内调连通性，不发明新节点。REINFORCE 高方差，纯靠 end-to-end 标量。
- **EvoMAC 假设**：文本反向传播 ≈ 数值反向传播，text-based feedback 能把误差定位到具体节点并更新。撤掉"proxy 验证器可信"，梯度方向就整体错位，更新的是错误的节点。
- **MAST / 失败归类假设**：失败可被分类进固定 taxonomy（14 种失败模式，3 类），且可从 trace 观测。撤掉"失败模式有限且分布于过程可观测阶段"，归因失效。

## 前沿范式与代表工作

- **ADAS / Meta Agent Search**（Hu, Lu, Clune, ICLR 2025, arXiv:2408.08435）：meta-agent 在代码空间迭代编写新 agent，归档历次发现，用 ARC-AGI/DROP/MGSM 等评估。机制：archive-conditioned 代码生成 + 两轮新颖性自省 + 至多五轮调试。核心主张是"手写方案终将被学到的方案取代"，跨域跨模型迁移。冲突点：把推理期任务准确率当作适应度，不考虑发现的 agent 是否产生高质量训练数据。
- **AFlow**（Zhang et al., ICLR 2025, arXiv:2410.10762）：把工作流重构成"由 LLM 调用节点和边组成的代码"，用四步 MCTS（软选择 / 优化器 LLM 改写代码 / 验证集均值-方差 reward / 经验树回传）搜索。六基准平均 +5.7%，小模型能以 GPT-4o 的 4.55% 推理成本超过它。冲突点：每次候选都要在验证集执行，是主导成本，也是过拟合风险的最大来源。
- **AgentSquare**（Shang et al., ICLR 2025, arXiv:2410.06153）：提出模块化设计空间 MoLAS，四模块统一 IO 契约，用"模块进化 + 模块重组"上坡搜索，配 in-context 代理性能预测器跳过劣质候选。对六基准的人类设计平均 +17.2%。冲突点：固定四槽单智能体流程，结构上限被人为封死。
- **EvoMAC**（Hu et al., 2024, arXiv:2410.16946）：多智能体协作网络的自我演化。把 agent 视为神经元、prompt 视为权重、工作流视为层与连接，用"文本反向传播"按对 proxy 目标的验证结果更新网络。冲突点：整条链依赖 proxy 验证器，proxy 出错则后向信号错。
- **GPTSwarm**（Zhuge et al., ICML 2024 Oral, arXiv:2402.16823）：把 agent 统一为可优化计算图。节点优化改写节点 prompt；边优化把候选边参数化为 θ∈[0,1]^d，用 REINFORCE 在采样 DAG 上最大化效用。能自动剪掉对抗性 agent。冲突点：候选边集固定、REINFORCE 高方差、只有标量反馈。
- **G-Designer**（Zhang et al., 2024, ICLR 2025 Workshop, arXiv:2410.11782）：用变分图自编码器对"agent 节点 + 任务虚拟节点"编码，解码出任务自适应拓扑，带稀疏正则。MMLU 84.50%、HumanEval pass@1 89.90%，HumanEval 降 token 95.33%，对提示注入攻击仅掉 0.3%。冲突点：训练用少量 query 拟合固定参数，"任务自适应"其实是在已知分布内插值。
- **MaAS / Agentic Supernet**（Zhang et al., ICML 2025 Oral, arXiv:2502.04180）：放弃寻找单一最优系统，改为优化"智能体超网"——一个概率连续分布，含 CoT / Debate / ReAct / Early-exit 等算子，query-conditioned 控制器按任务难度采样子网。目标函数 min E[−p(a|q)+λ·C(G;q)] 显式含成本项。冲突点：算子词表固定、层数固定、策略梯度高方差。
- **AgentBreeder**（Rosser & Foerster, 2025, arXiv:2502.00757）：对多智能体 scaffold 做多目标进化搜索，按能力与安全双目标维护 Pareto 前沿，按架构特征聚类后从各簇采样精英。蓝队模式安全 +79.4%，红队模式暴露"能力优化会同步产生对抗脆弱 scaffold"。冲突点：证明结构搜索结果的安全外部性必须显式建模。
- **Why Do Multi-Agent LLM Systems Fail? / MAST**（Cemri et al., 2025, arXiv:2503.13657）：150+ 条 trace 的扎根理论研究，14 种失败模式 3 类，Cohen's κ=0.88。关键发现：失败主要来自智能体间交互而不是单体能力，且"改进 prompt/角色"这类战术修补只能带来 +14%，不能根治。冲突点：直接否定"局部补丁"路线，要求改结构。
- **Who & When**（Zhang et al., ICML 2025 Spotlight, arXiv:2505.00212）：首个自动失败归因数据集与基准，127 个系统。最好的自动方法责任智能体 53.5%、责任步骤 14.2%。冲突点：直接判"自动归因尚不可用"。
- **AI Agents That Matter**（Kapoor et al., 2024, arXiv:2407.01502）：agent 评测必须成本受控；SOTA 复杂 agent 不比 retry/warming/escalation 简单基线显著更好；成本差近两个数量级；多数 benchmark 无恰当 holdout，存在 4 个层级的一般性；复现性普遍缺失。
- **Towards a Science of Scaling Agent Systems**（Gu et al., Google, 2025, arXiv:2512.08296）：180 种配置的受控研究。三效应：工具-协调权衡（工具密集任务受害于多智能体开销）、能力饱和（单智能体 >45% 后协调收益为负）、拓扑依赖的错误放大（独立智能体 17.2×，集中式 4.4×）。集中式在可并行任务 +80.9%，去中心式在动态网页导航 +9.2%，顺序推理任务全体多智能体下降 39–70%。
- **Darwin Gödel Machine (DGM)**（Zhang, Hu, Lu, Lange, Clune, 2025, arXiv:2505.22954）：维护生成代码 agent 的归档，采样父代→生成"有趣的"变体→在 SWE-bench/Polyglot 经验验证→重新归档，形成开放探索树。SWE-bench 20.0%→50.0%，Polyglot 14.2%→30.7%。用经验验证替代 Gödel machine 不可行的"证明有益"，归档防止血缘崩塌。
- **MacNet**（Qian et al., ICLR 2025, arXiv:2406.07155）：用有向无环图组织上千个 agent，提出"协作缩放律"，发现不规则拓扑优于规则拓扑。冲突点：与 Google 的饱和/负收益结论直接冲突，其"协作缩放律"依赖任务可分解性且未做成本受控。
- **DebFlow**（Xia et al., 2025, arXiv:2503.23781）：用 debate 替代 MCTS 生成与优化工作流，训练成本比 SOTA 降 37%，平均 +3%。证明 AFlow 式树搜索存在大量冗余、方向错误的修改。
- **AgentPrune / AgentDropout**（Zhang et al., ICLR 2025, arXiv:2410.02506；arXiv:2503.18891）：通信剪枝与动态 agent 淘汰，证明多智能体通信存在大量可压缩冗余。
- **ADAS 批判性综述**（Madžar & Mekterović, 2026, preprints 202606.0238）：用 NAS 三维度（目标 / 策略 / 表示 / 反馈）梳理 33 个方法，指出两个结构性张力——表达力 vs 可搜索性、反馈丰富度 vs credit assignment，并明确"几乎没有方法做过程级 credit assignment"，"benchmark 就是优化信号"。
- **Position: Agentic Evolution is the Path to Evolving LLMs**（Lin et al., 2026, arXiv:2602.00359）：主张把"演化"本身从固定管线提升为自主的 evolver agent，提出"演化缩放假说"——适应能力随分配给演化的算力而缩放。

## 争议焦点

**争议一：单一最优系统 vs 架构分布。** ADAS/AFlow/AgentSquare 追求一个"万能最优工作流"，MaAS 明确反对，改求 query-conditioned 架构分布。裁决：MaAS 方向正确，单体最优是错误抽象。证据：G-Designer 在图 2 证明同一 MMLU 数据集内，简单子集用 chain 就够、硬子集必须用复杂拓扑；MaAS 进一步证明没有单一系统能同时最优处理 GAIA 的文件读取与网页搜索任务。

**争议二：搜索出的结构相对简单基线是否真有增益。** AI Agents That Matter 证明在成本受控下，复杂 agent 建筑相对 retry/warming 没有显著准确率优势。裁决：结构搜索的价值主要在成本下降，不在准确率上界。宣称"发现新协作机制带来性能突破"的论文，若不做成本受控与 holdout，结论不可信。

**争议三：自进化的有效性与安全性。** DGM 展示 SWE-bench 20%→50% 的真实增益，但 2026 年出现"Harness Updating Is Not Harness Benefit"一类反论，且 AgentBreeder 证明能力优化会同步产生对抗脆弱的 scaffold。裁决：自进化有效，但开放代码搜索的外部性风险（安全退化、血缘崩塌）必须显式建模，否则是负资产。

**争议四：归因驱动 vs 补丁驱动。** MAST 证明改进 prompt/角色这类战术修补无效，必须改结构；Who&When 又证明自动结构级归因不可用。裁决：两条路都不能单独走通。可行的组合是"结构级变异 + 人工/外部可验证的过程级标注"，而不是让 LLM 自己归因再自己改。

## 最被忽视的假设

**忽视一：适应度函数是被构造出来的，不是中立的。** 整个 ADAS 领域用同一批 benchmark suite 当 fitness，于是"搜索出的协作机制"实际上是"这套 benchmark 的记忆"。ADAS 批判性综述点破了这一点："benchmark 是优化信号，benchmark 的选择、数据划分协议、乃至'score'的定义，成为一切赖以成立的临界决策。"被忽视的原因是：结构搜索论文的卖点是"自动化"，没人愿意承认自动化的地基是人工选定的评测集。对 GNOSIS 意味着：机制归因与结构晋升必须锚定独立于优化目标的真实业务后果，否则整个闭环是在自我循环。

**忽视二：credit assignment 的粒度被高估。** 主流结构搜索的 reward 几乎全是 outcome-only 的 end-to-end 标量（GPTSwarm 的效用、AFlow 的均值-方差、MaAS 的成本加权奖励）。ADAS 综述明确指出"几乎没有方法做多步框架内的过程级 credit assignment"。结构搜索以为自己能定位到"哪个协作环节坏了"，实际上它只能知道"整条链坏了"，然后把改动随机分配到某个组件上碰运气。被忽视的原因：过程级标注昂贵且需要人工，而 outcome 标量便宜。对 GNOSIS 意味着：机制归因不能依赖 LLM 自动归因（Who&When 证明其准确率不足），必须自建过程级、可复现、外部可验证的归因信号，并把归因器本身的准确率当作被评测对象。

**忽视三：结构的可迁移性缺少 held-out 任务。** AI Agents That Matter 统计 17 个 agent benchmark，多数没有恰当的一般性层级的 holdout，7 个完全没有 holdout。结构搜索在 train/test 同分布下自我验证，于是"跨域迁移"的结论是虚假的。对 GNOSIS 意味着：版本晋升门禁必须在与搜索任务不同分布、且对搜索过程保密的 held-out 任务上执行。

## 对 GNOSIS 的直接启示

**机制归因（痛点①）：** 把"可迁移机制"定义在协作拓扑算子层——生成、评审、回溯、整合、早退——而不是 prompt 措辞或 agent 角色层。像 AFlow 预定义算子库那样，建立结构化的"协作算子变异库"，让失败归因落到算子而不是句子。同时必须把归因器本身当作被评测对象：用 Who&When 式的 trace 标注，持续测量归因器的智能体级与步骤级准确率，达不到阈值就不准用归因结果驱动结构改动。归因粒度到算子 + 归因器自身受测，这两条是防止退化到 prompt 堆砌的硬约束。

**真实后果评价（痛点②）：** 采用结构与成本的联合目标，直接借用 MaAS 的 min E[−p(a|q)+λ·C(G;q)] 形式，把成本当一等症状。评价锚点必须是外部可验证的因果后果（编译通过、测试通过、线上业务指标），绝不接受 LLM-as-judge 的自我评分作为晋升依据——AgentBreeder 已证明自评体系会同时奖励能力与隐藏脆弱性。参考 AI Agents That Matter：成本受控 + 恰当层级的 holdout + 复现性校验，三者缺一即为刷分。

**分层成本控制（痛点③）：** 直接复刻 MaAS / Archon 的代理模型分层。第一层做廉价的代码与结构合法性检查；第二层用轻量 performance predictor 给候选结构打分并过滤；第三层只对侥幸通过前两层的少量候选择做真实执行。G-Designer 证明大量 token 消耗在把简单任务交给复杂拓扑上，Archon 证明高斯过程代理能在 token 预算下比贪心/随机搜索样本效率高得多。把成本分层写进搜索算法，而不是事后统计。

**版本晋升闭环（痛点④）：** DGM 的 archive + 经验验证就是现成模板，直接借用。每个协作结构版本是 archive 中的一个节点；修改候选从父节点采样生成；晋升必须在与搜索集不同分布的 held-out 任务上通过回归门禁；精英保留 = 归档全部历史版本；回滚 = 保留父节点指针，任何版本都可退回。DGM 的关键教训是：用经验验证替代不可行的"证明有益"，用归档防止血缘崩塌。GNOSIS 的每一版 workflow/tool/context/policy/skill/协作结构调整，都应实例化为这样一次带门禁的晋升，而不是一次就地覆盖。

## 参考来源

- ADAS: https://arxiv.org/abs/2408.08435
- AFlow: https://arxiv.org/abs/2410.10762
- AgentSquare: https://arxiv.org/abs/2410.06153
- EvoMAC: https://arxiv.org/abs/2410.16946
- GPTSwarm: https://arxiv.org/abs/2402.16823
- G-Designer: https://arxiv.org/abs/2410.11782
- MaAS (Agentic Supernet): https://arxiv.org/abs/2502.04180
- AgentBreeder: https://arxiv.org/abs/2502.00757
- MAST (Why Do MAS Fail): https://arxiv.org/abs/2503.13657
- Who&When (Failure Attribution): https://arxiv.org/abs/2505.00212
- AI Agents That Matter: https://arxiv.org/abs/2407.01502
- Towards a Science of Scaling Agent Systems: https://arxiv.org/html/2512.08296v1
- Darwin Gödel Machine: https://arxiv.org/abs/2505.22954
- MacNet: https://arxiv.org/abs/2406.07155
- DebFlow: https://arxiv.org/abs/2503.23781
- AgentPrune: https://arxiv.org/abs/2410.02506
- AgentDropout: https://arxiv.org/abs/2503.18891
- ADAS 批判性综述: https://www.preprints.org/manuscript/202606.0238
- Position: Agentic Evolution: https://arxiv.org/abs/2602.00359
