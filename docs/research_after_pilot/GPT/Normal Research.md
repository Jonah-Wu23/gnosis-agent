总判断：这次先导结果确实不好，而且坏点很集中。AutomationBench 的难度和成本都合格，真正失效的是“失败轨迹 → 可复用知识 → 被 Agent 正确执行 → 修复来源题”这条学习链。当前证据更支持“GNOSIS 的学习器/知识载体没有工作起来”，还没有走到检验作用域门禁、遗忘控制和验证费用优势那一步。

1. 不好在哪里

第一，最严重的是来源题修复率为 0/3。

三轮最终候选全部没有把各自来源任务从失败翻成通过，因此三个候选全部因为 `source_task_not_flipped` 被拒绝，正式知识库接受单元为 0。第 1 轮来源题失败→失败，第 2 轮失败→失败，第 3 轮失败→失败；对应 partial credit 是 0、0、5/6。   pilot_final_report_2026-10-06

这直接击中了整个自进化循环最基础的一环：连刚刚用于反思的原始失败都修不好，后面的跨任务迁移、持续学习和遗忘控制暂时没有研究价值。

第二，24 个独立迁移配对几乎没有正信号。

三轮独立题分别是 2胜/1负/5平、0胜/0负/8平、0胜/1负/7平。合计只有 2 胜、2 负、20 平。知识改变绝大多数任务结果的能力接近于零，少数改善还被少数退化抵消。   pilot_final_report_2026-10-06

因此问题已经超出“门禁过严”。即使完全去掉 SAGE 门禁，这批知识也没有形成稳定正迁移。

第三，生成出来的东西偏离了 GNOSIS 原本最有价值的设计。

科研计划的核心是“带服务/端点作用域的知识单元 + 使用时注入 + 只重测影响面”，知识单元本来应该围绕具体服务或端点组织。   GNOSIS-科研计划

实际三轮里，第 1、3 轮最终都是 GLOBAL 纯文字知识，第 2 轮才用了 T 组件。   pilot_final_report_2026-10-06

这意味着先导实际上退化成了“从单个失败中总结一句全局经验，再希望模型以后自己遵守”。GNOSIS 最核心的 scoped evolution 机制基本没有被真正发动起来。

第四，知识的行为采纳非常差。

29 次知识激活都有后续真实模型调用，但能够直接判定的行为证据里，第 1 轮是 0/1 一致；第 2 轮知识激活没有形成可判定遵循证据，T 组件虽然实际调用了 231 次，却只有 1 次产生 nudge，随后模型仍然创建 `tmp_probe` 并继续 Drive 探索，没有执行知识提示要求的通知；只有第 3 轮出现 1 次明确一致行为。   pilot_final_report_2026-10-06

这里已经出现非常典型的“知识存在、知识也进入系统了，但行为没有跟着改变”。

第五，第 3 轮甚至出现了“学习到了一个语义合理、任务上却错误的规律”。

`project_profitability` 候选知识要求将所有符合范围的项目纳入，并独立应用风险和低毛利规则。模型确实按照这条知识行动，把 Delta 同时标记为 At Risk 和 Low Margin；然而原任务要求排除 Delta，最终只拿到 5/6。换句话说，这条知识成功改变了行为，却把错误解释固化成了可复用规则。   pilot_final_report_2026-10-06

这是比“模型没有读知识”更危险的问题：错误 credit assignment 被永久化。

第六，接口发现仍然是明显的基础瓶颈。

当前版本记录了 151 次 `unauthorized_tool`，其中 B1 95 次、先导 56 次。   pilot_final_report_2026-10-06 AutomationBench 本身就专门考察 API discovery、跨应用协调和业务规则执行，官方还发现大量失败表现为 Agent 声称完成、实际世界状态错误，以及搜索不到数据后过早停止、找错应用、只处理列表的一部分等。[arXiv](https://arxiv.org/abs/2604.18934?utm_source=chatgpt.com)

而你当前规定“首次成功调用作用域内接口以后才注入知识”。   GNOSIS-科研计划 这对“接口都没找到”的失败天然太晚了：Agent 最需要帮助的时候，知识还没有触发。

同时有一个明确的好消息：基准本身选对了。B1 是 15/30 严格通过，正好 50%；平均费用约 $0.0298/题，也落在预定可承受区间。   pilot_final_report_2026-10-06 所以当前没有理由换 Benchmark。

1. 可能的原因

我按重要程度排序。

第一原因：单题、单轨迹、一次性反思的信息量不足，导致严重 credit assignment 问题。

当前 proposer 输入是执行轨迹、环境错误和一个 partial_credit 标量，同时刻意看不到 assertion 文本。   GNOSIS-科研计划

这保持了部署真实性，但代价是 partial_credit=5/6 只告诉模型“六件事错了一件”，没有告诉它究竟是哪件；一条长轨迹又包含 endpoint 搜索、数据选择、参数组织、业务判断、最终写入等多个决策。于是 proposer 很容易从失败轨迹里挑出一个听起来合理的解释，然后把相关性误当成错误原因。Delta 案例正是如此。

第二原因：知识压缩得过头。

你的 GLOBAL 纯文字单元本质属于 condensed experience。ICML 2026 的《Large Language Model Agents Are Not Always Faithful Self-Evolvers》对 13 个 backbone、9 个环境进行受控干预，发现 Agent 对完整原始轨迹的行为依赖明显更强，而对凝练后的规则、总结和启发式经常忽略或误读；作者归因于凝练内容的语义损失、模型内部对当前轨迹的偏置以及既有先验压制外部经验。[Proceedings of Machine Learning Research](https://proceedings.mlr.press/v306/zhao26aw.html?utm_source=chatgpt.com)

你的 0/1、0/1 行为一致证据正落在这个已知 failure mode 上。

第三原因：路由与采纳没有被拆开优化。

《Harness Updating Is Not Harness Benefit》专门区分“能不能写出好 harness 更新”和“solver 能不能真正从更新获益”。论文发现，不同强度模型写 harness 更新的效果差异很小；真正造成低收益的是两种情况：相关 artifact 没有被激活，或者激活以后没有被忠实遵循。[arXiv](https://arxiv.org/abs/2605.30621?utm_source=chatgpt.com)

因此把 DeepSeek V4.1 Flash proposer 换成更贵、更强的模型，优先级很低。你的主要缺口是 activation/adherence。

第四原因：注入触发条件与 AutomationBench 的主要错误类型错位。

计划规定首次“成功调用”作用域接口才注入。   GNOSIS-科研计划 但大量实际问题发生在成功调用之前：不知道用哪个服务、不知道 endpoint、参数错误、拿错记录、未经核验就结束。StarHarness 在同一个 AutomationBench Finance 上取得显著改善时，真正有效的补丁大量集中在 interface repair、环境约定和操作流程压缩，包括参数处理、复杂 schema、误导字段、结构化行操作等。[arXiv](https://arxiv.org/abs/2608.24804?utm_source=chatgpt.com)

也就是说，这类任务大量收益来自“把路修好”，不是在已经成功走上这条路以后追加一句文字提醒。

第五原因：一次失败就产生一条规则，样本粒度过细。

CL-Bench 今年的结果表明，自进化 Agent 经常过拟合最近一次观察，也经常无法在后续实例中复用真正的潜在规律，专用 memory system 甚至输给简单 ICL。[arXiv](https://arxiv.org/abs/2606.05661?utm_source=chatgpt.com) EvoAgentBench 则直接把失败拆成 experience encoding、routing、uptake 三层，并发现目前自动方法没有一个能在所有设置中持续获得正增益。[arXiv](https://arxiv.org/abs/2607.05202?utm_source=chatgpt.com)

你的“一道失败题 → 一次 proposer → 一个长期单元”恰好属于最容易把 episode-specific accident 写进长期记忆的结构。

第六原因：只有失败轨迹驱动更新，缺少“成功行为保护”。

SkillOpt 的一个关键设计就是把成功和失败轨迹分开反思：失败告诉优化器应该修什么，成功告诉优化器哪些行为必须保留；然后只允许 bounded add/delete/replace，小步修改，同时保存 rejected-edit buffer。其消融实验显示 textual learning rate、rejected buffer、slow update 都实质影响最终结果。[Microsoft GitHub](https://microsoft.github.io/SkillOpt/?utm_source=chatgpt.com)

GNOSIS 当前主要让失败自己解释失败，缺少反事实参照。

第七原因：样本只有三轮，放大了搜索成功率不足的问题。

预算只跑到三轮就 `budget_stop`。   pilot_final_report_2026-10-06 StarHarness 在三个环境最终需要 4–12 个“被接受的”改动才获得明显收益，AutomationBench 本身用了 5 个 accepted patches。[arXiv](https://arxiv.org/abs/2608.24804?utm_source=chatgpt.com)

不过这只是次要问题。三轮全部来源不翻转，加上 24 对迁移几乎全平，已经说明扩大轮数只会昂贵地继续采样当前低质量搜索分布。

1. 前人有没有解决方案，以及最值得 GNOSIS 吸收什么

有，而且与你现在的问题高度对应。但现有工作解决的是其中不同环节，目前还没有看到一篇工作同时解决你计划中的“跨领域持续进化 + 可证明作用域 + 验证费用随改动影响面增长”这一整套问题。所以 GNOSIS 的研究空位仍然存在。   GNOSIS-科研计划

最直接的第一条路线来自 Trace2Skill。它明确反对从一条轨迹顺序地产生一条长期技能，而是并行分析一批 execution traces，先生成 trajectory-local lessons，再层级合并成冲突较少的统一 skill。其论文和公开实现都采用“轨迹池 → 多分析器 → consolidation”的结构。[arXiv](https://arxiv.org/abs/2603.25158?utm_source=chatgpt.com)

这几乎就是你现在最需要补的东西。GNOSIS 应该从“每失败题立即写知识”改成“同一 scope / failure signature 累积若干轨迹后再归纳”。单条失败仍可立即产生临时 patch，但只有重复出现的规律才升级为持久单元。

第二条路线来自 SkillOpt。它把 skill 当成冻结 Agent 的可训练外部参数，使用 rollout batch、成功/失败分离反思、bounded edit、held-out gate、rejected-edit memory 和 slow update。[arXiv](https://arxiv.org/abs/2605.23904?utm_source=chatgpt.com)

GNOSIS 最值得拿的是三件东西：成功轨迹作为 preserve evidence；候选必须是“小改动”而非重新总结一个宏大规则；被拒绝的候选继续作为负经验喂给 proposer，避免反复生成同类坏知识。

第三条路线，也是与你 Benchmark 最匹配的，是 StarHarness。

它已经直接在 AutomationBench Finance 上证明：冻结模型以后，演化 harness 仍然可以显著改善结果；其改动范围包括 prompt、tool interface、skill、MCP provider 和 agent-loop configuration，并通过 baseline failure stratification 构造搜索池，搜索任务、隐藏 selection tasks 和最终 held-out tasks 分离。[arXiv](https://arxiv.org/abs/2608.24804?utm_source=chatgpt.com)

公开结果中，AutomationBench Finance 的 domain-objective score 从 57.1% 提高到 83.2%，partial credit 从 67.3% 提高到 86.1%，同时 guardrail violations 大幅下降；主要机制包括 interface repair、environment convention 和 operational knowledge。[MasterNodeAI](https://www.masternodeai.com/en/news/starharness-enterprise-agent-harness-performance?utm_source=chatgpt.com)

这说明 GNOSIS 不应该继续让“纯文本 GLOBAL 单元”占主导。最重要的知识应优先 materialize 成端点级、服务级的结构化 procedure 或 executable wrapper，例如参数规范化、分页、查找策略、写后回读、空值清理、列表完整遍历等。纯文字只承担无法代码化的业务规则。

第四条路线来自经验忠实性研究和 EvoAgentBench。

最合理的升级是把 GNOSIS 自身的学习效果正式拆成三层指标：

encoding：生成的单元本身是否正确；

routing：需要它的时候有没有激活；

uptake：激活后下一段行为是否真正遵循。

EvoAgentBench 已经明确提出这三个诊断维度；ICML 2026 的经验忠实性工作也证明，仅看“有无 memory 后最终分数”会掩盖大量真实失败。[arXiv](https://arxiv.org/abs/2607.05202?utm_source=chatgpt.com)

你现在其实已经有部分 uptake 审计基础，下一版应该把它提升为正式 gate，而不是报告阶段的人工复盘项。

第五条路线来自 SAGE。

SAGE 解决的是“候选已经有一定价值以后，怎样阻止统计噪声和局部增益把退化永久写进去”。它采用逐项 paired comparison、非对称惩罚回归和单侧统计检验，在多个环境显著降低 regression。[arXiv](https://arxiv.org/abs/2609.36043?utm_source=chatgpt.com)

你这次 SAGE 没有出问题。门禁正确地挡住了三个来源题都修不好的候选。这里不应该放松门禁。

第六条路线来自 ACE。ACE 使用 Generator → Reflector → Curator，把经验保存成逐项、增量更新的 playbook，避免每轮整体重写造成 context collapse 和细节丢失。[arXiv](https://arxiv.org/abs/2510.04618?utm_source=chatgpt.com)

它适合作为 GNOSIS proposer 内部的组织方式，但仍应服从 GNOSIS 的 scope，而不要重新回到一个越来越大的全局 playbook。

第七条值得保留的是你原计划里的 TTS-2 和 held-out evaluation。《Rethinking the Evaluation of Harness Evolution》发现，在等反馈、等推理预算下，一些 harness evolution 方法无法稳定击败普通 test-time scaling，而且训练任务上的 harness 改进常无法泛化到 held-out tasks。[Academus](https://academ.us/article/2607.12227/?utm_source=chatgpt.com) 你的计划已经设置 TTS-2、独立留出测试和预算对齐，这部分设计方向是对的。   GNOSIS-科研计划

最终裁决

现在不应该按原计划直接进入正式 GNOSIS 实验。

最值得做的一次架构升级可以概括成一句话：

把 GNOSIS 从“single-failure → condensed text → late injection”改成“trace-grounded pooled learning → operational scoped unit → early scoped activation → encoding/routing/uptake 三层门禁”。

具体形态我建议固定为这一条主线：以服务/端点和失败类型聚类轨迹；同时读取同 scope 的成功与失败轨迹；从多条证据中产生小规模候选 patch；默认生成结构化 procedure 或 executable wrapper；GLOBAL 文字知识必须有跨任务重复证据才能生成；把作用域触达定义从“首次成功调用”前移到“首次相关接口交互/尝试”，让接口发现类知识有机会介入；候选先证明能够修复来源任务，再测 uptake，再进入你现有的影响面 SAGE 门禁。

这条路线同时吸收了 Trace2Skill 的多轨迹归纳、SkillOpt 的小步受控更新、StarHarness 的接口级演化、Faithfulness/EvoAgentBench 的采纳诊断，同时保住 GNOSIS 真正有论文价值的 scoped regression testing。

而且最重要的是：不要把第三轮 5/6 当成“差一点成功”。它暴露的是更有研究价值的问题——Agent 已经忠实执行了新知识，但新知识本身是错的。下一版首先应该解决的，就是“怎样从失败轨迹中学到因果上正确的 operational knowledge”。这才是当前实验真正卡住的核心。