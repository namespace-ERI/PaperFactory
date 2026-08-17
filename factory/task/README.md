# PaperBench task factory

该目录把“已经选好的 paper list”转换为 PaperBench 兼容的论文输入包。它只处理论文与任务元数据，不生成 rubric。

## 输入

paper list 可以是 JSON 数组，也可以是含 `papers` 数组的 manifest。每篇论文至少需要：

```json
{
  "id": "example-paper",
  "title": "Full Paper Title",
  "pdf_path": "sources/example.pdf",
  "markdown_path": "sources/example.md",
  "assets_path": "sources/example-assets",
  "official_repo": "https://github.com/author/repository",
  "planned_scope": "...",
  "primary_artifacts": ["..."],
  "datasets": []
}
```

- `pdf_path` 可替换为 `paper_pdf` 或 `pdf_url`。
- `markdown_path` 可替换为 `paper_md`、`markdown_url` 或 `md_url`；若均未提供，使用 `pdftotext -layout` 从 PDF 生成可搜索文本。
- `blacklist` 可为字符串或字符串数组；未提供时使用 `official_repo`。
- 相对路径默认相对于 paper list 所在目录，可用 `--source-root` 改写。

## 使用

对当前 Top-10 manifest 构建（已有文件默认跳过）：

```bash
python3 factory/task/build_tasks.py
```

构建新的 paper list：

```bash
python3 factory/task/build_tasks.py \
  --paper-list /path/to/paperlist.json \
  --output-root /path/to/PaperBench \
  --offline
```

只构建一篇并覆盖现有输入：

```bash
python3 factory/task/build_tasks.py --paper tent --force
```

产物包括 `paper_sources/<paper_id>/{config.yaml,paper.pdf,paper.md,assets/,blacklist.txt}`、仅供数据作者使用的 `design/<paper_id>/{task_metadata.json,source_provenance.json}`，以及 `splits/<collection_id>.txt`。`papers/` 保留给按日期组织的最终 Harbor 批次。

安全边界：该脚本不会 clone、下载或读取 `official_repo`，只会把它登记到 `blacklist.txt`。

若要按正确顺序一次完成 task、rubric 和最终 Harbor 格式，请使用上级目录的
`python3 factory/build_paperbench.py ...`；该总入口会先验证所有 task 产物，再启动 rubric 树构造，最后生成 processed Harbor 批次。
