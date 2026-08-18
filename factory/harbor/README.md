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
论文输入：/workspace/paper
提交目录：/workspace/submission
Artifact： /workspace/submission
Verifier： /workspace/submission
```

`task.toml`、`instruction.md`、`tests/test.sh` 和 manifest 会被转换器共同校验，禁止
再次混入 `/home/paper` 或 `/home/submission`。

## Instruction 适配

`instructions.official.txt` 保存官方 PaperBench instructions 原文，并固定校验 SHA-256：

```text
712ed3968de5b8d98b96e25e7d33c95552c460649201743d8535e84c344bac56
```

输出 `instruction.md` 不再声称与官方原文逐字节相同。转换器以该固定原文为输入，只做
确定性 Harbor 协议适配：

1. `/home/paper` 改为 `/workspace/paper`；
2. `/home/submission` 改为 `/workspace/submission`；
3. “最多运行 7 天”改为实际 `reproduce.sh` verifier 时限；
4. 明确要求快速、确定性的 reproduction，避免在 grading 阶段依赖完整训练或大下载。

默认 reproduction 时限为 900 秒，题面会显示 `900 seconds (15 minutes)`。通过
`--reproduction-timeout-sec` 修改时，`task.toml`、`test.sh` 运行环境和题面会同步更新。

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

- `/workspace/paper`、PDF、Markdown 和 addendum 是否存在；
- `/workspace/submission`、`README.md` 和 `reproduce.sh` 是否存在；
- judge key/base URL 是否已注入；
- 实际 reproduction timeout。

Preflight 不输出任何 secret。

### Reproduction

原始 submission 被复制到隔离执行目录，再按
`PAPERBENCH_REPRODUCTION_TIMEOUT_SEC` 运行 `bash reproduce.sh`。默认 900 秒，与题面一致。

### 提交证据收集

Judge 不再按字典序只读取前 80 个文件。现在：

- 忽略 `.git`、`__pycache__` 和常见工具缓存；
- 优先 `README.md`、`reproduce.sh`、依赖清单；
- 其次优先 results/metrics/summary、源码和其他文本；
- 最多记录 200 个文件，并对送入 prompt 的文本设置分层字符预算。

这样核心代码和结果摘要是否可见不再由文件名字典序决定。

### LLM 请求

Paper、submission、executed submission 和 reproduction 日志均使用更小的独立字符预算。
Judge 只发送一次请求；网络超时或其他异常不会无条件重发同一个大请求。默认单请求时限
为 600 秒，可用 `--judge-request-timeout-sec` 调整。

## 单独转换

```bash
python3 factory/harbor/convert_to_harbor.py \
  --root /root/workspace/Task/PaperBench \
  --paper-list factory/paperlist/20260815.json \
  --paper tent \
  --batch-id 20260818-120000 \
  --reproduction-timeout-sec 900 \
  --judge-request-timeout-sec 600
```

默认优先使用已发布的 `paper_sources/<id>/rubric.json` 与 `addendum.md`；不存在时使用
`design/<id>/rubric_authoring/` 中的 draft。正式发布增加 `--require-approved`，禁止
draft 进入 Harbor。

相同 batch ID 默认拒绝覆盖。修复模板或协议后必须生成新 batch ID，不要热修并复用
已经 rollout 的任务目录。`--overwrite` 只用于明确废弃、尚未进入 rollout 的本地批次。

## 自动校验

转换完成前会检查：

- manifest 与任务目录一一对应；
- Harbor 文件集合完整；
- `task.toml` 不含 secret 占位符；
- instruction 只使用 `/workspace` 路径且时限已适配；
- judge 只读取 `JUDGE_LLM_*` 且不发送 temperature；
- test/judge/solution 与版本化模板一致；
- 目录权限为 755，普通文件为 644，可执行脚本为 755；
- resource metadata 与最终文件一致。

任一检查失败时不会发布最终 batch 目录。
