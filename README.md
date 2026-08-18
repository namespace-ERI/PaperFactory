# PaperFactory

中文文档入口：

- [完整 Factory 管线](factory/README.md)
- [Rubric 生成中文说明](factory/rubrics/README.zh-CN.md)
- [Rubric 制作规范](factory/rubrics/RUBRIC_CREATION_GUIDE_ZH.md)

PaperFactory 将已经选好的论文列表依次转换为：

1. PaperBench 论文 task 输入包；
2. 具有完整中间轨迹的显式树状 rubric 和 addendum；
3. 与 processed Harbor 数据对齐的最终任务批次。

总入口严格按 `task → rubric → Harbor` 顺序运行：

```bash
python3 factory/build_paperbench.py \
  --paper-list factory/paperlist/20260815.json \
  --model your-model \
  --base-url https://your-openai-compatible-endpoint/v1 \
  --task-workers 4 \
  --paper-workers 4 \
  --workers 2 \
  --batch-id 20260817-120000
```

完整的输入格式、目录契约、URL-only 支持、并发与模型切换、rubric 显式构树流程、
人工发布门槛、Harbor 转换和当前批次统计，见
[factory/README.md](factory/README.md)。Rubric 子流水线的独立说明见
[factory/rubrics/README.zh-CN.md](factory/rubrics/README.zh-CN.md)。

## 安装与测试

```bash
python3 -m pip install -r factory/requirements.txt
python3 -B -m unittest discover -s factory/tests -v
```

模型密钥只通过环境变量（默认 `OPENAI_API_KEY`）传入，不能提交到仓库。

## 仓库边界

本仓库跟踪 factory 代码、指南、测试和示例 paper list。以下内容是运行时生成或本机
依赖，不提交 Git：

- `paper_sources/`：下载的论文 PDF、提取文本和公开 task 资源；
- `design/`：rubric 模型输出、审查记录和作者侧 provenance；
- `papers/`：最终 Harbor 批次；
- `environment/`：本机镜像或环境归档；
- API 密钥、虚拟环境、缓存和日志。

生成的 Harbor 批次可以单独上传到数据存储；正式发布必须使用人工批准后的 rubric，
并在转换时启用 `--require-approved`。
