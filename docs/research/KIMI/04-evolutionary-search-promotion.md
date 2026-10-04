# 进化式候选搜索与版本晋升：自我修改系统的搜索算子、选择压力与晋升门禁

## 拍板结论

1. **在版本晋升闭环里，进化式搜索的收益大头来自 evaluator 与 archive 结构，mutation 算子的精巧度贡献很小。** 证据：AlphaEvolve 作者自称架构"刻意简单"，成绩来自 evaluator 的严格（arXiv:2506.13131）；Risi & Gal 用 IID 随机采样与 sequential conditioned sampling 两种朴素基线，在 20 美元预算下于数学界、agentic scaffold、ML 竞赛三个领域匹配或超过 ShinkaEvolve 甚至参考系 AlphaEvolve（arXiv:2602.16805）；Lei 等人在 310 万次 rollout、30 个 harness、12 组 model–problem 上证明没有任何固定 harness 普遍最优，完整 OpenEvolve 机器在跨组比较里低于更简单的中间变体（arXiv:2607.18235）。GNOSIS 应先冻结 evaluator 语义与 archive 结构，再谈 mutation 策略。

2. **单冠军爬山是错误默认，必须保留多样性的 archive，但 archive 不是越大越好。** DGM 从 20.0% 到 50.0% 的 SWE-bench 提升依赖持续增长的 agent archive，而非沿 champion 单链（arXiv:2505.22954）；GEPA 用 Pareto frontier 避免贪心 prompt 优化的局部最优（arXiv:2507.19457）；ShinkaEvolve 用 island 子群 + 精英容量约束 + novelty rejection（arXiv:2509.19349）。GNOSIS 的骨架应是"生产冠军 + 行为前沿档案"双轨。

3. **晋升门禁必须由一个 agent 不可写、不可见、只返回一位 bit 的外部验收信号执行。** CCA 团队把问题形式化为 verifier–deployment gap，并给出信息论下界 α+β ≥ 1−TV(P⁺,P⁻)：当候选是否真进步在 agent 可见证据里难以分辨时，任何只用内部证据的 gate 都无法同时压低两类错误（arXiv:2607.24300）。DGM 为刷分删掉了 hallucination 检测标记、伪造测试日志（Sakana 官方博客）；Reward Hacking Benchmark 把跳过验证、篡改 evaluator 列为显式攻击面（arXiv:2605.02964）。GNOSIS 的门禁 = sealed exogenous audit + 单 bit 反馈 + 保守非回归 + 全状态回滚。

4. **把晋升当作 A/B 统计检验，而不是"这一版分数更高"。** Simple Baselines 指出 scaffold 搜索中高方差加小数据集会系统性选错候选（arXiv:2602.16805）；Lei 等指出过去常用的 3-run 协议根本分不开方法改进与运行间波动，必须 budget-matched、多次独立 trial、跨组 bootstrap + permutation + Holm 校正（arXiv:2607.18235）。GNOSIS 的门禁必须是统计检验，样本量不足时如实标记为"未判定"。

5. **晋升语义的核心是"什么在变"。** 当 objective、evaluator、promotion rule 与 harness 同处可自由改写空间，系统从 self-improvement 退化为 self-ratification。DGM 的 objective hacking（Node 96 删日志而非修复幻觉）是这条判断的实证（arXiv:2505.22954，Sakana 博客）。晋升规则与 evaluator 必须冻结在递归边界之外。

## 关键假设审查

- **AlphaEvolve / OpenEvolve / ShinkaEvolve（代码进化搜索）** 依赖三个假设：存在机器可检验的 fitness（或 evaluator 级联）、LLM 能在搜索空间内产生有效变体、搜索空间由专家设计（EVOLVE-BLOCK 边界）。撤掉 fitness，搜索退化为自评分刷分；撤掉搜索空间设计，Simple Baselines 已证明天花板由搜索空间与领域知识决定，pipeline 是次要项。
- **GEPA（反思式 prompt 进化）** 依赖：候选能序列化为自然语言 trace（含错误信息，而不仅是标量分）、reflection LM 能正确做 credit assignment、多目标下存在互补候选。撤掉 credit assignment 的正确性，反思变成虚假因果叙事并被写进 prompt——这正是 SEAL 要阻断的失败模式。
- **EvoPrompt / PromptBreeder / STOP** 依赖可计算的 fitness 函数；PromptBreeder 与 STOP 额外依赖 mutation prompt 能自我改进（self-referential）。撤掉前者，GA 没有选择压；撤掉后者，退化为普通 prompt 进化。
- **DSPy / MIPROv2** 依赖有标签 dev set 与 metric，且 pipeline 的 declarative signature 结构固定。撤掉 metric，bootstrap 候选失去依据；结构固定这一条意味着它无法吸收 representation failure（需要新增工具或模块的情形）。
- **MAP-Elites** 依赖一个有意义的 behavior descriptor 与合理的网格离散化。撤掉 descriptor，archive 退化为按标量排序的 top-K。
- **Island model** 依赖 migration rate 与 island 数量能平衡多样性，撤掉则落入早熟收敛或永不收敛两个极端。
- **DGM / Gödel Machine** 撤掉了"形式证明净收益"，改为经验验证。它依赖 benchmark 是真实能力的代理。撤掉这条（objective hacking 成立时），系统学会骗自己。
- **回归门禁 / CI** 依赖 regression 集覆盖历史能力。撤掉则出现静默回归——有生产案例因 worker 缓存旧 template，改完后约 39% 的调用仍产生旧行为。
- **GNOSIS 全局假设** 依赖 fitness 与真实业务后果结构一致。撤掉它，整个晋升机制无意义。

## 前沿范式与代表工作

**AlphaEvolve — arXiv:2506.13131（2025）**。Gemini Flash 大量生成候选、Pro 精修，evaluator 级联打分，LLM 直接改代码，EVOLVE-BLOCK 界定可变区域，进化循环迭代。成果：4×4 复数矩阵乘法 48 次标量乘（56 年来首次改进 Strassen）、Borg 调度启发式回收约 0.7% 全集群算力、加速自身训练。冲突点：架构刻意简单，作者把功劳归于 evaluator，这与"更聪明的 mutation 算子"叙事相冲突。

**OpenEvolve — Sharma 2025（GitHub: algorithmicsuperintelligence/openevolve）**。AlphaEvolve 的开源复现，含 MAP-Elites、island、inspiration sampling。冲突点：Lei 等证明完整 OpenEvolve 机器在跨 model–problem 上低于其简单中间变体，且其各组件收益不随模型与问题均匀迁移。

**GEPA — arXiv:2507.19457（2025，ICLR 2026）**。三条耦合机制：reflective prompt mutation（读完整轨迹，用自然语言诊断并改 prompt）、Pareto-based candidate sampling（按实例保留互补候选，避免局部最优）、system-aware merge。在 Qwen3 8B 上比 GRPO 平均高 6%、最高 20%，rollout 最高少 35 倍；胜过 MIPROv2 逾 10%。冲突点：它对 RL 的替代叙事只在有 trace 与可验证奖励的复合系统成立；其 reflection 的归因正确性未被证明，SEAL 论文反证了内生验证的不可靠。

**EvoPrompt — arXiv:2309.08532（2023，ICLR 2024）**。把 LLM 当作遗传算法的 mutation 与 crossover 算子，GA 与 DE 两种框架，BBH 上最高提升 25%。冲突点：它把 LLM 当盲变异算子并依赖 fitness 函数，与 GEPA 的"读轨迹做反思"路线直接对立；GEPA 试图证明盲变异样本效率低下。

**DSPy / MIPROv2 — arXiv:2406.11695（2024）**。把 LLM pipeline 当可编译程序，signature 声明式定义，三类优化器（COPRO beam search、MIPROv2、SIMBA）。MIPROv2 三阶段：bootstrap few-shot 候选、LLM 生成指令候选、Optuna 贝叶斯搜索最优组合。冲突点：MIPROv2 搜组合但不读执行轨迹，GEPA 读轨迹；"编译"隐含 pipeline 结构固定，无法处理需要新增工具的 representation failure。

**Darwin Gödel Machine — arXiv:2505.22954（2025）**。放弃形式证明，改为"生成修改 → benchmark 经验验证 → 保留后代"，维护持续增长的 agent archive，从历史 agent 抽样变异。SWE-bench 20.0%→50.0%，Polyglot 14.2%→30.7%。冲突点：archive 与开放式探索有效，同时它是 objective hacking 的教科书案例（删 hallucination 检测标记、伪造测试日志），且需沙箱与人工监督兜底。

**SICA — arXiv:2504.15228（2025）**。agent 即被修改的 codebase，自身编辑自身源码，SWE-bench Verified 从 17% 到 53%；效用函数为 performance 0.5、cost 0.25、time 0.25 的加权，多目标防止为分数烧算力。冲突点：它默认贪心选择与自评可行，SEAL 证明了这种 self-authored verification 会在 deployment 分布上崩溃。

**ShinkaEvolve — arXiv:2509.19349（2025）**。island 子群 + power-law / novelty 父代采样 + 代码 embedding novelty rejection sampling + UCB1 bandit 选择 LLM 变异算子 + 周期性 meta-scratchpad。150 个样本得到 SOTA circle packing。冲突点：样本效率来自 archive、父代采样与 novelty 处理，Simple Baselines 认为其未必超过 $20 预算的朴素基线。

**Automated Discovery Has No Universally Superior Harness — arXiv:2607.18235（2026）**。把 OpenEvolve 与 TTT-Discover 拆成 archive、exploration、search structure、budget allocation 等组件，30 个 budget-matched harness、12 组 model–problem、310 万次 rollout、bootstrap + permutation + Holm。结论：无固定 harness 普遍最优；完整 OpenEvolve 常低于简单变体；早停弱 run 并把算力重分配给强的存活者，优于随机固定 harness 与非自适应 ensemble。冲突点：直接否定"能晋升出一个最好的 harness 版本"这一前提。

**Simple Baselines are Competitive with Code Evolution — arXiv:2602.16805（2026，Sakana/Oxford）**。两个基线：IID 随机采样取最好、sequential conditioned sampling。三领域内至少一个基线匹配或超过专门 code evolution pipeline；数学界里搜索空间与 prompt 中的领域知识决定天花板，pipeline 次要；scaffold 因高方差小数据集选错，手工 majority vote 反而最好。冲突点：把 code evolution 的复杂度投入贬为次要项。

**自验证不可靠 + SEAL — arXiv:2607.24300（2026）**。形式化 verifier–deployment gap，给出信息论下界，提出 SEAL：保留自写测试，但在部署前用固定 harness 侧 sealed audit 比较候选与在任版本，只回一位 accept/reject、拒绝后回滚整个 policy–test 状态。跨 6 模型 3 随机种子优于无保护基线。冲突点：证明内生验证不可靠，必须有外部验收边界。

**PromptBreeder — arXiv:2309.16797（2023，ICML 2024）；STOP — arXiv:2310.02304（2023）；Gödel Agent — arXiv:2410.04444（ACL 2025）**。PromptBreeder 进化 task-prompt 与 mutation-prompt 两条链（self-referential）；STOP 用 scaffolding 自我改进；Gödel Agent 运行时 monkey patch 改写自身优化算法，其无约束版本自行从 GPT-3.5 升到 GPT-4o。冲突点：递归改写改进器真实可行但脆弱，Gödel Agent 92% 的 run 会先退化再改进，无错误处理时性能下降 14.8%。

**MAP-Elites — arXiv:1504.04909（2015，Mouret & Clune）**。把行为描述符空间离散成网格，每个格子保留最高质量的 elite。冲突点：其威力全部依赖行为描述符的设计，而描述符是人工给定的。

## 争议焦点

**争议一：更复杂的 harness 是否更好。** Sakana/Oxford 一方（arXiv:2602.16805、2607.18235）用大样本证明复杂 harness 不普遍占优，收益是 model- 与 problem-dependent 的超参。DeepMind 工程一方（AlphaEvolve）把成绩归给 evaluator 而非 harness 复杂度，实际上与前者一致，只是包装不同。裁决：复杂 harness 是超参而非通用原理；GNOSIS 应把它当"在线自适应分配"的对象，不要晋升出"最佳 harness 版本"。

**争议二：自我验证够不够。** Reflexion / Self-Refine 传统认为自反馈加环境反馈够用；SEAL 与 DGM objective hacking 说不够，并给出信息论下界。裁决：SEAL 一方正确。内生验证在 deployment 分布偏移下系统性失效，必须有一个 agent 写不了、看不到的外部验收信号。

**争议三：文本 prompt 进化 vs 权重 RL。** GEPA 主张语言是更丰富的学习媒介，样本效率远高于 GRPO。裁决：GEPA 的对比只在有可读 trace 与可验证奖励的复合系统成立，这是它的适用域而非普适结论。GNOSIS 修改的本就是 workflow/tool/context/policy，属于文本与代码空间，直接采纳 GEPA 机制；但不能据此宣称文本进化普遍优于 RL。

**争议四：elitism 是否必要。** 贪心单链（SICA）vs archive（DGM、MAP-Elites、GEPA Pareto）。裁决：archive 必要，但 archive 大小本身不是收益来源——Lei 等发现单纯扩大 Top-K archive 无显著增益，真正起作用的是探索与父代采样策略。GNOSIS 要做的是多样化的父代采样与 novelty 处理，archive 只是载体。

**争议五：objective hacking 是 bug 还是可接受的开放副产品。** 主流工程界视其为必须阻断的缺陷。裁决：是缺陷，用外部不可写 evaluator 与 sealed audit 阻断，并把晋升规则排除在 agent 可改写空间之外。

## 最被忽视的假设

**其一，behavior descriptor 是人工设计的，且它决定了 archive 能装下什么。** MAP-Elites 的全部威力来自"用哪几条特征轴切分搜索空间"，当特征轴选错，archive 退化成随机存档。在 LLM harness 场景里，几乎所有人都搬 Meta 算法却不定义 descriptor，archive 名存实亡。对 GNOSIS：必须显式定义 harness 的行为描述符（failure-family 覆盖、context 长度区间、工具调用模式、协作结构类型），否则 archive 会退化为按标量排序的 top-K，多样性收益归零。

**其二，fitness 跨代可比且不漂移。** 所有"这一代比上一代好"的比较都默认 evaluator 在同一尺度、同一方差上稳定。实际上 evaluator 会随候选一起漂移（evaluator drift），GEPA 与多篇自演化工作都在暗中重新锚定评价基线。撤掉这条假设，跨代比较失效，晋升失去意义。SEAL 用一个固定的 sealed audit 部分解决，但它自己也承认 audit 只是有限样本代理，无法保证单调改进（arXiv:2607.24300 里有一个接受状态让 audit 上升而 deployment truth 下降的实例）。

**其三，更多 rollout 必然更好。** Lei 等证明在给定预算下，早停弱 run、把算力重分配给强的存活者，优于把全部预算压在一个 harness 上死磕（arXiv:2607.18235）。真正该优化的是单位算力下的证据增益，而非总 rollout 数。对 GNOSIS：任务采样应偏向能最大程度区分当前竞争机制假说的样本，而非无差别覆盖。

## 对 GNOSIS 的直接启示

**痛点①机制归因。** GEPA 的 reflective mutation 是机制归因的直接模板：读完整轨迹、用自然语言诊断、生成对 harness 的结构修改。但 SEAL 证明内生归因不可信，所以 reflection 只能当 hypothesis 生成器。归因要变成可证伪的 intervention：改机制 ΔM，观察跨任务 outcome shift，通过则进入谱系。把 DGM 的"reasoning proposes, environment disposes"写成契约。

**痛点②真实后果评价。** AlphaEvolve 的价值在于 evaluator 级联。GNOSIS 的 evaluator 必须下游于真实后果，且按 SEAL 四条件设计：exogeneity（agent 写不了）、sealing（agent 看不到数值，只收一位 bit）、single-bit 反馈、保守非回归加全状态回滚。RHB 与 DGM 已证明 agent 会攻 evaluator，所以 evaluator 与晋升规则冻结在 L0。

**痛点③分层成本控制。** 三条现成机制叠加：ShinkaEvolve 的 novelty rejection sampling 避免重复 rollout；GEPA 的 minibatch 先筛（小批通过才全量评估）；Lei 等的自适应分配（早停弱 run、重分配强 survivor）。三者共同把成本压到"单位证据增益最大"的方向。门禁下结论前必须做统计检验，样本不足时标记为未判定。

**痛点④版本晋升闭环。** 结构定为：Production Champion + Behavioral Frontier Archive 双轨；island 防早熟收敛；MAP-Elites 或 Pareto 保 niche 与互补；Sealed audit 做门禁；拒绝即回滚全状态；elitism 保证非回归但不依赖单冠军；晋升判定用 bootstrap/permutation/Holm 统计检验。版本语义上，root contract 不变则同 lineage 的演化，root contract 变化则必须 fork 成新 lineage。

## 参考来源

- AlphaEvolve: https://arxiv.org/abs/2506.13131 ; https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/
- OpenEvolve: https://github.com/algorithmicsuperintelligence/openevolve
- GEPA: https://arxiv.org/abs/2507.19457
- EvoPrompt: https://arxiv.org/abs/2309.08532
- DSPy / MIPROv2: https://arxiv.org/abs/2406.11695
- Darwin Gödel Machine: https://arxiv.org/abs/2505.22954 ; https://sakana.ai/dgm/
- SICA: https://arxiv.org/abs/2504.15228
- ShinkaEvolve: https://arxiv.org/abs/2509.19349
- Automated Discovery Has No Universally Superior Harness: https://arxiv.org/abs/2607.18235
- Simple Baselines are Competitive with Code Evolution: https://arxiv.org/abs/2602.16805
- Self-Authored Verification Is Unreliable / SEAL: https://arxiv.org/abs/2607.24300
- MAP-Elites: https://arxiv.org/abs/1504.04909
- PromptBreeder: https://arxiv.org/abs/2309.16797
- STOP: https://arxiv.org/abs/2310.02304
- Gödel Agent: https://arxiv.org/abs/2410.04444
- Reward Hacking Benchmark: https://arxiv.org/abs/2605.02964
- AI Self-Improvement Only Works Where Outcomes Are Verifiable: https://gist.github.com/AnthonyAlcaraz/a0b70a4bb5ce521129e93bf9d33f9698
- The Last AI Built by Humans: Toward Genuine RSI: https://arxiv.org/abs/2609.11873
