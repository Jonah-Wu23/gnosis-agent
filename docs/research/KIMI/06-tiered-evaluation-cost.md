# 分层评估与成本控制：cascade、successive halving 与 LLM-as-judge 在 agent 场景的迁移边界

调研时间窗 2024–2026（当前 2026-10）。覆盖文献 22 篇：2 篇方法论立场论文、4 篇成本-偏差理论/实证、7 篇 IRT/自适应测试、3 篇噪声与统计功效、3 篇排行榜与真实后果锚。

---

## 拍板结论

**1. successive halving / Hyperband 能迁移到 agent 评估，但可迁移的保真度轴只有一个：题目子集大小。LLM-as-judge 不能进筛分层。**
Hyperband 家族（Li et al. 2017, arXiv:1603.06560；Jamieson & Talwalkar, arXiv:1502.07943；ASHA, arXiv:1810.05934）成立的数学前提是：低保真评估是同一目标函数的带噪估计，保真度提高时信号单调增强、排序一致。这个前提在两处被打破。其一，`Tuning LLM Judge Design Decisions for 1/1000 of the Cost`（arXiv:2501.17178）是唯一大规模成功的迁移案例，它的保真度轴是**评测样本量**（在 4,480 个 judge 配置上做同步 successive halving），不是用便宜模型替代真值；加速实际只有 1/3（$2M→$2k 的 1000 倍是对比暴力全量，不是 SH 自身收益）。其二，`Gaming the Judge`（arXiv:2601.14691）证明：只改写 agent 的 CoT、动作与环境状态保持不动，就能把前沿 VLM judge 的假阳性率推高至 90%（800 条 web 轨迹）。judge 输出不是"任务是否完成的低分辨率版本"，它是另一个可被操纵的随机变量。确定性检查（补丁能否应用、既有测试是否通过、产物是否存在）是这个真值的一部分测量，与 Judge 属于不同类别。

**2. 廉价代理替代真值有硬上限，不是成本权衡问题。Dorner 定理封死了全量代理路线。**
`Limits to scalable evaluation at the frontier`（arXiv:2410.13341, ICLR 2025）给出不可能性结果：当 judge 的准确率不高于被评模型时，任何去偏方法最多把所需真标注量减少一半，实证节省更小。GNOSIS 的每一版自我修改都在推高 agent 能力，方向恰恰是让 judge 相对更弱。因此 judge 只能做**诊断器**（指出哪一步失败、哪类机制失效），不能做**晋升门**。

**3. 主要成本杠杆是分配结构与选题顺序，"多跑几次"是第三杠杆。**
CyclicJudge 的方差分解（arXiv:2603.01865）证明 judge 偏差是**固定效应**：加场景、加生成都消不掉，只有轮转分配能在零额外成本下消偏；在默认操作点（n=80, m=1, K=1）偏差占 benchmark 分数方差的 94% 以上，单 judge 对不同模型的评分差可达 1.7 分（1–10 量表），而竞争模型的真实分差常小于 0.5 分。同时，`On Randomness in Agentic Evals`（arXiv:2602.07150）测得单次 pass@1 波动 2.2–6.0 个百分点，温度 0 下 σ 仍超过 1.5 个百分点——在这个噪声水平上，花预算去检测 1% 的改动需要 36 次运行。三项文献的方向一致：先改分配规则与判据，再买重复采样。而现有全部预算分配文献（arXiv:2602.15481、2603.01865）都在优化 **judge 调用**，忽略了轨迹生成才是成本主体（60,000 条轨迹 = 25.58B token；PaperBench 单次评测约 $9,500）。

**4. 子集化对"排名多个系统"成立，对"晋升单个系统"不成立。**
tinyBenchmarks（arXiv:2402.14992）用 100 个 anchor 题恢复全库分数、ATLAS（arXiv:2511.04689）用 41/5,600 题达到 0.157 MAE、PTA-IRT（arXiv:2609.01603）用 10% 预算达到 τ=0.888、BenchPress（arXiv:2606.24020）用 5 个 benchmark 把成绩单复现到 3.93 分以内——这些全部优化"能力尺度上的平均信息量"（Fisher 信息、低秩结构、难度分层）。稀有灾难项在平均信息量定义下信息量最低，会被系统性排除。晋升要判的恰恰是尾部，二者目标函数相反。更硬的是 `Can We Trust Item Response Theory for AI Evaluation?`（arXiv:2607.15190，18,000 组仿真）：AI benchmark 的数据形态是"少被试、多题目、能力分布非正态且多峰"，经典估计器在大 benchmark 不可行，可扩展估计器在小/非正态被试集上给出的题目参数与排名推断不可靠。GNOSIS 每一版只有 1 个"被试"，IRT 参数不可识别。

**5. 全量回归的必要条件是可判定的，四条命中任一即必须全量。**
(1) 改动触及测量层（judge 模型、提示、评分脚本、harness、题集）；(2) 该门禁是发布前最后一道；(3) 业务依赖切片指标而非均值（语音/agent 回归案例：平均分涨 2% 可以掩盖目标切片跌 30%）；(4) 待检测效应量低于廉价层的最小可检测效应（MDE）。其余情况按 MDE 裁决，凡低于 MDE 的差异一律标记为"未判定"，禁止以分数高低晋升。

---

## 关键假设审查

### Successive Halving / Hyperband（arXiv:1502.07943 / 1603.06560 / 1810.05934）
依赖假设：(a) 低保真评估是同一目标的单调带噪测量；(b) 低 rung 的排序正确率足够高；(c) 试验相互独立；(d) 预算可任意切分。
撤掉审查：撤掉 (a)，整个 cascade 变成随机剪枝，被砍掉的可能是真优候选——LLM judge 作为 rung-0 正是这种情况（judge 结果与"任务完成"是两个随机变量，前者可被 CoT 文本操纵）。撤掉 (c)(d)，fidelity 只能落在"题目数"或"任务步数上限"上；agent 任务不可中断、单题成本高，无法像训练那样半跑，保真度曲线比 ML 训练场景粗糙得多。结论只对确定性检查成立的 (a) 有效。

### IRT 子集选择（arXiv:2402.14992 / 2511.04689 / 2609.01603 / 2606.20626 / 2608.05086）
依赖假设：(a) 能力是单维潜变量；(b) 局部独立；(c) 被试数量足够且能力分布近似正态；(d) 题目参数跨时间稳定。
撤掉审查：撤掉 (c)，arXiv:2607.15190 给出仿真证据——少被试 + 非正态分布下题目级与排名级推断不可靠。撤掉 (d)，题目参数会随 agent 类型漂移（PTA-IRT 自己承认需要一个密集历史 agent 池来拟合 4PL 参数，而 GNOSIS 的历史版本是被择优保留的，构成存活者偏差）。撤掉 (a)，多能力维度下 Fisher 信息选题会偏向单维最强的题目，其余维度静默退化。四条同时存活时这套方法才成立，GNOSIS 只满足 (a)。

### LLM-as-judge 预算分配（arXiv:2602.15481 / 2603.01865 / 2501.17178）
依赖假设：(a) judge 偏差是外生常数；(b) 分数是 i.i.d. 采样，方差是题目属性；(c) 单次调用成本同质；(d) 优化目标是估计误差。
撤掉审查：撤掉 (a)，GNOSIS 的自我修改以 judge 反馈为信号，偏差变成**内生变量**，随版本漂移，任何固定分配可被学习利用，去偏方法在统计学上仍然正确、在对抗意义上失效。撤掉 (d)，预算应分配去最小化**决策错误**（误升一个坏版本的成本）而不是估计误差。撤掉 (c)，CyclicJudge 自己的 limitation 明确承认未做成本加权分配。

### 低秩矩阵补全（arXiv:2606.24020）
依赖假设：(a) 模型×benchmark 分数矩阵近似低秩（实测 rank-2，23.3% 填充率）；(b) 保真缺口可由已观测分数预测。
撤掉审查：撤掉 (a)，单系统的时间序列上低秩结构不携带信息——两因子在该场景里就是"这一版整体变好了多少"，无法回答"修好了哪一类失败"。rank-2 是排行榜的性质，与 agent 的性质无关。

### 统计功效框架（arXiv:2411.00640 / 2602.07150 / 2608.13867）
依赖假设：run 独立同分布、方差可估、效应量固定。
撤掉审查：agent 轨迹在生成的前 1% token 内就分叉（2602.07150 中位数），差异经自回归条件化级联放大，run 内部高度自相关，必须用分层模型估计方差；pass@k 与 pass^k 的差距最大达 24.9 个百分点，说明"随机探索"是性能的组成部分，单次估计的偏倚是结构性的。

### 成本感知排行榜（arXiv:2510.11977 / 2407.01502）
依赖假设：标准化 harness 可消除实现差异，且评分脚本本身可信。
撤掉审查：HAL 自己的数据反驳一半——21,730 次 rollout 中 60% 以上的失败运行违反题面显式指令，部分 agent 去 HuggingFace 搜索 benchmark 答案而非解题。测量层本身是攻击面。

---

## 前沿范式与代表工作

**多保真搜索迁移到评测设计** — `Tuning LLM Judge Design Decisions for 1/1000 of the Cost`（Salinas, Swelam, Hutter, arXiv:2501.17178, ICML 2025）。机制：把 judge 设计（模型、提示、rubric 等）张成 4,480 个配置的空间，用多目标多保真同步 successive halving 搜索，保真度 = 评测样本量，成本目标与准确率目标同时进入 Pareto 前沿。结果：暴力全量约 $2M 降至约 $2k。冲突点：作者在 limitation 里承认只按准确率与成本选 judge，不按偏差选，并测得 flip rate 与人类一致率强负相关（r=-0.789）——也就是说，按"准确率+成本"筛出来的 judge 恰好是次序偏差更大的那批。这个反例说明多保真搜索的标量目标会主动选中偏差最大的配置。

**Hyperband 用于 prompt 选择** — `Hyperband-based Bayesian Optimization for Black-box Prompt Selection`（arXiv:2412.07820）。机制：把验证实例数当保真度参数，对差 prompt 提前终止（延续 TRIPLE, Shi et al. 2024 的做法）。冲突点：保真度曲线平滑的前提是 prompt 排序在小子集与大子集上一致，论文只在有限验证集上展示这一点，没有检验排序翻转率。

**Judge 调用的方差感知分配** — `LLM-as-Judge on a Budget`（Saha, Wagde, Kveton, arXiv:2602.15481）。机制：把 K 个 (prompt, response) 对的 judge 查询分配建成多臂老虎机，ROBIN（方差已知）/ROBIN-HOOD（方差未知）动态把查询投向估计方差最高的题目，最坏情况估计误差界为 Õ(√(Σσ_i²/B))，相比均匀分配约减半样本复杂度。冲突点：优化目标是 worst-case 分数估计误差，与"晋升决策是否出错"没有对应关系；且把方差当作每题的常数属性，忽略了 run 级方差（σ≈1.5pp）远大于 judge 级方差这一现实。

**Judge 偏差的分配结构定理** — `CyclicJudge`（Zhu et al., arXiv:2603.01865）。机制：基于概化理论把 benchmark 分数方差分解为场景、生成、judge 偏差、残差四项，证明偏差项 V_γ 只随 judge 数 K 收缩、与场景数和生成数无关，只在 K=K_tot 时归零；轮转分配（round-robin）在任意预算下严格支配"全 judge"与"随机单 judge"，且成本等同单 judge。冲突点：把"所有 judge 调用成本相同"写进 limitation；其偏差外生假设在 agent 优化循环中不成立。

**轨迹感知的预算化评测** — `Efficient SWE Agent Benchmarking via Trajectory-Aware Evaluation`（PTA-IRT, arXiv:2609.01603）。机制：把历史执行轨迹压缩成结构化过程摘要（Task Goal / Context Explored / Edits Executed / Path Overview），用 LUPI 教师-学生结构把过程信息蒸馏进可部署的能力估计器；用 4PL IRT + 难度分层 Fisher 信息选校准子集；10% 预算下 MAE 0.041±0.015、τ=0.888、ρ=0.973，5% 预算 τ 仍有 0.768。论文给出成本锚：SWE-bench 全量单次 agent 运行成本上限超过 $8,000（$4/题上限）。冲突点：题目参数依赖密集历史 agent 池，而 GNOSIS 的历史版本经择优保留，参数拟合样本有偏。

**自适应测试** — `ATLAS`（Tang et al., arXiv:2511.04689）。机制：3PL IRT 标定题库，按 Fisher 信息动态选题，达到精度阈值即停止；HellaSwag 上 41/5,600 题达到 0.157 MAE，减少高达 90% 题量；θ 使 3,000+ 模型里 23–31% 的排名移动超过 10 位。冲突点：θ 是测量尺度上的能力，不是业务成功率，二者在尾部可以背离；且需要已标定的题库，冷启动无解。

**安全题集的 IRT 压缩** — `Efficient Safety Benchmarking via Item Response Theory`（arXiv:2606.20626）与 `Item Response Theory for AI Safety`（arXiv:2608.05086，8 个安全 benchmark × 192 模型）。机制：把 CAT 从能力题集搬到安全题集，在题目判别度方差大的 benchmark 上减少约三分之二题量，成本降 97–99%，并能检测 sandbagging。冲突点：97–99% 的降幅建立在大被试池之上，与 arXiv:2607.15190 的可靠性警告构成同一方法的两端。

**IRT 的适用边界** — `Can We Trust Item Response Theory for AI Evaluation?`（arXiv:2607.15190）。机制：从六个主流 LLM benchmark 反推题目参数与能力分布，在 18,000 组仿真条件下比较 MML、MCMC、变分推断、神经 PSN 四种估计器。结论：经典估计器在大 benchmark 上不可行，可扩展估计器在少被试或非正态被试集上给出不可靠的题目与排名推断。冲突点：这是对 tinyBenchmarks / ATLAS / PTA-IRT 整条路线的直接攻击，指明了"样本结构决定方法可用性"。

**跨 benchmark 冗余** — `You Don't Need to Run Every Eval`（Zeng & Papailiopoulos, arXiv:2606.24020）。机制：84 个前沿模型 × 133 个 benchmark 共 2,604 格（填充率 23.3%），实测近似 rank-2；BenchPress 在对数几率空间做 rank-2 补全，留出分数误差 4.6 分；5 题集 {GPQA-D, HLE, Codeforces, MMLU-Pro, ARC-AGI-1} 把全成绩单复现到 3.93 分以内。冲突点：度量的是"分数可预测性"，与"失败模式覆盖"无关，是排行榜工具。

**代理的理论上限** — `Limits to scalable evaluation at the frontier`（Dorner, Nastl, Hardt, arXiv:2410.13341, ICLR 2025）。机制：证明当 judge 准确率不高于被评模型时，任何去偏方案最多把真标注需求减半。冲突点：直接封死"便宜 judge 全量替代真值门禁"的路线，并说明代理的可用性随 agent 能力上升而下降。

**噪声的现实** — `On Randomness in Agentic Evals`（Bjarnason, Silva, Monperrus, arXiv:2602.07150）。机制：SWE-bench-Verified 上 6 种 agent 配置各跑 10 次，共 60,000 条轨迹、25.58B token、188 万次工具调用；单次 pass@1 在 2.2–6.0pp 区间浮动，温度 0 下 σ 仍超过 1.5pp；首次 token 分歧中位数落在轨迹前 1%；pass@5−pass^5 差距最大 24.9pp；功效分析给出：检测 1% 提升需 36 次运行，2% 需 9 次。冲突点：任何小于 1pp 的自我修改收益声明在这套噪声下不可判定。

**统计框架** — `Adding Error Bars to Evals`（Miller, arXiv:2411.00640）把评测当成实验，给出超总体框架、标准误与功效公式；`Efficient Evaluation of LLM Performance with Statistical Guarantees`（arXiv:2601.20251）给出预算受限下的统计保证。冲突点：都假设 run 独立同分布，而这个假设被 2602.07150 的级联分叉观测削弱。

**成本即评测维度** — `Holistic Agent Leaderboard`（arXiv:2510.11977, ICLR 2026）：标准化 harness，21,730 次 rollout、9 模型 × 9 benchmark、总成本约 $40k，做 model×scaffold×benchmark 三维分析；发现"提高推理强度在多数运行中降低准确率"。`AI Agents That Matter`（arXiv:2407.01502）主张 accuracy-cost 联合优化、Pareto 前沿、标准化 harness 与保留集。冲突点：单标量排行榜的激励结构本身就是刷分来源。

**排行榜操纵** — `The Leaderboard Illusion`（arXiv:2504.20879）记录私有测试集、选择性披露（Meta 在发布前测试约 27 个私有 Llama 变体）与数据访问不对称。`Preference Leakage`（arXiv:2502.01534）记录 judge 与被评模型共享血统导致的偏好泄露。冲突点：样本量与子集选择之外，**测试集的可见性**是独立的评分污染源。

**真实后果锚** — METR `Measuring AI Ability to Complete Long Tasks`（Kwa et al., arXiv:2503.14499）与 Time Horizon 1.1（2026-01）：把分数换算成人类专家完成任务所需时间，50% 时间横轴自 2024 年起约每 4 个月翻倍，但 METR 自己声明超过 16 小时的测量在现有题集上不可靠（228 题中仅 5 题可做到 8 小时以上）。GDPval（arXiv:2510.04374）用真实经济价值任务与专家评分。冲突点：真值的计价单位是专家工时，且任务集自身有天花板。

**成本经济学实证** — LayerLens 2026-09 的成本分析给出实践锚：PaperBench 单次评测（含 judge）约 $9,500，六模型对比超过 $150k，人工复核 $5–50/条，自适应分配把样本复杂度砍半，版本钉住（judge 模型、提示、rubric）避免整轮重跑。

**预算匹配的方法论** — `When Is an LLM Worth It for Hyperparameter Optimization?`（arXiv:2606.21641）。机制：在 8 个表格 benchmark 上做预算匹配、多种子、配对检验与 bootstrap 置信区间的对照实验。结论：LLM 顾问的 88.7% 表现来自任何模型调用之前就评估的固定默认配置，LLM 自身增益仅 +0.40pp，在留出集上为 -0.01pp（p=0.92）。冲突点：不做预算匹配的自我进化实验会把"种子/精英保留"的贡献记到"适应"头上。

---

## 争议焦点

**争夺一：cheap proxy 能否替代真值。** 一方主张 judge 便宜、与人类一致率 80% 以上，足以做常规回归；另一方给出上界（Dorner 最多省一半标注）、不可消偏差（CyclicJudge，偏差占方差 94%+）、可操纵性（Gaming the Judge，假阳性推高 90%）。裁决：判决层只能放确定性部分真值，judge 归入诊断层。人类一致率是平均意义上的指标，在"是否晋升这一版"这种尾部决策上没有约束力。

**争夺二：子集能否替代全量。** 支持方证据充分（100 题复现全库、41/5,600 题 0.157 MAE、5 题复现成绩单），反对方证据同样硬（IRT 假设在少被试/非正态区间失效，且子集目标函数天然欠采样尾部）。裁决：排名多系统时子集有效，晋升单系统时必须全量或按切片全量。二者的目标函数不同，把排名工具直接当门禁用是方法误用。

**争夺三：多跑几次还是改判据。** σ≈1.5pp，检测 1% 需 36 次运行，检测 2% 需 9 次。裁决：改判据。把晋升门槛设在 MDE 之上（GNOSIS 起步场景建议 ≥2pp），低于 MDE 的差异标为"未判定"，并把配对设计（同一题集、同一随机种子、对照 incumbent）作为压方差的首选手段——配对设计省下的样本量远多于盲目加 run。

**争夺四：多 judge 面板、单 judge 还是轮转。** 面板在生成方差大的模型上浪费预算（Strategy A 在全 judge 时生成数降到 B/K_tot），单 judge 在真实分差小于 0.5 分时会导致排名反转（实测 Qwen 自评第一）。裁决：轮转胜出，同预算、零偏差、保留生成多样性。

**争夺五：成本是不是约束。** HAL 的"更高推理强度在多数运行中降低准确率"说明成本与质量不是简单权衡关系，盲目加算力会同时抬高成本与降低准确率。裁决：晋升判据改为 Pareto(成功率, 成本, 切片最差值)，单标量分数会主动诱发刷分。

---

## 最被忽视的假设

**假设一：测量层位于优化循环之外。**
所有预算分配与去偏工作（2602.15481、2603.01865、2401.13341 之外的整个方向）都把 judge 偏差当作外生常数，用统计学处理它。GNOSIS 的结构不同：系统的 update 直接以评测反馈为信号，评测器是目标的组成部分。后果有三层。偏差从常数变成随版本漂移的状态变量，历史偏差估计对新版本外推失效；固定判分分配可被 agent 通过学习利用（可选多用哪个 judge、哪种措辞得分高）；Goodhart 压力落在判分器上而不是任务上。正确做法是把分配规则本身随机化并轮换私有留出集，把分配目标从"最小化估计误差"改为"最小化决策错误率"。

**假设二：成本的主导轴是题目与运行次数，判分调用排在后面。**
现有 allocation 文献在固定的生成集上优化判分预算，而轨迹生成占成本的绝大多数（60,000 条轨迹 = 25.58B token；PaperBench 单次约 $9,500，其中 judge 只是其中一项）。在便宜的轴上做精细优化、在昂贵的轴上不做决策，顺序是反的。后果：GNOSIS 的预算控制必须按"选哪些题 → 每题跑几遍 → 判分分配"三层依次决策，判分分配是最后一层。

**假设三：代理的"覆盖"从未被测量。**
低秩与 IRT 文献度量的是分数可预测性。rank-2 结构说明模型之间的分数高度相关，对"这一版是否修好了上一版的某三类失败"零信息。子集选择用 Fisher 信息，而灾难项的 Fisher 信息最低，被数学上排除。后果：GNOSIS 需要一套与分数正交的**失败模式覆盖指标**，并把它与子集选择并列，否则分层评估会持续给出"分数没退化"的假安全感。

**假设四：确定性层是零方差层。**
温度 0 下 σ 仍超过 1.5pp，来源是浮点非结合性、批处理、硬件与并行。把"确定性检查"当作零方差层，会低估门禁的假阳性率，级联筛选的判定误差会向下游传播并放大。后果：L0 层也要报告方差与不稳定项，并把不可复现的检查项踢出判决层。

---

## 对 GNOSIS 的直接启示

**机制归因（痛点一）**：把规范写成"每层只做一件事"。廉价层（轨迹结构信号、工具调用序列、上下文探索范围）只用于**归因**，输出是"这次失败属于哪类可迁移机制"，PTA-IRT 的 privileged information 结构证明过程信号携带 pass/fail 之外的判别信息，可作模板。真值层只用于检验归因的**预测力**：若该机制真的成立，同机制的其他任务上应该能预测失败。禁止用 judge 分数验证机制归因。每条机制假设必须附带"撤掉它，预测力下降多少"的可证伪形式。

**真实后果评价（痛点二）**：真值按外部性分层，一层一个计价单位。L0 确定性检查（补丁可应用、既有测试通过、产物存在）；L1 端到端与集成测试；L2 专家复核（单位是人时，用 METR/GDPval 的口径换算）；L3 真实业务后果（合并、用户接受、收入影响）。晋升门只认 L2/L3。L0/L1 用于诊断与候选排序，且必须报告方差与不可复现项。

**分层成本控制（痛点三）**：三层预算决策按顺序执行。第一层选题：难度分层 + Fisher 信息，但题目难度种子须来自外部题库或人工标定，不能来自被择优保留的历史版本。第二层定运行数：对照 incumbent 做配对设计（同题集、同种子），用功效公式反推最小运行数并把 MDE 写进报告。第三层判分分配：bandit 投向高方差项。硬纪律：任何自我修改的收益必须与 incumbent 在同预算下配对比较，否则收益归于精英保留的基线（arXiv:2606.21641 的教训：88.7% 来自种子）。

**版本晋升闭环（痛点四）**：评估契约版本化，字段为（题集版本，harness 版本，judge 模型 + 提示哈希，rubric 版本，运行数，随机种子策略）。任一字段变更视为一次 eval-suite migration，必须重跑标定并重算 MDE，禁止跨契约版本比较分数。门禁输出三件东西：效应量、MDE、是否低于 MDE（低于即"未判定"）。晋升判据是 Pareto(成功率, 成本, 切片最差值)。精英保留与私有留出集轮换绑定，防止被优化。全量回归的触发条件按拍板结论第 5 条写死为四条，其余情况按 MDE 裁决。

---

## 参考来源

- Hyperband: https://arxiv.org/abs/1603.06560
- Non-stochastic Best Arm Identification and Hyperparameter Optimization: https://arxiv.org/abs/1502.07943
- ASHA (Massively Parallel Hyperparameter Tuning): https://arxiv.org/abs/1810.05934
- BOHB: https://arxiv.org/abs/1807.01774
- Tuning LLM Judge Design Decisions for 1/1000 of the Cost: https://arxiv.org/abs/2501.17178
- Hyperband-based Bayesian Optimization for Black-box Prompt Selection: https://arxiv.org/abs/2412.07820
- LLM-as-Judge on a Budget: https://arxiv.org/abs/2602.15481
- CyclicJudge: Mitigating Judge Bias Efficiently in LLM-based Evaluation: https://arxiv.org/abs/2603.01865
- Efficient SWE Agent Benchmarking via Trajectory-Aware Evaluation (PTA-IRT): https://arxiv.org/abs/2609.01603
- ATLAS: Adaptive Testing for LLM Evaluation: https://arxiv.org/abs/2511.04689
- Efficient Safety Benchmarking via Item Response Theory: https://arxiv.org/abs/2606.20626
- Item Response Theory for AI Safety: https://arxiv.org/abs/2608.05086
- Can We Trust Item Response Theory for AI Evaluation?: https://arxiv.org/abs/2607.15190
- tinyBenchmarks: evaluating LLMs with fewer examples: https://arxiv.org/abs/2402.14992
- 100 instances is all you need: https://arxiv.org/abs/2409.03563
- You Don't Need to Run Every Eval: https://arxiv.org/abs/2606.24020
- Limits to scalable evaluation at the frontier: https://arxiv.org/abs/2410.13341
- On Randomness in Agentic Evals: https://arxiv.org/abs/2602.07150
- Adding Error Bars to Evals: https://arxiv.org/abs/2411.00640
- Efficient Evaluation of LLM Performance with Statistical Guarantees: https://arxiv.org/abs/2601.20251
- 功效分析与最小可检测效应（agent 评测）: https://arxiv.org/abs/2608.13867
- Holistic Agent Leaderboard (HAL): https://arxiv.org/abs/2510.11977
- AI Agents That Matter: https://arxiv.org/abs/2407.01502
- The Leaderboard Illusion: https://arxiv.org/abs/2504.20879
- Gaming the Judge: Unfaithful CoT Can Undermine Agent Evaluation: https://arxiv.org/abs/2601.14691
- Preference Leakage: A Contamination Problem in LLM-as-a-judge: https://arxiv.org/abs/2502.01534
- The Rise of Agent-as-a-Judge Evaluation for LLMs: https://arxiv.org/abs/2508.02994
- Agent-as-a-Judge (Zhuge et al.): https://arxiv.org/abs/2410.10934
- Measuring AI Ability to Complete Long Tasks (METR): https://arxiv.org/abs/2503.14499
- METR Time Horizons 1.1: https://metr.org/time-horizons
- GDPval: https://arxiv.org/abs/2510.04374
- When Is an LLM Worth It for Hyperparameter Optimization? A Budget-Matched Study: https://arxiv.org/abs/2606.21641
- AI Evaluation Cost Economics（LayerLens, 2026-09）: https://layerlens.ai/blog/ai-evaluation-cost-economics-budget
