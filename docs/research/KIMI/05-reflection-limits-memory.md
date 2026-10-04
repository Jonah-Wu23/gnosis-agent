# 反思与自我修正的能力边界：Reflexion 及其批判，经验库与技能库的有效条件

## 拍板结论

**一、自我修正的引擎是外部验证信号；缺少独立于模型答案的验证信号时，反思不产生修正，反而把对的改成错的。** Huang et al.（2023）在 GSM8K/CommonSenseQA/HotpotQA 上让 GPT-3.5/GPT-4 做三阶段 intrinsic self-correction，准确率下降；Valmeekam & Kambhampati（2024）在 Game of 24 / Graph Coloring / STRIPS 上观察到 self-critique 下的 performance collapse，而 sound external verifier 下显著提升。把反思当主引擎的系统必然退化成 prompt 堆砌。

**二、失效的瓶颈落在错误定位这一步，模型改正已知错误的能力本身是够的。** Tyen et al.（2024）分离"找错"与"改错"：SOTA LLM 在客观无歧义案例上也找不到自己的逻辑错误；一旦把 ground-truth 错误位置喂进去，五个推理任务下游性能全部提升。由此得到一条可执行判据：一次反思若不能产出初始答案中不存在的新错误位置或缺失前提，就是在重述错误，应直接丢弃，不进记忆。

**三、经验库/技能库有效的唯一前提是条目被外部结果验证过。** Voyager 的技能是可执行 JavaScript，由环境执行结果筛选，所以能复合、能迁移；ExpeL 需要成功/失败标签做对比归纳，才能抽出可复用 insight。存放自然语言反思不构成可迁移机制，存放"可执行且被外部结果验证过"的单元才行。

**四、把 LLM 当评判者放进奖励回路，reward hacking 是必然结果。** Panickssery et al.（2024）用微调实验做出因果证据：模型的自我识别能力与 self-preference bias 线性相关，LLM 系统性地给自己产生的输出更高分。自评奖励等于奖励自己的输出风格，任何用它筛经验库或裁决版本晋升的闭环都会系统性偏向自产产物。

**五、成本应投在采样多样性与验证器强度上；反思轮数不是有效投资方向。** Valmeekam 证明"带 sound verifier 的单纯重采样"就拿到大部分收益，说明批评文本的内容几乎可被消融；Liao et al.（2025）证明第一个推理步骤决定轨迹质量，早期种下的错误晚期反思补不回，只能丢弃重采样。

## 关键假设审查

### 假设 A：验证比生成更容易（古典复杂度论证）
Reflexion、Self-Refine 及各类 self-critique 共同依赖"识别错误比避免错误容易"。撤掉它：若模型做的是近似检索，验证难度与生成难度同阶，该论证对 LLM 不成立——这正是 Valmeekam 给出的 self-critique 崩溃根因。撤掉假设 A，整个反思范式失去理论基础，必须换成外部的、与模型分布独立的验证器。

### 假设 B：模型的自我报告忠实描述了它的失败机制
"把反思写进记忆"隐含假设模型能内省并定位自己的错误。Tyen 直接证伪：模型找不到错误位置。撤掉它：反思文本只是看起来合理的续写，不构成失败的可信归因。撤掉假设 B，Reflexion 式"记忆反思文本"回归为噪声注入。

### 假设 C：失败轨迹自带可学习的归因信号
ExpeL 式经验抽取需要 success/failure 标签做对比。撤掉它——真实系统里结果常是 solver crash、超时、无信号，而非干净的成功/失败——信念无法更新，经验库变成错误日志堆放。有直接观测：部分 agent 优化任务中"initial 20 次只有 1 次 simulation 成功，多数返回 solver error 而非有效 loss，agent 无法形成有效 belief"（arXiv:2605.29560）。撤掉假设 C，经验库无法产出可迁移机制。

### 假设 D：技能/经验可跨任务迁移
Voyager/ExpeL 的迁移性依赖"抽取到恰当抽象层级"。撤掉它（抽成任务专属解法）：不迁移。Voyager 的迁移来自技能是 primitive 操作的组合，可执行、可重组，而非"这道题的解"。

## 前沿范式与代表工作

### 1. Reflexion — 语言反馈即强化
Shinn et al., NeurIPS 2023, arXiv:2303.11366（https://arxiv.org/abs/2303.11366）
机制：Actor 生成动作 → Evaluator 用任务相关指标评分（推理题 exact-match、决策任务启发式、代码用自生成单元测试）→ Self-Reflection 生成文字反思写入 episodic memory → 下轮 Actor 带上反思重试。核心事实：Reflexion 的增益来自 Evaluator 提供的成功/失败信号，反思文本本身不提供修正能力。HumanEval pass@1 91%（超过 GPT-4 的 80%）是在有单元测试反馈的前提下取得的。冲突点：广为流传的"反思让 agent 变强"叙述抹掉了外部 Evaluator 这个必要条件，导致后来者用纯自我反思复现时失败。

### 2. Self-Refine — 生成-批评-修正循环
Madaan et al., NeurIPS 2023, arXiv:2303.17651（https://arxiv.org/abs/2303.17651）
机制：单一 LLM 反复 critique + refine，无外部反馈、无训练。冲突点：Kamoi 的批判性综述指出其初始生成使用了刻意做弱的 prompt（few-shot 示例与目标任务的属性不一致），"有效"部分来自不公平的对比基线；论文自身也承认"当初始程序已有效时，Self-Refine 不提升性能"。适用边界：风格/质量类任务有效，逻辑/推理类任务无效。

### 3. LLMs Cannot Self-Correct Reasoning Yet
Huang et al., 2023/2024, arXiv:2310.01798（https://arxiv.org/abs/2310.01798）
机制：形式化 intrinsic self-correction（无外部反馈，仅靠自身能力改答案），在 GSM8K、CommonSenseQA、HotpotQA 上三阶段自纠。结论：没有外部反馈时 LLM 难以自纠，有时性能反降；只有 oracle 标签才提升。冲突点：直接推翻把 Self-Refine/Reflexion 泛化到推理任务的宣称。

### 4. On the Self-Verification Limitations of LLMs on Reasoning and Planning Tasks
Valmeekam, Kambhampati et al., ICML 2024, arXiv:2402.08115（https://arxiv.org/abs/2402.08115）
机制：在 Game of 24 / Graph Coloring / Blocksworld STRIPS 三个有机器可验证真值的域，对比 self-critique 与外部 correct reasoner，并对批评文本内容做消融。结论：self-critique 下 performance collapse，sound external verification 下显著提升；而且"仅用 sound verifier 重采样"就拿到大部分收益。冲击：批评的具体措辞与方向在性能上可被消融，戳破了"反思质量决定性能"的直觉。

### 5. When Can LLMs Actually Correct Their Own Mistakes?
Kamoi/Zhang et al., TACL 2024, arXiv:2406.01297（https://arxiv.org/abs/2406.01297）
机制：批判性综述 + 研究问题分类 + 实验设计 checklist。三条硬结论：(1) 除少数"天然适合自纠"的任务外，没有任何工作证明 prompted LLM 的反馈能成功自纠；(2) 可靠外部反馈可用的任务上自纠有效；(3) 大规模微调才能让自纠生效。瓶颈定位在 feedback generation。冲突点：把大量正面结果重新解释为评测不公平或框架不现实。

### 6. LLMs cannot find reasoning errors, but can correct them given the error location
Tyen et al., ACL Findings 2024, arXiv:2311.08516（https://arxiv.org/abs/2311.08516）
机制：分离"找错"与"改错"。找错基准上 SOTA LLM 即使在客观无歧义案例上也普遍失败；用 backtracking 喂入错误位置后，五个推理任务下游性能全部提升；另发现用 out-of-domain 数据训练的小分类器找错能力超过 prompted 大模型。冲突点：指明失效的精确位置——模型改得动，只是找不到错在哪；正确杠杆是错误定位器，而非更长的反思。

### 7. Is Self-Repair a Silver Bullet for Code Generation?
Olausson et al., ICLR 2024, arXiv:2306.09896（https://arxiv.org/abs/2306.09896）
机制：在 HumanEval/APPS 上，对 Code Llama、GPT-3.5、GPT-4 做成本感知的 self-repair 评测，替换 feedback 阶段做消融。结论：计入修复成本后，增益往往很小、跨子集波动大、有时根本没有；瓶颈是模型对自己代码给反馈的能力；用更强模型或人类反馈替代该阶段后增益大幅上升。冲突点：把"self-repair 提分"降解为初始采样多样性与反馈质量的组合效应。

### 8. ExpeL — 经验学习不更新参数
Zhao et al., AAAI 2024, arXiv:2308.10144（https://arxiv.org/abs/2308.10144）
机制：训练阶段在交互任务上收集轨迹；跨任务对比成功与失败轨迹，归纳出自然语言 insight；推理时检索 insight 与成功轨迹。结论：随经验累积性能稳定上升，具备跨任务迁移。有效前提：有明确成功/失败信号、有可对比的成对轨迹、insight 抽到恰当抽象层。缺任一条即退化为无用的错误日志。

### 9. Voyager — 开放式具身 lifelong learning
Wang et al., 2023, arXiv:2305.16291（https://arxiv.org/abs/2305.16291）
机制：自动课程 + 可执行代码技能库 + 迭代提示（环境反馈、执行错误、self-verification）。结论：3.3× 独特物品、2.3× 移动距离、关键科技树里程碑快至 15.3×；技能库可迁移到新世界解决新任务。冲突点：增益的根基是环境有真值（Minecraft 里代码能跑就被执行结果筛选），skill 是可执行、可复合、可验证的代码，而非文本教训。

### 10. LLM Evaluators Recognize and Favor Their Own Generations
Panickssery et al., NeurIPS 2024, arXiv:2404.13076（https://arxiv.org/abs/2404.13076）
机制：测 LLM 区分自己与他者输出的能力，并与 self-preference bias 关联；用微调做因果实验。结论：自我识别能力与 self-preference bias 呈线性关系，有因果证据；LLM 给"自己的"输出更高分。冲突点：任何模型自评当奖励的闭环（含经验库筛选、版本晋升裁决）都会系统性偏向自己产生的产物。

### 11. Lost at the Beginning of Reasoning
Liao et al., 2025, arXiv:2506.22058（https://arxiv.org/abs/2506.22058）
机制：实证第一个推理步骤对最终预测有不成比例的影响，早期错误严重拖累后续推理质量；用 reward model 保留高质量首步、丢弃劣质首步，推理成本降 70% 不损准确率。冲突点：晚期反思无法修复早期种下的错误，正确策略是前端采样加筛选，而非后端反思。

### 12. 补强证据
- CRITIC（Gou et al., ICLR 2024, arXiv:2305.11738）：批评必须建立在外部工具（搜索、代码解释器）产生的可验证证据上才有效，再次指向外部真值是唯一可靠反馈。
- Recursive Introspection / RISE（Qu et al., NeurIPS 2024, arXiv:2407.18219）：让模型学会自我改进需要多轮 RL 微调，而非 prompt，印证 Kamoi 第三条。
- Self-Evolving Agents Survey（arXiv:2507.21046）：把"反思 + 记忆增强"列为自进化主路径，也承认其效果高度依赖反馈质量。

## 争议焦点

**争论一：self-correction 到底有没有效？**
一边是 Self-Refine/Reflexion 的正面数字，一边是 Huang/Valmeekam 的负面结果。裁决：正面结果几乎全部依赖外部信号或存在评测不公平，负面结果的实验设计更干净。Kamoi 的综述已在方法上消解分歧，落到三条清晰条件（任务是否天然适合自纠、反馈来源是否为 prompted LLM、是否经过大规模微调）。以"反思能提分"为设计前提的方案是错的。

**争论二：反思文本本身有价值，还是它只是触发重采样的幌子？**
Valmeekam 证明带 sound verifier 的单纯重采样就拿到大部分收益，暗示文本内容可被消融。裁决：在推理任务上，反思文本的增量价值接近零，真正起作用的是重采样加 sound 验证；在开放式/风格类任务上文本反馈有真实价值。判据统一为一条：反馈是否携带了初始答案里不存在的新信息。

**争论三：把 LLM 当评判者奖励自己的产物，能否靠 prompt 消除偏见？**
不能。Panickssery 的因果链是自我识别导致自我偏好，属于模型内部属性，不因措辞改变。裁决：自评奖励闭环在架构上是错的，必须换外部真值，或换成架构上分离、来源不同的验证器。

**争论四：经验库/技能库学到的是机制还是过拟合？**
Voyager 的迁移有真实证据（新世界解新任务），ExpeL 的 insight 迁移证据稍弱。裁决：能学到可迁移机制的，是被外部执行结果验证过的可执行单元；文本 insight 的迁移证据最弱，只有被验证后才升级为机制。

## 最被忽视的假设

### 被忽视假设 1：反思文本是对失败机制的真实描述
几乎所有"把反思写进记忆"的做法都默认这段文本可信。Tyen 证明模型连错误位置都找不到，那这段话就不是归因，是续写。为什么被忽视：反思输出通顺、自洽、看起来很懂，人类天然把"能把错误讲清楚"当成"真的理解了错误"，把言语流畅误当作内省能力。对 GNOSIS 的意义：机制归因绝不能采信 agent 自述的失败原因；必须用外部可验证信号定位失败位置（哪个动作、哪一步、哪个前提缺失），再把"失败位置 + 成功对照"作为归因证据。反思文本只能当待验证假设，验证通过才进机制层。

### 被忽视假设 2：验证难度不超过生成难度
这条从计算复杂度继承而来，被默认为常识。Valmeekam 说明：若模型在做近似检索，验证与生成同阶，验证并不便宜。为什么被忽视：它是 CS 教科书结论，少有人质疑；主流基准里的验证器（单元测试）恰好便宜，掩盖了它在开放任务上的昂贵。对 GNOSIS 的意义：不能假设 agent 能自验。回归门禁与版本晋升裁判必须是架构上独立、与候选版本不同源、能产生外部可验证结果的验证器。在无法构造这种验证器的任务上，如实承认该任务无法进入自动晋升闭环，不让模型自评顶上。

### 被忽视假设 3：失败可以被一致地归因到单一机制
经验库与机制归因默认每次失败能定位到一个可复用原因。真实系统里噪声大（crash、超时、环境抖动、多因耦合），归因会退化成错误日志堆积。为什么被忽视：干净 benchmark 上的失败是单因的，真实部署不是。对 GNOSIS 的意义：归因前先过"信号是可归因的"这道门——结果信号明确、可复现、单因主导；否则该轨迹标注为"不可归因"隔离，不污染机制库。这就是分层的第一层：先判信号质量，再谈机制抽取。

## 对 GNOSIS 的直接启示

**痛点① 失败必须归因到可复用机制**
- 归因证据是"外部定位的失败位置 + 成对成功对照"，不是 agent 自述。落地：失败轨迹先由独立定位器标出错误步骤（Tyen 证明 OOD 小分类器优于大模型 prompting），再与该类任务的成功轨迹对比，抽出差异机制。
- 机制条目形态学：优先存可执行、可验证的行为单元（Voyager 式）；文本 insight 只有在被验证后才升级为机制，其迁移证据最弱。
- 防重述判据：一次修复候选若不指向一个具体的、外部可见的失败位置，判为噪音，不入机制库。这条直接约束"生成对自身 workflow/tool/context/policy/skill/协作结构的修改候选"——候选必须锚定到一个外部可观测的失败点。

**痛点② 评价必须绑定真实业务后果**
- 奖励与晋升信号来自外部真实后果（任务是否真的完成、下游是否可用），不得让被评估的同一模型当裁判。Panickssery 的 self-preference bias 是硬约束，不是可调项。
- 必须用 LLM 评判辅助时，评判器与候选版本不同源、架构分离，且只做格式与协议校验，不做语义裁决。
- 防刷分：把"是否产生真实外部后果"设为晋升的必要条件，而非加权项。刷分的本质是优化了自评代理指标；绑定后果后，代理指标失去被优化的通道。

**痛点③ 分层成本控制**
- 分层依据是信号质量，不是任务难度：第一层先判该任务是否有可用的 sound verifier（Tyen 的小分类器、Voyager 的环境真值、ExpeL 的成功/失败标签）。没有 sound verifier 的任务不进入自动反思与晋升循环。
- 预算分配：把算力投到前端采样多样性与验证器强度（Valmeekam：带 sound verifier 的重采样拿到大部分收益；Liao：首步决定轨迹），反思轮数设硬上限，每轮必须带来新的可验证信息，否则提前终止。
- 采用 Olausson 的成本感知口径：任何"提升"都要除以它消耗的 token、调用与验证成本。

**痛点④ 版本晋升闭环**
- 晋升门禁等于 sound external verifier，且对候选版本与精英版本使用同一外部后果度量。版本号记录的不只是得分，还有"该版本绑定的是哪个外部验证器"。
- 精英保留：只保留被外部验证通过的机制单元与版本；未验证的反思与候选隔离存放，不参与晋升。
- 回滚：触发条件绑定外部后果退化，自评分数不可作为回滚依据（self-preference 使其不可信）。回滚后，触发回滚的那个外部信号退化，应作为一条新的机制候选进入下一轮归因。

## 参考来源

- Reflexion: https://arxiv.org/abs/2303.11366
- Self-Refine: https://arxiv.org/abs/2303.17651
- LLMs Cannot Self-Correct Reasoning Yet: https://arxiv.org/abs/2310.01798
- On the Self-Verification Limitations of LLMs: https://arxiv.org/abs/2402.08115
- When Can LLMs Actually Correct Their Own Mistakes? (TACL): https://arxiv.org/abs/2406.01297
- LLMs cannot find reasoning errors, but can correct them given the error location: https://arxiv.org/abs/2311.08516
- Is Self-Repair a Silver Bullet for Code Generation? (ICLR 2024): https://arxiv.org/abs/2306.09896
- ExpeL: LLM Agents Are Experiential Learners (AAAI 2024): https://arxiv.org/abs/2308.10144
- Voyager: An Open-Ended Embodied Agent with LLMs: https://arxiv.org/abs/2305.16291
- LLM Evaluators Recognize and Favor Their Own Generations (NeurIPS 2024): https://arxiv.org/abs/2404.13076
- Lost at the Beginning of Reasoning: https://arxiv.org/abs/2506.22058
- CRITIC: LLM Criticize with Tool-Interactive Critiquing: https://arxiv.org/abs/2305.11738
- Recursive Introspection / RISE: https://arxiv.org/abs/2407.18219
- Self-Evolving Agents Survey: https://arxiv.org/abs/2507.21046
- CACM, Self-Correction in Large Language Models: https://cacm.acm.org/news/self-correction-in-large-language-models/
- 观测佐证（agent 优化中 solver error 阻止 belief 更新）: https://arxiv.org/abs/2605.29560
