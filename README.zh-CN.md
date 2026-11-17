# StockMind

[English](README.md)

StockMind 是一个基于 **LangChain**、**LangGraph** 和 **ReAct** 模式的股票研究
Agent。它将 LLM 推理与数据获取、确定性金融计算分离，并基于带来源信息的观测结果生成
结构化研究报告。

> StockMind 是工程项目和研究辅助工具，不构成投资建议。

## 项目概览

StockMind 通过显式的 LangGraph 工作流实现 Agent 循环，而不是将其隐藏在预构建抽象之后。
因此 State、工具观测、执行上限和失败信息均保持可见且可测试。

最终报告中的事实与指标只能由有效工具观测组装而来。Finalize 直接使用确定性组装器，
不再额外调用 LLM；研究阶段仍由 LLM 推理和选择工具。LLM 生成的文本或仅通过 schema
校验的 JSON 本身不被视为证据。

## 功能

- 带迭代上限、工具重试、LLM/工具/整次运行超时控制的 ReAct 风格工作流。
- 市场数据、财务报表、新闻、技术分析和估值工具。
- MA、RSI、MACD、波动率、回撤、P/E、EV/EBITDA 和 FCF 收益率的确定性计算；DCF 情景
  估值要求现金流性质和股数口径明确。
- 英文或简体中文的结构化 `StockResearchReport` 输出。
- 显式 State、追踪、可选内存或 SQLite checkpoint，以及可重复的离线脚本化演示。
- 默认确定性的 **mock 数据**，并明确标注为模拟数据；可选 yfinance 与 AkShare
  数据源用于真实数据实验。

## 架构

```text
用户请求
    │
    ▼
LangGraph ReAct 工作流 ──► LLM 推理 / 工具选择
    │                                  │
    │                                  ▼
    └────────────────────────── 工具与数据源
                                       │
                                       ▼
                                  已验证观测结果
                                       │
                                       ▼
                           确定性报告组装器
                                       │
                                       ▼
                          StockResearchReport
```

| 组件 | 职责 |
| --- | --- |
| `app/agents` 与 `app/models` | Agent 门面、提示词、LLM Provider、脚本化演示模型 |
| `app/graph` | LangGraph State、路由、deadline、重试与终止 |
| `app/tools` | Provider 访问与确定性计算 |
| `app/reporting` | 基于证据的报告组装与安全的无数据输出 |
| `app/schemas` | 报告与观测结果的 Pydantic 契约 |
| `app/observability` | 追踪与成本观测工具 |

LLM 负责解读可用观测结果和请求工具，不负责金融计算。工具失败会表示为数据不可用，
不会被替换成虚构的数值。

## 快速开始

需要 Python 3.11 或更新版本。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,sqlite]"
cp .env.example .env
```

该命令安装核心包、测试工具和完整测试套件所需的 SQLite checkpoint 支持。仅使用核心
功能可执行 `pip install -e .`。可选组为 `dev`（测试）、`sqlite`（持久化 checkpoint）、
`web`（Gradio）和 `real-data`（金融数据源），均在 `pyproject.toml` 中声明。

默认数据源是 `mock`。它是确定性的模拟数据，并非实时行情。如需启用可选的真实数据源，
安装其依赖并在本地 `.env` 中设置数据源：

```bash
pip install -e ".[real-data]"
# STOCKMIND_DATA_PROVIDER=yfinance  # 或 akshare
```

如需使用真实 LLM Agent，请在 `.env` 中将 `STOCKMIND_LLM_PROVIDER` 设为 `openai`
或 `anthropic`，配置对应 Provider 的 API Key 和匹配的 `STOCKMIND_LLM_MODEL`。
Provider 可能收费。请勿提交 `.env`；Git 已忽略它。

运行离线测试套件：

```bash
python -m pytest
```

## 使用示例

### Scripted Demo（未配置 API Key 时的默认模式）

```bash
python examples/research.py --tickers NVDA,AMD,TSM --lang zh
python examples/nvda_research.py --lang en
```

未配置 LLM API Key 时，上述命令使用 `ScriptedReActDemoModel`。它会针对传入的
ticker 执行一段**固定顺序**的工具调用，以便离线演示工作流；它不是自主 Agent，也不会
依据请求动态选择工具。其默认结果使用模拟 mock 数据。

### 真实 LLM Agent

```bash
python examples/research.py --tickers AAPL --lang en
```

当 OpenAI 或 Anthropic API Key 可用时，`research.py` 会使用真实聊天模型创建
`StockMindAgent`。模型可以在工作流的迭代和时间限制内请求已注册工具。除非更改
`STOCKMIND_DATA_PROVIDER`，数据仍默认为 mock。

### 异步 Checkpoint 演示

```bash
python examples/async_research.py --ticker AMD --lang en
```

需要 `sqlite` 可选依赖。两次调用各自使用全新的 Scripted Demo 序列和显式 Mock 工具，
共享一个 SQLite thread。它保留 checkpoint 消息，不代表自主追问推理或中断任务恢复。

### 脚本化 Web 演示

```bash
pip install -e ".[web]"
python app.py
```

打开 `http://127.0.0.1:7860`。Web UI 使用固定工具序列，数据默认为 Mock。页面没有
真实 LLM 模式切换入口，设置 API Key 也不会启用；真实 LLM Agent 请使用上面的研究 CLI。
需要 requirements 文件的平台可在仓库根目录执行 `pip install -r requirements.txt`；
该文件统一引用 `.[web]`。

### 离线评估

```bash
python examples/evaluate.py --output /tmp/stockmind-evaluation.json
```

使用固定合成 fixtures 和脚本化 LLM 响应运行 12 个版本化案例，不随 API Key 或 Provider
配置切换模式。JSON 包含工具集合 Precision / Recall / F1、规则化任务完成检查、报告与
观测一致性及执行指标。这些是回归检查，不代表真实 LLM 性能或独立金融准确性验证。
详见[指标定义与限制](docs/engineering-notes.zh-CN.md#评估与-prompt-记录)。

## 技术栈

- Python 3.11+
- LangChain Core 与 LangGraph
- Pydantic
- pandas、NumPy 与 matplotlib
- OpenAI 或 Anthropic 集成（可选）
- yfinance 或 AkShare 数据源（可选）
- pytest

## 当前限制

- 默认运行使用模拟 mock 数据；外部数据源是可选项，依赖网络并受其覆盖范围和可用性影响。
- Scripted Demo 有意采用固定脚本，不应被解读为 LLM 的自主工具选择能力。
- 真实 LLM 输出受超时与已验证观测结果约束，但其定性解读并非投资建议。
- Provider 数据缺失、非有限或不完整时，报告的相应部分可能不可用。StockMind 不会合成
  缺失的市场价格、债务或技术指标。
- 估值会披露各输入的来源与期间，包括 unknown 或 mixed-period。当前 Provider 无法
  确认 FCFF/FCFE，因此其 DCF 不可用；年度 EPS 回退不会被标成 TTM。
- 本项目不提供交易执行、投资组合优化或历史回测。

## 文档

- [工程说明](docs/engineering-notes.zh-CN.md) — 可靠性、数据完整性、异步/
  checkpoint、评估与 prompt 记录细节。
- [配置模板](.env.example) — 支持的环境变量；请仅在本地保留已填写的 `.env`。
- [示例](examples/) — CLI 与异步/checkpoint 演示。
- [Prompt 记录器](promptlog/README.md) — 独立的 prompt 记录工具。
