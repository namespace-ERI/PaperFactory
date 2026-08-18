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
        ├── tests/
        └── solution/
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
    │   └── judge_config.json
    └── solution/
        ├── reproduce.sh
        └── README.md
```

模板最初参考 processed Harbor 任务，但不再在运行时读取共享目录，避免外部模板被热修后
相同 Factory 代码产生不同任务。`pipeline_commit` 同时覆盖 converter、instruction 原文
和全部模板文件。

如确需实验其他模板，可显式传 `--template-task`；正式数据生产建议使用仓库内版本。

## 路径契约

生成任务统一使用：

```text
论文输入：/home/paper
提交目录：/home/submission
Artifact： /home/submission
Verifier： /home/submission
```

`task.toml`、`instruction.md`、`tests/test.sh` 和 manifest 会被转换器共同校验，保证
题面和实际挂载、提交、Artifact、Verifier 路径一致。

## Processed Harbor 格式契约

`task.toml` 使用 `schema_version = "1.4"` 和 `construction_format =
"native_rollout_task_v1"`，并包含当前 processed Harbor 公共元数据字段。PaperBench 的
native contract 记为 `paperbench_authored_task_v1`，`native_task_id` 使用论文 ID；因为
`solution/` 只是 verifier smoke fixture 而非论文复现参考答案，
`reference_solution_available = false`。

运行资源在三个位置保持一致：task metadata 的 `gpu_tier = "H200"`、`gpu_count = 1`，
agent 和 verifier environment 的 `gpus = 1`、`gpu_types = ["H200"]`，以及
`resource_metadata.json` 的 `harbor_resource_metadata_v3` resource estimate。即使论文实验
本身 CPU 足够，运行环境仍按题面承诺提供一张 H200。

## Instruction 适配

两种模式分别保存官方 PaperBench instructions 原文并固定校验 SHA-256：

```text
712ed3968de5b8d98b96e25e7d33c95552c460649201743d8535e84c344bac56
instructions.code-dev.official.txt: 65a75977810a1bca53e69767740c07f5c71c6d632838ebd32ba22d69e2a49d9e
```

转换器以该固定原文为输入，输出 `instruction.md` 时只做一处确定性资源适配：

```text
NVIDIA A10 GPU → NVIDIA H200 GPU
```

除 GPU 型号外，路径、任务要求和“最多运行 7 天”等官方文字逐字保持不变。默认
`PAPERBENCH_REPRODUCTION_TIMEOUT_SEC` 为 604800 秒，与官方七天说明一致。

`--rubric-mode code-dev` 使用官方 code-only instruction。该题面明确说明评分时不执行代码，
因此没有 GPU/七天运行段落，也不做 H200 替换；其他内容与官方 code-only 原文逐字一致。

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
- `PAPERBENCH_REPRODUCTION_TIMEOUT_SEC`。

Judge 请求不发送 `temperature`，兼容拒绝该参数的 gpt-5.5 上游 API。

## Verifier 行为

### Preflight

`test.sh` 在 reproduction 前写出 `/logs/verifier/preflight.json`，检查：

- `/home/paper`、PDF、Markdown 和 addendum 是否存在；
- `/home/submission` 和 `README.md` 是否存在；常规模式还要求 `reproduce.sh`；
- `/home/submission` 是否是以自身为仓库根且至少有一个 commit 的 Git 仓库；
- `README.md` 是否已跟踪；常规模式还要求 `reproduce.sh` 已跟踪；
- HEAD 中 committed files 的 blob 总量是否不超过 1,000,000,000 bytes；
- judge key/base URL 是否已注入；
- 实际 reproduction timeout。

Preflight 不输出任何 secret。

### Reproduction

通过 preflight 后，原始 submission 被复制到隔离目录并执行 `git clean -fd`。Judge 只读取
该清理副本；reproduction 则在清理副本的第二份隔离副本中运行，因此未跟踪文件不会参与
评分，运行产生的结果又能与原始提交证据分开。随后按
`PAPERBENCH_REPRODUCTION_TIMEOUT_SEC` 运行 `bash reproduce.sh`。默认 604800 秒，与题面
七天说明一致。

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

### 提交证据收集

Judge 不再按字典序只读取前 80 个文件。现在：

- 忽略 `.git`、`__pycache__` 和常见工具缓存；
- 优先 `README.md`、`reproduce.sh`、依赖清单；
- 其次优先 results/metrics/summary、源码和其他文本；
- 最多记录 200 个文件，并对送入 prompt 的文本设置分层字符预算。

这样核心代码和结果摘要是否可见不再由文件名字典序决定。

### LLM 请求

与官方 PaperBench 的 judge 调度一致，每个 rubric leaf 单独构造 prompt、单独调用一次
LLM；内部节点不调用 LLM，而是在本地按直接子节点的局部权重逐层聚合。请求使用受控并发，
默认最多同时 100 个，可用 `--judge-max-workers` 调整。某个 leaf 网络超时、返回非法 JSON
或遗漏指定 leaf 时，只把该 leaf 记为 0 并留下错误详情，其他成功 leaf 仍参与总分计算。

Paper、submission、executed submission 和 reproduction 日志均使用独立字符预算。每个 leaf
请求不做盲目重试，默认单请求时限为 600 秒，可用 `--judge-request-timeout-sec` 调整。

## 单独转换

```bash
python3 factory/harbor/convert_to_harbor.py \
  --root /root/workspace/Task/PaperBench \
  --paper-list factory/paperlist/20260815.json \
  --paper tent \
  --rubric-mode regular \
  --batch-id 20260818-120000 \
  --reproduction-timeout-sec 604800 \
  --judge-request-timeout-sec 600 \
  --judge-max-workers 100
```

转换 code-dev rubric 时改为 `--rubric-mode code-dev`；转换器会同时校验 rubric 只含
`Code Development` 叶节点、选择官方 code-only instruction，并写入 `code_only = true`。

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
- regular instruction 除 `A10 → H200` 外与官方原文一致；code-dev 与官方 code-only 原文一致；
- rubric、judge config、task metadata 的模式完全一致；
- task、Artifact 和 verifier 统一使用 `/home/paper`、`/home/submission`；
- judge 只读取 `JUDGE_LLM_*` 且不发送 temperature；
- judge 只接受叶节点 0/1，并按 rubric 树递归聚合局部兄弟权重；
- verifier 检查 Git 仓库、提交状态和 1GB 上限，并执行 `git clean -fd`；
- test/judge/solution 与版本化模板一致；
- 目录权限为 755，普通文件为 644，可执行脚本为 755；
- v3 resource metadata 与最终文件、TOML 的 H200 资源契约一致。

任一检查失败时不会发布最终 batch 目录。
