# 面向论文复现的 PaperBench 风格 Rubric 制作指南

## 1. 目标与适用范围

本文档说明如何为一篇新的机器学习论文制作类似 PaperBench 的层级式评分标准。目标不是把论文摘要改写成检查清单，而是建立一套能够客观衡量以下三件事的评价体系：

1. 候选提交是否正确实现了论文方法；
2. 提交是否实际运行了要求的实验；
3. 运行产生的证据是否支持论文的主要实证结论。

PaperBench 的原始 rubric 由研究工程师人工起草，经过内部审查，并与论文原作者多轮核验。其高质量主要来自专家审查、范围澄清和实际评分校准，而不是单纯依靠自动抽取。

### 1.1 最终交付物

为一篇新论文制作 rubric 时，建议至少交付以下文件：

| 文件 | 是否必需 | 用途 |
|---|---:|---|
| `rubric.json` | 是 | 定义评分树、局部权重和叶节点类别 |
| `addendum.md` | 是 | 向复现者公开论文中缺失但完成任务所需的信息 |
| `judge.addendum.md` | 按需 | 只向 Judge 提供判分说明，不得隐藏完成任务所需的信息 |
| `config.yaml` | 是 | 注册论文 ID 和标题 |
| `blacklist.txt` | 按需 | 声明禁止使用的原作者代码、镜像或其他资源 |
| `assets/` | 按需 | 存放允许使用的辅助数据、模板或参考资源 |

### 1.2 本文档中的规范用语

- **必须**：不满足时，rubric 不应进入正式评测。
- **应该**：默认应满足；只有记录了明确理由时才可偏离。
- **可以**：按论文和评测环境选择的可选做法。

### 1.3 最短开发路径

如果准备直接开始开发，可以按以下顺序使用本文档：

1. 按阶段一冻结复现契约，并起草 `addendum.md`；
2. 按阶段二建立“论文主张—实验—证据”清单；
3. 参考第 4 节的两组真实样例构建评分树；
4. 按第 5 节生成 `rubric.json`；
5. 运行结构校验，并按第 7 节逐项验收；
6. 由另一名工程师或领域专家独立试评后再冻结版本。

## 2. PaperBench 的原始设计原则

### 2.1 树状分解

Rubric 是一棵从总目标逐步分解到可独立判断要求的树：

```text
论文核心贡献已复现
├── 数据和模型资源已准备
├── 核心方法已实现
│   ├── 模块 A 已实现
│   ├── 模块 B 已实现
│   └── 训练目标已实现
├── 核心实验已执行
└── 主要实证结论已复现
```

内部节点用于组织和聚合；只有叶节点由 Judge 直接评分。一个父节点的所有子节点都满足时，原则上应足以说明父节点已经满足。

### 2.2 原子化叶节点

PaperBench 持续拆分要求，直到熟悉论文的专家预计可以在 15 分钟以内判断一个叶节点是否满足。

叶节点应当：

- 只判断一件事；
- 能够二元判定为通过或不通过；
- 可以从代码、执行日志或结果文件中找到证据；
- 不要求评分者猜测作者意图；
- 尽可能指向论文中的章节、公式、算法、表格或图；
- 不强制与论文结论无关的代码组织方式。

### 2.3 局部权重

每个节点的 `weight` 表示该节点相对于同一父节点下兄弟节点的重要程度，而不是实现难度或算力成本。

父节点得分为子节点得分的加权平均：

```text
score(parent) = Σ(score(child) × weight(child)) / Σ weight(child)
```

最终论文得分为根节点得分。一个叶节点的全局有效权重，是其路径上每一层归一化局部权重的乘积。

### 2.4 三类证据

PaperBench 将叶节点分成三种主要类型：

| 类别 | 判断问题 | 主要证据 |
|---|---|---|
| `Code Development` | 提交中是否存在正确的实际代码实现？ | 源代码、配置、脚本 |
| `Code Execution` | `reproduce.sh` 是否真正执行了该项内容？ | 启动脚本、执行日志 |
| `Result Analysis` | 执行产物是否支持论文中的结果或结论？ | 结果表、JSON、日志、图 |

同一研究贡献通常需要分别检查“写了”“跑了”和“结果成立”。三者不能互相替代。

### 2.5 Addendum 处理欠规范问题

论文经常没有完整说明数据划分、模型版本、预处理、随机种子或评测细节。PaperBench 为每篇论文提供 `addendum.md`，必要时还提供只对 Judge 可见的 `judge.addendum.md`。

原始 PaperBench 通常将只在附录中新增的实验视为非核心范围；正文提出、仅把具体实现细节放入附录的实验仍属于范围。

## 3. 推荐制作流程

### 3.1 阶段一：确定复现契约

在写 rubric 之前，先冻结评价设置。至少回答：

- 是完整复现，还是只检查代码实现？
- 是否必须从头训练？能否使用预训练 checkpoint？
- 允许使用哪些公开代码、模型、API 和数据？
- 是否禁止查看原作者代码仓库？
- 可用 GPU、CPU、时间、内存和存储是多少？
- 哪些正文实验必须执行？
- 哪些附录实验、额外分析或昂贵实验不在范围内？
- 结果允许多大随机误差？
- Judge 能读取哪些文件类型？
- `reproduce.sh` 应生成哪些证据文件？

这些决定应记录在 addendum，而不是隐藏在 rubric 中。

### 3.2 阶段二：建立论文主张清单

完整阅读摘要、引言、方法、实验、结论和附录，建立如下工作表：

| 主张或成果 | 论文来源 | 所需实现 | 所需实验 | 预期证据 | 是否核心 |
|---|---|---|---|---|---|
| 新方法优于主要 baseline | Table 2 | 新方法和 baseline | 主实验 | 指标表 | 是 |
| 模块 A 带来主要增益 | Table 4 | 可关闭模块 A | 消融实验 | 指标差值 | 是 |
| 方法对超参数较稳定 | Appendix C | 参数扫描 | 稳健性实验 | 曲线或表格 | 可选 |

应重点覆盖：

- 摘要和引言明确列出的贡献；
- 正文中的主要表格和图；
- 方法部分的关键模块、公式和算法；
- 数据集、模型、预处理和超参数；
- 重要 baselines、指标和统计方法；
- 支撑核心结论的消融与稳健性实验。

完成后检查：每一个声称要评分的核心结论，是否都有相应实现、执行和结果证据。

### 3.3 阶段三：构建顶层树

优先按科研成果组织，而不是按代码文件组织。推荐骨架：

```text
论文核心实证贡献已复现
├── 资源与数据准备
│   ├── 数据集可获取
│   ├── 数据划分正确
│   └── 预训练模型可获取
├── 方法实现
│   ├── 核心模块 A
│   ├── 核心模块 B
│   └── 优化目标与训练过程
├── 实验执行
│   ├── 主实验
│   ├── Baseline
│   └── 消融实验
└── 结果与结论
    ├── 主结果结论
    ├── 消融结论
    └── 定性分析
```

不要使用 `models.py 已完成`、`train.py 已完成` 作为评分目标，因为不同实现可以采用完全不同的文件组织。

### 3.4 阶段四：把节点拆成原子要求

不合格的叶节点：

> 模型已正确实现并完成所有实验，结果与论文一致。

这个节点包含至少三个可以独立失败的条件，无法合理二元评分。

推荐拆成：

```text
- 模型包含论文 §3.2 定义的门控适配器。
- 训练损失包含 Equation 4 的正则项。
- reproduce.sh 执行 Dataset A 上的主实验。
- 主实验输出记录 accuracy、F1 和随机种子。
- Method X 在 Dataset A 上优于 Baseline Y。
```

判断是否还需继续拆分时，使用以下测试：

1. 该要求中是否存在两个可以分别成功或失败的动作？
2. Judge 是否需要检查多个不相关文件或多个独立实验？
3. “部分完成”时是否很难决定给 0 还是 1？
4. 专家是否可能需要超过 15 分钟？

任一答案为“是”，通常应继续拆分。

### 3.5 阶段五：设计可验证证据

每个叶节点都应在设计时明确其证据来源。

#### 3.5.1 Code Development

适合检查：

- 模块结构；
- 损失函数；
- 数据处理；
- 采样或推理算法；
- 配置和超参数；
- 评测指标实现。

不要把 README 中的描述当成实现证据。

#### 3.5.2 Code Execution

适合检查：

- `reproduce.sh` 是否调用了对应入口；
- 日志是否显示数据准备、训练或评估运行；
- 所需任务、模型、数据集和种子是否被覆盖；
- 运行是否产生预期输出。

#### 3.5.3 Result Analysis

适合检查：

- 结构化结果文件是否由复现过程生成；
- 指标是否达到预定义标准；
- 方法间排序、趋势和消融结论是否成立；
- 图表或定性结果是否支持论文主张。

推荐要求提交生成机器可读的 `results.json` 或 CSV，而不只是控制台文本。

### 3.6 阶段六：定义结果容差

随机训练结果通常不应要求与论文数值完全相等。可以选择：

- 绝对误差：`|reproduced - reported| <= δ`；
- 相对误差：`|reproduced - reported| / |reported| <= r`；
- 排序：新方法优于指定 baseline；
- 趋势：随着数据量增加，指标总体改善；
- 比率：达到论文结果的某一比例；
- 置信区间或多随机种子统计；
- 论文核心定性结论成立。

阈值必须在查看候选提交之前确定。不要根据已经看到的结果反向调整阈值。

对于极昂贵实验，可以同时定义：

- 完整规模复现标准；
- 在受限资源下验证算法正确性的代理实验标准。

二者应位于不同节点，并具有不同权重。

### 3.7 阶段七：分配权重

建议先使用简单的 `1/2/3`：

```text
核心方法         3
主实验           3
关键消融         2
辅助分析         1
额外可视化       1
```

权重审核原则：

- 反映科研重要性，不反映工作量；
- 同一贡献不要在多个分支重复计分；
- 每个父节点下只比较其直接子节点；
- 检查树过深是否让重要叶节点的有效权重意外变小；
- 在最终发布前计算并检查所有叶节点的全局有效权重。

### 3.8 阶段八：处理依赖关系

PaperBench 原始格式主要通过子节点顺序隐含依赖：

```text
数据准备 → 方法实现 → 实验执行 → 结果分析
```

如果必须完全兼容 PaperBench，应把前置节点排在依赖它的节点之前。

如果允许扩展格式，推荐加入显式依赖：

```json
{
  "id": "main-result",
  "depends_on": ["method-implementation", "main-experiment-execution"]
}
```

显式依赖可以区分“前置条件失败导致不可评”和“前置条件满足但结果不成立”。注意原始 `TaskNode` 不认识 `depends_on`，使用前需要修改数据类和 Judge。

### 3.9 阶段九：编写 Addendum

建议 addendum 至少包含：

- 任务范围和排除项；
- 数据集版本与划分；
- 模型和 checkpoint 版本；
- 未公开的预处理细节；
- 随机种子和重复次数；
- 可接受的替代依赖；
- 评测脚本和指标定义；
- 算力受限时允许的缩小实验；
- 数值容差和判断规则；
- 必须输出的证据文件。

如果某项信息是完成任务所必需的，就应放入对候选者公开的 addendum；不要只放进 judge addendum。

### 3.10 阶段十：专家审核和校准

推荐采用 PaperBench 类似的审核流程：

1. 第一位研究工程师起草；
2. 第二位工程师逐项核对论文来源；
3. 领域专家检查技术正确性；
4. 尽可能邀请原作者审阅；
5. 根据反馈修改 rubric 和 addendum；
6. 使用实际提交校准；
7. 最终签字确认并冻结版本。

至少使用三类提交测试：

1. 接近完整的正确复现；
2. 只实现代码、没有真正运行实验的提交；
3. 含明显缺陷、缺失结果或预先写死结果的提交。

让两位评分者独立打分，并检查：

- 哪些叶节点经常产生分歧；
- 哪些要求需要论文之外的信息；
- 哪些节点包含多个独立条件；
- 是否存在重复计分；
- 是否能通过硬编码结果骗过评分；
- 是否存在合理实现却被 rubric 错误排除；
- 每个节点是否能在目标时间内完成判断。

分歧大的节点应重写或继续拆分，而不是只增强 Judge 提示词。

## 4. 真实 Rubric Few-shot

下面两组内容逐字段摘录自 PaperBench 的 BBOX 论文
[`data/papers/bbox/rubric.json`](data/papers/bbox/rubric.json)，没有翻译或改写，适合作为生成新 rubric 时的 few-shot。示例只截取完整评分树中的子树，因此不能单独代表整篇论文的覆盖范围。

使用时应学习其**分解方式和证据类型**，不要直接照抄论文专属的公式、数值或实验设置。真实数据也不等于完美模板；生成新 rubric 后仍必须执行第 7 节的检查。

### 4.1 示例一：从论文公式拆到可检查的实现与训练要求

这个子树先把 Equation 3 对应的 NCE 目标拆成正样本项和负样本项，再将训练执行、完整损失、梯度更新和谱归一化分开检查。它展示了两个关键做法：

- 内部节点表达一个较高层的科研目标，叶节点承担实际判分；
- “代码中实现了损失”和“确实用该损失完成训练”属于不同证据类型。

```json
{
  "id": "nce-loss",
  "requirements": "The adapter training loss from Equation 3 has been implemented with spectral normalization and training process from Section 3.2.",
  "weight": 1,
  "sub_tasks": [
    {
      "id": "loss-components",
      "requirements": "The NCE loss components have been implemented.",
      "weight": 1,
      "sub_tasks": [
        {
          "id": "positive-terms",
          "requirements": "The positive sample terms $-\\mathbb{E}_{y^+ \\sim p_{data}(y|x)}[g_\\theta(x,y^+)] + \\alpha\\mathbb{E}[g_\\theta(x,y^+)^2]$ have been implemented.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Method Implementation"
        },
        {
          "id": "negative-terms",
          "requirements": "The negative sample terms $\\mathbb{E}_{y^- \\sim p_\\theta(y|x)}[g_\\theta(x,y^-)] + \\alpha\\mathbb{E}[g_\\theta(x,y^-)^2]$ have been implemented.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Method Implementation"
        }
      ],
      "task_category": null,
      "finegrained_task_category": null
    },
    {
      "id": "training-implementation",
      "requirements": "The training process components have been implemented.",
      "weight": 1,
      "sub_tasks": [
        {
          "id": "adapter-training-execution",
          "requirements": "The adapter has been trained using the NCE loss and spectral normalization for the specified number of steps.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Execution",
          "finegrained_task_category": "Experimental Setup"
        },
        {
          "id": "loss-computation",
          "requirements": "The NCE loss has been implemented according to Equation (3) in the paper, which includes: $\\nabla_{\\theta}\\ell(\\theta) = \\nabla_{\\theta}\\{-\\mathbb{E}_{y+\\sim p_{data}(y|x)}[g_{\\theta}(x,y+)] + \\alpha\\mathbb{E}[g_{\\theta}(x,y+)^2] + \\mathbb{E}_{y-\\sim p_{\\theta}(y|x)}[g_{\\theta}(x,y-)] + \\alpha\\mathbb{E}[g_{\\theta}(x,y-)^2]\\}$, incorporating both positive and negative sample terms with regularization.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Method Implementation"
        },
        {
          "id": "gradient-updates",
          "requirements": "The gradient updates $\\nabla_{\\theta} \\mathcal{L}_{NCE}$ have been implemented with $\\eta=5e-6$ as specified in Appendix H.2.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Method Implementation"
        },
        {
          "id": "spectral-normalization",
          "requirements": "Spectral normalization has been applied to the energy model $g_{\\theta}$ to increase gradient stability.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Method Implementation"
        }
      ],
      "task_category": null,
      "finegrained_task_category": null
    }
  ],
  "task_category": null,
  "finegrained_task_category": null
}
```

开发新 rubric 时，可把该模式抽象为：

```text
论文中的训练目标
├── 目标函数的独立组成项（Code Development）
├── 优化与稳定化机制（Code Development）
└── 使用上述实现完成训练（Code Execution）
```

注意：示例中的 `loss-components` 与 `loss-computation` 存在一定语义重叠。新建 rubric 时，应检查两者是否在重复计分；若证据无法清晰区分，应该合并或重新定义边界。

### 4.2 示例二：配置—执行—结论的证据链

这个子树对应论文 Figure 3(a)，将实验配置、实际推理和结果结论分开。`beam-analysis` 的局部权重为 4，表达结果结论相对于同级实验执行分支更重要，而不是表示它需要四倍工作量。

```json
{
  "id": "beam-size-analysis",
  "requirements": "Figure 3(a) has been replicated.",
  "weight": 1,
  "sub_tasks": [
    {
      "id": "beam-experiments",
      "requirements": "The experiments with different beam sizes have been conducted.",
      "weight": 1,
      "sub_tasks": [
        {
          "id": "beam-configurations",
          "requirements": "The adapter has been configured to run with beam sizes $k$ = 1, 3, and 5.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Experimental Setup"
        },
        {
          "id": "beam-inference",
          "requirements": "The inference has been executed for each beam size configuration.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Code Execution",
          "finegrained_task_category": "Evaluation, Metrics & Benchmarking"
        }
      ],
      "task_category": null,
      "finegrained_task_category": null
    },
    {
      "id": "beam-analysis",
      "requirements": "The results evaluating the impact of beam size match those reported in Figure 3(a).",
      "weight": 4,
      "sub_tasks": [
        {
          "id": "performance-tracking-beam",
          "requirements": "The performance changes across beam sizes ($k$ = 1, 3, 5) have been tracked and calculated.",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Result Analysis",
          "finegrained_task_category": "Evaluation, Metrics & Benchmarking"
        },
        {
          "id": "beam-size-trends",
          "requirements": "The results show that increasing the number of beams contributes to an average performance enhancement of ~2.41% across different adapter sizes (0.1B and 0.3B).",
          "weight": 1,
          "sub_tasks": [],
          "task_category": "Result Analysis",
          "finegrained_task_category": "Evaluation, Metrics & Benchmarking"
        }
      ],
      "task_category": null,
      "finegrained_task_category": null
    }
  ],
  "task_category": null,
  "finegrained_task_category": null
}
```

开发新 rubric 时，可把该模式抽象为：

```text
目标图表或主张
├── 论文指定实验条件已配置（Code Development）
├── 所有条件均实际运行（Code Execution）
├── 输出包含可计算的逐条件指标（Result Analysis）
└── 预先定义的数值容差、排序或趋势成立（Result Analysis）
```

这里的 `~2.41%` 是 BBOX 论文专属目标。迁移到其他论文时，必须根据论文报告值、随机性和资源约束预先制定容差；不要让 Judge 仅凭“match”自行猜测允许偏差。

### 4.3 Few-shot 的使用方式

建议向 rubric 生成器同时提供：

1. 新论文正文及附录；
2. 已冻结的复现契约和 addendum；
3. 论文主张清单；
4. 上述一至两组真实子树；
5. 第 5 节的 JSON 结构约束；
6. 明确指令：只生成初稿，不自动决定缺失事实、结果容差或最终权重。

生成后必须由人逐项核对论文出处。Few-shot 的作用是稳定结构与粒度，不能替代领域判断。

### 4.4 可直接用于生成初稿的指令模板

将下面指令与论文、addendum、主张清单及本节示例一起提供给生成模型：

```text
请为给定论文生成 PaperBench 兼容的 rubric.json 初稿。

目标：评价提交是否实现论文方法、实际执行核心实验，并产生支持核心结论的证据。

硬性约束：
1. 输出一棵 JSON 树；每个节点只包含 id、requirements、weight、sub_tasks、
   task_category、finegrained_task_category。
2. 内部节点的两个 category 均为 null；叶节点必须使用本文档列出的合法类别。
3. 每个叶节点只检查一个可独立成功或失败的条件，并能在约 15 分钟内判断。
4. 分别检查 Code Development、Code Execution 和 Result Analysis，不得互相替代。
5. requirements 尽量引用论文的章节、公式、算法、表格或图。
6. 权重表示同级科研重要性，不表示实现成本；避免重复计分。
7. 不得补造论文未说明的版本、超参数、阈值或实验结果。
8. 无法从论文和 addendum 确认的信息，用单独的 unresolved_questions 列表报告，
   不得写入 rubric.json 作为既定事实。

请先输出 rubric.json，再输出：
- 每个顶层分支覆盖的论文主张；
- unresolved_questions；
- 可能重复计分的节点；
- 需要人工确定的容差与权重。
```

`unresolved_questions` 等审查信息是生成阶段的旁路产物，不属于 PaperBench 的 `rubric.json`。正式落盘时只能保存兼容评分树。

## 5. PaperBench 兼容 JSON 模板

```json
{
  "id": "root",
  "requirements": "The core empirical contributions of the paper have been reproduced.",
  "weight": 1,
  "sub_tasks": [
    {
      "id": "method",
      "requirements": "The proposed method has been implemented.",
      "weight": 3,
      "sub_tasks": [
        {
          "id": "training-objective",
          "requirements": "The implementation includes the objective defined in Equation 4.",
          "weight": 2,
          "sub_tasks": [],
          "task_category": "Code Development",
          "finegrained_task_category": "Method Implementation"
        }
      ],
      "task_category": null,
      "finegrained_task_category": null
    }
  ],
  "task_category": null,
  "finegrained_task_category": null
}
```

原始实现允许的叶节点主类别：

```text
Code Development
Code Execution
Result Analysis
```

允许的细分类别：

```text
Environment & Infrastructure Setup
Dataset and Model Acquisition
Data Processing & Preparation
Method Implementation
Experimental Setup
Evaluation, Metrics & Benchmarking
Logging, Analysis & Presentation
```

内部节点的 `task_category` 应为 `null`；叶节点必须提供合法的 `task_category`。

### 5.1 字段契约

| 字段 | 类型 | 必须满足 |
|---|---|---|
| `id` | string | 全树唯一、稳定、可读；建议使用 `kebab-case` |
| `requirements` | string | 清楚说明可观察的通过条件；叶节点必须能二元判断 |
| `weight` | number | 非负；表示相对兄弟节点的重要性 |
| `sub_tasks` | array | 叶节点为空数组；内部节点至少包含一个子节点 |
| `task_category` | string/null | 叶节点必须使用合法类别；内部节点必须为 `null` |
| `finegrained_task_category` | string/null | 叶节点应该使用合法细分类别；内部节点通常为 `null` |

虽然当前 `TaskNode` 接受浮点数和零权重，但正式 rubric 应优先使用小整数。一个父节点的所有直接子节点都为零权重时，其聚合得分恒为 0；除非这是明确设计，否则必须阻止发布。

### 5.2 最小自动校验

在 PaperBench 仓库根目录执行下面的校验。第一部分复用项目的 `TaskNode` 检查字段、权重和叶节点类别；第二部分补充检查唯一 ID、空文本和同级全零权重：

```bash
uv run python - data/papers/<paper-id>/rubric.json <<'PY'
import json
import sys
from pathlib import Path

from paperbench.rubric.tasks import TaskNode

path = Path(sys.argv[1])
raw = json.loads(path.read_text())
root = TaskNode.from_dict(raw)
seen = set()

def check(node):
    assert node.id.strip(), "node id must not be empty"
    assert node.id not in seen, f"duplicate node id: {node.id}"
    seen.add(node.id)
    assert node.requirements.strip(), f"empty requirements: {node.id}"
    if node.sub_tasks:
        assert any(child.weight > 0 for child in node.sub_tasks), (
            f"all children have zero weight: {node.id}"
        )
    for child in node.sub_tasks:
        check(child)

check(root)
print(f"OK: {len(seen)} nodes validated from {path}")
PY
```

自动校验只能发现结构错误，不能证明要求忠于论文、粒度合适或权重合理。这三项仍需要人工审核。

## 6. 新论文目录模板

```text
data/papers/<paper-id>/
├── config.yaml
├── paper.pdf
├── paper.md
├── rubric.json
├── addendum.md
├── judge.addendum.md
├── blacklist.txt
└── assets/
```

`config.yaml`：

```yaml
id: example-paper
title: "Full Paper Title"
```

完成数据包后，将论文 ID 加入 `experiments/splits/<split>.txt`。`PaperRegistry` 会按目录自动加载，无需在 Python 中注册。

## 7. 发布前检查清单

### 7.1 范围

- [ ] 核心贡献均有覆盖；
- [ ] 正文主要表格和图均已决定纳入或排除；
- [ ] 附录实验范围清楚；
- [ ] 计算资源与时间限制清楚；
- [ ] 禁止资源记录在 blacklist。

### 7.2 树结构

- [ ] 根节点描述完整复现目标；
- [ ] 子节点合起来足以证明父节点；
- [ ] 没有仅用于代码组织的节点；
- [ ] 节点 ID 唯一且稳定；
- [ ] 依赖顺序合理。

### 7.3 叶节点

- [ ] 每个叶节点只判断一件事；
- [ ] 每个叶节点可以判 0 或 1；
- [ ] 每个叶节点有可定位证据；
- [ ] 每个叶节点有合法类别；
- [ ] 专家可以在约 15 分钟内判断；
- [ ] 要求没有不必要地绑定某种实现方式。

### 7.4 权重

- [ ] 权重反映重要性而非难度；
- [ ] 没有重复计分；
- [ ] 核心贡献具有足够有效权重；
- [ ] 零权重节点是有意设置的。

### 7.5 结果

- [ ] 每项结果都能由 `reproduce.sh` 生成证据；
- [ ] 容差或趋势标准已预先确定；
- [ ] 不依赖预先写入的静态结果；
- [ ] 机器可读结果文件格式明确。

### 7.6 审核

- [ ] 至少两名人员检查过 rubric；
- [ ] 领域专家或原作者核验过关键要求；
- [ ] 使用正确、部分完成和错误提交做过校准；
- [ ] Judge 分歧较大的节点已重写；
- [ ] Rubric、addendum 和代码版本已经冻结。

## 8. 常见失败模式

### 8.1 过粗

一个节点同时要求方法、运行和结果全部正确，导致部分完成无法得分。

### 8.2 过细但没有价值

把每个函数、参数或文件都单独计分，却与论文核心贡献关系很弱。

### 8.3 把实现难度当成重要性

某实验很昂贵不代表它在论文中最重要。

### 8.4 只检查 README

提交可以声明实现了某功能，但没有实际代码或执行证据。

### 8.5 要求精确复现随机数值

不同硬件、随机种子和依赖版本可能产生合理偏差，应检查预定义容差或核心趋势。

### 8.6 结果节点没有来源约束

如果 Judge 无法区分预先写入的结果和 `reproduce.sh` 生成的结果，容易被硬编码结果欺骗。

### 8.7 Rubric 泄露任务关键信息

如果候选者正常完成任务所必需的信息只存在于隐藏 rubric 中，评测测到的是猜测能力而不是论文复现能力。必要澄清应写入公开 addendum。

## 9. 最终质量标准

一份高质量 PaperBench 风格 rubric 应满足：

> 每个核心科研主张均被覆盖；每个叶节点单一、可观察、可二元判断；所有子节点合起来足以证明父节点；权重表达科研重要性；实现、执行和结果证据相互独立；论文歧义经过专家确认并记录在 addendum 中。

## 10. 参考实现

- PaperBench 论文：https://arxiv.org/abs/2504.01848
- Rubric 数据结构：`paperbench/rubric/tasks.py`
- Judge 树递归与加权：`paperbench/judge/base.py`
- SimpleJudge 叶节点取证：`paperbench/judge/simple.py`
- 论文数据注册：`paperbench/paper_registry.py`
- 示例 rubrics：`data/papers/*/rubric.json`
