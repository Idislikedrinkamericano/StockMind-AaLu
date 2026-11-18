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

- 带迭代上限、工具重试、LLM/工具/整次运行超时控制的 ReAct 风格工作流；跳过同次运行的重复请求。
- 未恢复的 LLM 异常保留 `error` 状态，同时返回安全的部分或 no-data 报告；已处理的数据
  缺失不会自动使运行失败。
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

### 显式真实 Smoke Test

先安装前述 `real-data` 可选依赖，再通过安全方式将所选 Provider 的
`OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` 导出到 shell 环境。此入口**不加载
`.env`**，也不回退到 Mock 或 Scripted Demo。使用 `STOCKMIND_LLM_PROVIDER`
（`openai` / `anthropic`）和 `STOCKMIND_LLM_MODEL` 选择模型；OpenAI-compatible
服务可设置 `OPENAI_BASE_URL`（或优先级更高的 `OPENAI_API_BASE`）。

```bash
# 仅离线预检：保存 blocked 记录，不发出请求。
python examples/real_smoke.py --case aapl-price
# 确认计划及潜在费用后，显式启用真实请求：
python examples/real_smoke.py --case aapl-price --execute-real
# 自定义案例（要求有来源的价格与历史行情）：
python examples/real_smoke.py --ticker MSFT --query "Research price and trends" \
  --provider yfinance --deadline 60 --output-dir smoke-results
```

省略 `--case` 检查全部四个目标案例；自定义案例也需要 `--execute-real` 才会请求服务。
默认每案例最多 4 次 LLM 调用，每次最多 1,024 输出 token；LLM / Tool / Run
限制分别为 20 / 15 / 60 秒，SDK 和工具均不重试。Token 限制不是金额上限。
结果和安全报告存入 Git 已忽略的 `smoke-results/run-*/`。
退出码：全部 pass 为 0，存在 fail 为 1，无 fail 但存在 blocked 为 2；仅生成报告不算通过。

日线股票路由已通过离线验证：AAPL/MSFT 等美股代码使用 yfinance 或 AkShare/Sina；
支持的沪深 A 股六位代码（如 600519/000001）使用 AkShare/Eastmoney，保留前导零。
A 股财务数据仍不可用。AkShare 1.20.65 使用 `stock_us_hist_sina` /
`stock_zh_a_hist_em`；仅当新函数不存在时才回退到明确的旧名称。
识别限流后停止自动重试，同一 Provider 实例也不再请求该来源；Smoke 保留失败结果。

**真实验证仍待完成：**用户报告已成功通过真实 yfinance 获取 AAPL 五日历史行情，
本阶段没有重复该请求。A 股真实数据在东方财富限流后仍未验收，真实 LLM E2E
仍因缺少凭据而阻塞。P3-C 没有发出真实请求。
详见 [Provider 兼容性与限流](docs/engineering-notes.zh-CN.md#provider-兼容性与限流p3-c)。


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
python examples/evaluate.py --suite benchmark --output-dir benchmark-results
```

使用脚本/Fake Model 和固定合成 fixtures，一次运行 12 个基线案例与 12 个可靠性案例。
不加载 `.env`，不根据 API Key 选择真实服务。生成 `benchmark-results/evaluation-report.md`
及同目录的 `evaluation-results.json`。它们是 **本地生成产物**，已由 Git 忽略，并非
已提交的 GitHub 文档；可用 `--output-dir` 指定其他目录。

已记录离线结果（2026-11-17）：基线完成与 grounding 均 **12/12**，脚本化 macro
P/R/F1 **1.0**；可靠性 **11 pass / 0 fail / 1 unsupported**，安全终止 **10/10**，
恢复 **7/8**，安全违规 **0**。这些是工作流与一致性检查，不代表真实 LLM 性能或
独立金融准确性验证；历史对比明确引用开发记录。原单套件命令保留：省略 `--suite`
运行基线，或使用 `--suite reliability`，均可选用 `--output results.json`。
详见 [Benchmark 用法、指标定义与限制](docs/engineering-notes.zh-CN.md#可复现离线-benchmarkp2-c)。

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
- Checkpoint 评估支持继续执行暂停的内存 graph；重读已到 END 的 timeout checkpoint
  不会重试失败的研究。

## 文档

- [工程说明](docs/engineering-notes.zh-CN.md) — 可靠性、数据完整性、异步/
  checkpoint、评估与 prompt 记录细节。
- [配置模板](.env.example) — 支持的环境变量；请仅在本地保留已填写的 `.env`。
- [示例](examples/) — CLI 与异步/checkpoint 演示。
- [Prompt 记录器](promptlog/README.md) — 独立的 prompt 记录工具。
