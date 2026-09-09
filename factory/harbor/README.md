# PaperBench → Harbor 转换器

`convert_to_harbor.py` 把已经完成 task 和 rubric authoring 的论文转换为 processed
Harbor 任务：

```text
<batch-id>/
├── manifest.jsonl
└── harbor_task/
    └── <batch-id>-research-paperbench-<6hex>/
        ├── task.toml
        ├── instruction.md
        ├── resource_metadata.json
        ├── environment/paper/
        └── tests/
```

## 版本化模板

转换器默认使用仓库内固定模板：

```text
factory/harbor/templates/
├── instructions.official.txt
├── instructions.code-dev.official.txt
└── processed_task/
    ├── tests/
    │   ├── test.sh
    │   ├── llm_rubric_judge.py
    │   ├── judge_config.json
    │   └── paper/              # verifier-only immutable text/PDF copy (no assets)
```

模板最初参考 processed Harbor 任务，但不再在运行时读取共享目录，避免外部模板被热修后
相同 Factory 代码产生不同任务。`pipeline_commit` 同时覆盖 converter、instruction 原文
和全部模板文件。

如确需实验其他模板，可显式传 `--template-task`；正式数据生产建议使用仓库内版本。

## 路径契约

生成任务统一使用：

```text
论文输入：/workspace/paper
提交目录：/workspace/submission
Artifacts：/workspace/submission、/logs/agent/trajectory.json
Verifier 输入：/workspace/submission；论文 ground truth：/tests/paper
```

`task.toml`、`instruction.md`、`tests/test.sh` 和 manifest 会被转换器共同校验，保证
题面和实际挂载、提交、Artifact、Verifier 路径一致。

## Processed Harbor 格式契约

`task.toml` 使用 `schema_version = "1.4"` 和 `construction_format =
"native_rollout_task_v1"`，并包含当前 processed Harbor 公共元数据字段。PaperBench 的
native contract 记为 `paperbench_authored_task_v1`，`native_task_id` 使用论文 ID。
Factory 没有论文的参考复现，因此设置 `reference_solution_available = false`，并按 Harbor
规范省略可选的 `solution/`；不会输出一个缺少 `solve.sh` 的伪 solution 目录。

运行资源在三个位置保持一致：task metadata 的 `gpu_tier = "H200"`、`gpu_count = 1`，
agent 和 verifier environment 的 `gpus = 1`、`gpu_types = ["H200"]`，以及
`resource_metadata.json` 的 `harbor_resource_metadata_v3` resource estimate。即使论文实验
本身 CPU 足够，运行环境仍按当前任务设定提供一张 H200。

## Instruction 适配

两种模式分别使用官方 PaperBench full 与 Code-Dev instructions 原文，并固定校验 SHA-256：

```text
712ed3968de5b8d98b96e25e7d33c95552c460649201743d8535e84c344bac56
65a75977810a1bca53e69767740c07f5c71c6d632838ebd32ba22d69e2a49d9e
```

转换器以该固定原文为输入，输出 `instruction.md` 时只做以下确定性运行环境适配：

```text
NVIDIA A10 GPU → NVIDIA H200 GPU（full 原文中的 reproduction 环境）
/home/paper → /workspace/paper
/home/submission → /workspace/submission
```

两种模式都追加官方 BasicAgent 的 `ADDITIONAL NOTES`，声明当前可用一张 H200、工作时间为
12 小时、API key 路径为 `/workspace/agent.env`。Full 模式保留官方 `reproduce.sh` 七天上限；
Code-Dev 使用官方 code-only instruction，明确代码不会在评分时执行，也不要求 `reproduce.sh`。
除官方动态 notes、GPU 型号和 Harbor 工作目录路径外，任务要求保持不变。默认
`PAPERBENCH_REPRODUCTION_TIMEOUT_SEC` 为 604800 秒，与官方七天说明一致。

`--rubric-mode code-dev` 会同时选择官方 Code-Dev instruction、从完整 rubric 确定性剪枝出
Code Development 视图、跳过 `reproduce.sh`，且 judge 只根据提交的代码证据评分。

## Judge 环境变量

`task.toml` 不包含任何密钥或 URL 模板，因此不会出现：

```text
LLM_API_KEY = "${LLM_API_KEY}"
LLM_BASE_URL = "${LLM_BASE_URL}"
```

Harbor 应在 verifier 运行时安全注入：

```text
JUDGE_LLM_API_KEY
JUDGE_LLM_BASE_URL
```

`llm_rubric_judge.py` 只读取这两个名称。非秘密设置仍写在 `[verifier.env]`：

- `PAPERBENCH_JUDGE_MODEL`；
- `PAPERBENCH_JUDGE_TIMEOUT_SEC`；
- `PAPERBENCH_JUDGE_CONTEXT_WINDOW_TOKENS`；
- `PAPERBENCH_REPRODUCTION_TIMEOUT_SEC`。

Judge 请求不发送 `temperature`，兼容拒绝该参数的上游 API。
官方 source id 为 `pinn` 时，`judge_config.json` 还会设置 `max_prior_nodes = 5`，与官方
`create_judge.py` 对超大 rubric 的特例一致。

`task.toml` 把 `/workspace/submission` 和 `/logs/agent/trajectory.json` 声明为 Harbor artifacts。
轨迹 artifact 是 Harbor 1.4 向 separate verifier 传递 ATIF 的标准方式，用于执行官方
PaperBench blacklist monitor；它不是 agent 的交付路径。论文 ground truth 不从 agent
容器回传：转换器在隐藏的 `tests/paper/` 保存 `paper.pdf`、`paper.md`、`addendum.md` 和
`blacklist.txt` 的同哈希快照，verifier 从 `/tests/paper` 读取，避免 agent 修改
`/workspace/paper` 后污染判分。对于 URL-only 条目，转换器复制 task 阶段生成且由
`paper.md` 实际引用的语义 figure assets；对于带 `asset_files` 的条目，则复制数据作者
明确选择的文件并移除未选择的本地图片标签。两条路径都不会带入网页非 figure 图片，也不
使用图片数量、尺寸或文件名启发式。
官方 judge 不读取图片 assets，因此选中的 assets 只在 agent 可见的
`environment/paper/assets/` 保留一份，不复制到隐藏快照。

## Verifier 行为

### Preflight

`test.sh` 在 reproduction 前写出 `/logs/verifier/preflight.json`，检查：

- verifier 隐藏的 `/tests/paper`、PDF、Markdown 和 addendum 是否存在；
- `/workspace/submission`、`README.md` 和 `reproduce.sh` 是否存在；
- `/workspace/submission` 是否是以自身为仓库根且至少有一个 commit 的 Git 仓库；
- `README.md` 和 `reproduce.sh` 是否已跟踪；
- HEAD 中 committed files 的 blob 总量是否严格小于 1,000,000,000 bytes；
- judge key/base URL 是否已注入；
- 实际 reproduction timeout。

Preflight 不输出任何 secret。

### Reproduction

Reproduction 之前先按官方 `BasicMonitor` 规则检查 ATIF 轨迹中的真实 tool-call 参数：只有
`git clone`、`curl` 或 `wget` 调用同时命中 `blacklist.txt` 条目才构成违规。命中后直接判零，
不运行 reproduction，也不调用 LLM judge。轨迹不存在时与官方找不到 agent log 的行为一致：
记录 monitor 未运行证据，但不凭空判违规。

通过 preflight 后，原始 submission 被复制到隔离目录并执行题面承诺的 `git clean -fd`。
Code-Dev 的源代码证据直接从 Git HEAD 枚举，因此未跟踪和被忽略的文件不会进入判分上下文；
regular 模式按官方 `SimpleJudge` 检查 reproduction 后的完整工作树。reproduction 在清理副本
的第二份隔离副本中运行，先移除复制后失效的 `venv/.venv`，写入
`reproduce.log.creation_time`，再把 stdout/stderr 的合并顺序流保存到 `reproduce.log`。
Result Analysis 以该官方时间戳判断文件是否由本次运行创建或修改。默认运行时限 604800 秒，
与题面七天说明一致。

Code-dev 模式不运行 `reproduce.sh`，直接使用经过 Git 校验和 `git clean -fd` 的 committed
代码副本评分；即使提交中存在 `reproduce.sh` 也不会执行，`reproduction_ran` 固定为 0，
reproduction exit 状态写为 `not_run`。

### Rubric 评分

LLM 对每个叶节点只能返回 0 或 1；缺失、重复、分数值非法的叶节点按 0 处理并记录为
invalid。最终分数与官方 PaperBench 一样，从叶到根逐层计算：每个内部节点只用其直接
子节点的局部权重归一化聚合。不会把整棵树的叶节点摊平后直接平均。

Code-dev 转换会对完整 regular rubric 应用与官方 `TaskNode.code_only()` 相同的确定性剪枝：
保留 Code Development 叶节点及其祖先和原局部权重，删除其他叶节点和空祖先。Judge 只
接收剪枝后的叶节点和清理后的 committed source-code 证据；prompt 不包含 executed
submission、reproduction log 或结果摘要。评分时剩余兄弟逐层重新归一化。

转换前会拒绝任何仍带有 Code Execution / Result Analysis 类别的 Code-dev 叶节点。除此
之外不对存活的 Code Development 叶节点添加文件名或交付形式方面的非官方过滤规则。

### 提交证据收集

证据选择移植官方 `SimpleJudge` 的两阶段流程：先按官方目录黑名单和三类扩展名白名单
构造完整目录树，再用官方 file-ranking prompt 为每个 rubric leaf 选择最多 10 个相关文件，
最后读取这些文件供正式判分。不存在按文件名字典序截取前 80/200 个文件、按
results/metrics 关键词抢占预算、每文件 3500 字符或总计 60000 字符等自定义规则。

Code-dev 不运行 reproduction，文件候选严格从清理后 Git `HEAD` 枚举，因此 `.gitignore`
忽略的数据、logits、checkpoints、生成结果和其他未提交文件不会参与目录树或消耗选择预算。
regular 模式则与官方流程一样，正式判分查看运行 `reproduce.sh` 后的 submission 文件树。

### LLM 请求

与官方 PaperBench 一样，每个 rubric leaf 依次经过文件排序、官方三段式判分和官方二元
分数解析；正式判分 prompt 不附加 Harbor 自定义评分政策或 JSON 指令。JSON 只用于解析结果
和 Harbor 落盘。内部节点不调用 judge，而是在本地按直接子节点的局部权重逐层聚合。
叶节点并发默认最多 100 个，可用 `--judge-max-workers` 调整；单次上游请求默认时限 600 秒。
网络或解析失败只影响对应叶节点，并在详情中记录。
文件内容按官方模型上下文预算截取，不再使用固定文件数/单文件 3500 字符/总计 60000 字符
之类的自定义三层上限。默认上下文为 400000 tokens（与官方 gpt-5 配置一致），可用
`--judge-context-window-tokens` 对齐实际部署模型；若镜像提供 `tiktoken`，使用模型 tokenizer，
否则使用保守的 UTF-8 字节上界保证请求不超预算。
详情同时区分发送给上游的 `requested_model` 与上游响应自报的 `reported_model`；后者仍是
兼容 API 的自报字段，不能被当作对路由后真实模型身份的独立证明。

## 单独转换

```bash
python3 factory/harbor/convert_to_harbor.py \
  --root /root/workspace/Task/PaperBench \
  --paper-list factory/paperlist/20260815.json \
  --paper tent \
  --rubric-mode regular \
  --batch-id 20260818-120000 \
  --agent-timeout-sec 43200 \
  --verifier-timeout-sec 609000 \
  --reproduction-timeout-sec 604800 \
  --judge-request-timeout-sec 600 \
  --judge-max-workers 100 \
  --judge-context-window-tokens 400000
```

转换 code-dev rubric 时改为 `--rubric-mode code-dev`；转换器会同时校验 rubric 只含
`Code Development` 叶节点、使用官方 Code-Dev instruction，并写入 `code_only = true`。

默认优先使用与 `--rubric-mode` 一致的已发布 `rubric.json` 与 `addendum.md`；没有同模式
发布版本时使用 `design/<id>/rubric_authoring/` 中的同模式 draft。正式发布增加
`--require-approved`，禁止 draft 进入 Harbor。

相同 batch ID 默认拒绝覆盖。修复模板或协议后必须生成新 batch ID，不要热修并复用
已经 rollout 的任务目录。`--overwrite` 只用于明确废弃、尚未进入 rollout 的本地批次。

## 自动校验

转换完成前会检查：

- manifest 与任务目录一一对应；
- Harbor 文件集合完整；
- `task.toml` 不含 secret 占位符；
- `task.toml` 的 processed Harbor 公共字段齐全且 agent/verifier 均声明一张 H200；
- regular 与 code-dev 分别以官方 full/code-only instruction 为基线，只做官方动态 notes、
  `A10 → H200`（full）和 `/home → /workspace` 运行环境适配；
- rubric、judge config、task metadata 的模式完全一致；
- judge addendum 不得要求对单个普通叶节点给部分/小数分；
- agent 题面/挂载统一使用 `/workspace/paper`、`/workspace/submission`，verifier 从隐藏的
  `/tests/paper` 读取同哈希 ground truth，并从 `/workspace/submission` 建立隔离评分副本；
- ATIF trajectory 作为显式 artifact 传给 separate verifier，并在 reproduction 前执行 blacklist monitor；
- judge 只读取 `JUDGE_LLM_*` 且不发送 temperature；
- judge 只接受叶节点 0/1，并按 rubric 树递归聚合局部兄弟权重；
- verifier 检查 Git 仓库、提交状态和 1GB 上限，并执行 `git clean -fd`；
- test/judge 与版本化模板一致，且无伪 solution 目录；
- 目录权限为 755，普通文件为 644，可执行脚本为 755；
- v3 resource metadata 与最终文件、TOML 的 H200 资源契约一致。

任一检查失败时不会发布最终 batch 目录。
