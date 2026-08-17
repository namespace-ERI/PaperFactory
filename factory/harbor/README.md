# PaperBench → Harbor converter

该转换器以如下已处理批次为格式参照：

```text
/mnt/shared-storage-user/songdemin/user/yangzhixiong/agent_training/
  tasks_processed/20260807-162700
```

输出完全采用同样的顶层结构：

```text
<batch-id>/
├── manifest.jsonl
└── harbor_task/
    └── <batch-id>-research-paperbench-<6hex>/
        ├── task.toml
        ├── instruction.md
        ├── resource_metadata.json
        ├── environment/paper/
        │   ├── paper.pdf
        │   ├── paper.md
        │   ├── addendum.md
        │   ├── blacklist.txt
        │   └── assets/
        ├── tests/
        │   ├── test.sh
        │   ├── llm_rubric_judge.py
        │   ├── judge_config.json
        │   ├── rubric.json
        │   └── judge.addendum.md
        └── solution/
            ├── reproduce.sh
            └── README.md
```

`test.sh`、`llm_rubric_judge.py` 和 smoke fixture 使用参照 Harbor 批次中的权威模板。`instruction.md` 不使用该批次二次加工的题面，而是逐字节复制官方 PaperBench 的 `paperbench/instructions/instructions.txt`；转换时固定校验其 SHA-256 `712ed396...bac56`，成品校验也会检查字节完全相同。论文标题、rubric、judge config、资源统计、task.toml 和 manifest 按题动态生成。处理后的任务不会包含 `environment/Dockerfile` 或 `tests/Dockerfile`。

单独转换：

```bash
python3 factory/harbor/convert_to_harbor.py \
  --root /root/workspace/Task/PaperBench \
  --paper-list /root/workspace/Task/PaperBench/manifest.json \
  --paper tent \
  --batch-id 20260817-120000
```

默认优先使用已发布的 `paper_sources/<id>/rubric.json` 与 `addendum.md`；不存在时使用 `design/<id>/rubric_authoring/` 中的 draft。最终批次默认输出到 `papers/<日期>/`。正式发布数据集时增加 `--require-approved`，禁止 draft 进入 Harbor：

```bash
python3 factory/harbor/convert_to_harbor.py ... --require-approved
```

如果参照批次不在默认路径，可通过 `--template-task /path/to/reference/harbor_task/<task-id>` 指定具有相同契约的模板题。
