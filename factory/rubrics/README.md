# PaperBench rubric factory

这里实现 [RUBRIC_CREATION_GUIDE_ZH.md](RUBRIC_CREATION_GUIDE_ZH.md) 所要求的分阶段制作流程。模型只生成可审计初稿，不能绕过 gold run、领域专家复核和人工批准。

## 流水线

```text
paper.md + task_metadata.json
  → 分段抽取 claims / methods / experiments / ambiguity
  → contribution_evidence_matrix.json
  → addendum.draft.md
  → rubric_tree_plan.json（顶层科研贡献骨架）
  → rubric_subtrees/*.json（逐分支拆到原子叶节点）
  → rubric_tree_unweighted.json（确定性组装）
  → rubric_weight_plan.json（单独设计局部权重）
  → rubric.draft.json
  → 自动结构检查 + 独立模型审查 + 可选结构修复
  → 按需生成 judge.addendum.draft.md
  → 人工解决 unresolved questions / gold-run 容差 / 权重
  → 显式批准后发布 rubric.json 与 addendum.md
```

初稿全部写到 `design/<paper_id>/rubric_authoring/`；论文中间包位于 `paper_sources/<paper_id>/`。`papers/` 只保存按日期组织的最终 Harbor 批次。

## 生成初稿

脚本只使用 Python 标准库，通过 OpenAI-compatible `POST /v1/chat/completions` 请求 JSON：

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=...
python3 factory/rubrics/create_rubrics.py --paper tent
```

自定义兼容端点、批量论文和模型：

```bash
python3 factory/rubrics/create_rubrics.py \
  --paper tent --paper adalora \
  --model your-model \
  --base-url https://example.internal/v1
```

重要参数：

- `--target-leaves 40-120`：只是粒度目标，不要求机械凑数。
- `--chunk-chars 50000 --workers 3`：论文分块和并发抽取设置。
- `--repair-rounds 1`：只修自动检查或独立审查明确指出的问题。
- `--overwrite`：覆盖 `design/` 中已有初稿；使用前先保留人工编辑。
- `--mock-responses-dir DIR`：从 `<paper-id>.<stage>.json` 读取离线响应，便于测试和复核；stage 如 `elements-001`、`matrix`、`addendum`、`tree-plan`、`subtree-<branch-id>`、`weighting`、`review`、`repair-01`、`judge-addendum`。

## 人工审核

至少核对：

1. `contribution_evidence_matrix.json` 是否覆盖主文贡献，论文定位是否真实；
2. `unresolved_questions.json` 中的范围、资源、容差问题是否由专家或 gold run 解决；
3. `rubric.draft.json` 是否原子、可观察、没有重复计分或实现绑定；
4. `quality_review.json` 的 blocking issues 是否均已处理；
5. `validation_report.json` 中有效权重是否合理。

人工修改 rubric 后重新校验：

```bash
python3 factory/rubrics/validate_rubric.py \
  design/tent/rubric_authoring/rubric.draft.json \
  --report design/tent/rubric_authoring/validation_report.manual.json
```

解决问题后，将 `unresolved_questions.json` 置为 `[]`，并在复核后清空 `quality_review.json` 的 `blocking_issues`。警告允许由人工判断，但会记录在批准 provenance 中。

## 发布

发布需要稳定的人工审核者标识；存在结构错误、addendum TODO、未解决问题或 review blocker 时脚本拒绝发布：

```bash
python3 factory/rubrics/publish_rubric.py \
  --paper tent \
  --approved-by reviewer-name \
  --notes "gold run and independent review completed"
```

发布后完整包检查：

```bash
python3 factory/rubrics/validate_rubric.py --paper tent --packages
```

已有正式文件不会被静默覆盖；确需更新时必须显式使用 `--replace`，并会生成新的 `human_approval.json` 和内容哈希。
