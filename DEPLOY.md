# StockMind Web 安装与运行

Web UI 是固定工具序列的 Scripted Demo，默认使用 Mock 模拟数据。页面没有真实 LLM
模式入口；添加 API Key 不会切换模式。管理员更改数据 Provider 时，以报告中的来源为准。

## 本地安装

在完整仓库根目录、已激活的 Python 3.11+ 虚拟环境中执行：

```bash
pip install -e ".[web]"
python app.py
```

然后打开 `http://127.0.0.1:7860`。根目录 `app.py` 使用 `0.0.0.0:7860` 启动
`web/app.py` 中的 Gradio 界面。现有布局包含进度、报告、关键指标与图表，队列配置为
`max_size=8`；该值不是吞吐量或并发性能承诺。

## requirements 兼容入口

需要 `requirements.txt` 的部署环境可在仓库根目录执行：

```bash
pip install -r requirements.txt
```

文件引用 `.[web]`，实际依赖版本统一维护在 `pyproject.toml`。必须提供完整项目，不能仅
上传 requirements 文件。可选真实数据 Provider 另需安装 `.[real-data]` 并配置数据源。

## 真实 LLM 与验证范围

真实 LLM Agent 使用 `python examples/research.py`，需要配置对应 Provider 的 API Key、
`STOCKMIND_LLM_PROVIDER` 和匹配的模型，可能产生 Provider 费用；参见中英文 README。
Web 本身仍为 Scripted Demo。

Cleanup-B 验证环境缺少 Gradio，因此仅验证了 Web 文案和语法，未启动浏览器或服务。
外部托管平台的构建、运行和部署流程需单独验证；本文件不表示项目已部署。
