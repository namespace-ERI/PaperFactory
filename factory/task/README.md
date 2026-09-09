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
  "asset_files": ["figure-1.png", "figures/main-result.jpg"],
  "official_repo": "https://github.com/author/repository",
  "planned_scope": "...",
  "primary_artifacts": ["..."],
  "datasets": []
}
```

- `pdf_path` 可替换为 `paper_pdf` 或 `pdf_url`。
- `markdown_path` 可替换为 `paper_md`、`markdown_url` 或 `md_url`；若均未提供，使用 `pdftotext -layout` 从 PDF 生成可搜索文本。
- 官方 PaperBench 的 `paper.md` 会用 `assets/asset_N.*` 引用论文图。对于仅提供 arXiv URL
  的条目，Factory 从论文 HTML 正文的语义 `<figure>` 中按出现顺序提取 `img`/image
  `object`，同步生成 Markdown 引用与 assets；网页 logo、导航图片等非 figure 资源不会进入
  task。该过程不设置文件数、尺寸或关键词阈值。
- 若数据作者已经整理好本地资源，可用 `assets_path`/`paper_assets` 配合显式
  `asset_files` 覆盖自动路径；只复制列出的相对路径，未选择的本地图片引用会从生成的
  `paper.md` 删除。非 arXiv URL 且没有可用 Markdown/HTML figure 时仍会保留空 assets。
- Factory 不按文件数、尺寸或关键词猜测图片重要性。多面板图应在输入侧整理为一个逻辑
  figure 文件，再把该文件列入 `asset_files`，与官方成品的一图一资源粒度一致。
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

已有 package 默认复用，因此修改 `asset_files` 或 URL-only figure 生成逻辑后若还需要同步重写
`paper_sources/<id>/paper.md` 和 assets，请使用 `--force`。最终 Harbor 转换无论是否重建
source package，都会再次按当前 paperlist 的 `asset_files` 执行选择，防止旧的宽泛抓取泄漏。

产物包括 `paper_sources/<paper_id>/{config.yaml,paper.pdf,paper.md,assets/,blacklist.txt}`、仅供数据作者使用的 `design/<paper_id>/{task_metadata.json,source_provenance.json}`，以及 `splits/<collection_id>.txt`。`papers/` 保留给按日期组织的最终 Harbor 批次。物化或复用 paper package 时会把目录规范为 `0755`、文件规范为 `0644`，防止本地来源的 `0600` 权限导致后续 Harbor 转换在不同 UID 下失败。

安全边界：该脚本不会 clone、下载或读取 `official_repo`，只会把它登记到 `blacklist.txt`。

若要按正确顺序一次完成 task、rubric 和最终 Harbor 格式，请使用上级目录的
`python3 factory/build_paperbench.py ...`。大批量时增加 `--stream-papers`，即可并行制作
rubric，并由单一协调器将完成事件串行导出为 Harbor task；`--asset-workers 4` 只并发下载该论文已经按同一规则选出的语义
figure，不改变图片选择范围，也不增加文件、字符或大小截断。
