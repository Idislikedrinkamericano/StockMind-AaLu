# StockMind 工程说明

[English](engineering-notes.md) · [项目主页](../README.zh-CN.md)

本页保留面向贡献者的实现细节，避免项目主页变成开发记录。

## 工作流与报告

`StockMindAgent` 运行由 `agent`、`tools` 和 `finalize` 节点组成的异步 LangGraph
工作流。State 会保留请求 ticker、消息、工具调用、观测结果、错误、状态和运行元数据。
工作流限制迭代次数，拒绝 ticker 与单股票请求不一致的工具调用，并将工具失败记录为观测
结果，而不是悄悄当成数据处理。

`app/reporting/assembler.py` 仅从有效观测结果确定性地构建最终报告。工作流可能会尝试
结构化 LLM 输出以用于追踪和校验，但 LLM 候选报告永远不会替换该结果。ticker 错误、不受
观测支持的指标、原始文本，以及没有支持观测的结论都会被拒绝。没有有效观测结果时，报告
会明确说明证据不可用。

## 运行时安全

`AgentConfig` 提供迭代上限以及 LLM、工具和整次运行的时间预算。以 `STOCKMIND_` 开头的
环境变量可配置默认值，详见 [`.env.example`](../.env.example)。deadline 通过运行元数据
传递，因此重试、退避、LLM 调用和新的工具调用都不能超过剩余预算。超时运行会保留已验证
观测结果、记录超时错误，并返回部分数据或无数据报告，而不是编造结论。

`asyncio.CancelledError` 会被重新抛出；取消不会被转化为普通工具失败。

## 数据完整性规则

默认 Provider 为确定性的 mock 数据，并始终是模拟数据。可选的 yfinance 和 AkShare
Provider 是外部、依赖网络的数据源，使用前应针对目标市场和期间进行评估。

- OHLC 缺失、NaN 或非有限值的 AkShare 行会被丢弃；缺失价格绝不会被转为零。Provider
  返回的字面零可保留为原始数据，但没有有效最新收盘价时会返回不可用/错误结果。
- `total_debt` 优先使用明确披露的总债务；否则仅在短期和长期有息负债两项均明确存在时
  才计算。总负债和部分债务数据不会作为替代项。
- MA、RSI、MACD、年化波动率和最大回撤要求收盘价有限且严格大于零。空序列、样本不足、
  零、负数、NaN 和无穷序列会返回不可用结果，不会产生 NaN、无穷或虚构指标。

该计算规则有意比原始数据保存更严格：字面零可保留为源数据，但不适用于价格型指标。

## 异步、Checkpoint 与追踪

公共 API 以异步为优先（`ainvoke`/`arun`），并提供同步包装供简单脚本使用。默认
checkpointer 为内存实现。SQLite checkpoint 可通过
`app.graph.checkpoint.build_checkpointer` 使用。除非配置 `STOCKMIND_TRACE_DIR` 输出
JSONL，否则 trace 仅保存在内存中。

```bash
python examples/async_research.py --ticker NVDA --lang en
```

内置异步示例使用 Scripted Demo 模型；它展示工作流与 checkpoint，而不是实时 LLM。

## 评估与 Prompt 记录

`examples/evaluate.py` 和 `app/evaluation/` 提供本地实验用的评估框架。仓库不发布
benchmark，也不做性能承诺；结果取决于模型、Provider、fixture 和运行配置。

`promptlog/` 是一个独立的本地 prompt 记录工具：

```bash
python -m promptlog add "你的 prompt" --tag finance
python -m promptlog list --limit 20
python -m promptlog export
```

存储与导出说明见 [promptlog/README.md](../promptlog/README.md)。

## 测试

测试套件对 Agent、Provider、超时、报告组装和计算行为使用离线 fixture 与 mock，不需要
真实 LLM、金融 API 或 API Key。

```bash
python -m pytest
```

可选 Provider 的集成行为仍取决于已安装的包、网络可用性和上游响应格式。
