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

## 估值口径与财务期间

### DCF 范围与假设

现有两阶段 DCF 现在要求明确的 `cash_flow_basis`：

| 口径 | 折现率假设 | 价值与每股换算 |
| --- | --- | --- |
| FCFF | WACC | 折现得到企业价值；股权价值 = 企业价值 − 净债务；再除以流通在外股数 |
| FCFE | 股权资本成本 | 折现直接得到股权价值；除以流通在外股数，不再调整债务 |
| Unknown | 不可用 | 不生成估值 |

当前所有内置 Provider（包括 mock）均未确认 FCF 性质。仅有已报告 FCF 或经营现金流减
资本支出，并不能证明其为 FCFF 或 FCFE。因此这些 Provider 的 DCF 工具返回
`value: null`、`status: unavailable` 及原因。明确口径的情景由离线 fixture 验证；这不代表
真实 Provider 已能提供严格 DCF 所需的完整输入。

工具要求年度或 TTM 现金流，以及明确披露的流通在外股数（fixture 可使用明确标注的模拟
股数）。未知、市值/价格反推和加权平均股数均不被接受。FCFF 还要求债务和现金均有数据，
且已披露日期与现金流期末一致。缺失项不会被假定为零；净现金（负净债务）允许使用。
FCFE 不请求也不扣减净债务。

计算边界要求现金流与股数有限且为正，折现率为正且严格高于永续增长率，预测/永续增长率
大于 −100%，预测年数为 1–20 的整数。非有限输入、溢出和非正股权价值均返回不可用。
增长率和折现率仍为用户假设，包括默认的 8% 增长率、10% 折现率、3% 永续增长率和五年
预测期。即便成功返回，也会标为简化情景；尚未全面处理其他权益请求权、币种/单位统一及
历史时点的数据可得性。

### 期间与来源

财务记录增补 `period`、`period_end`、`published_at`、`source_updated_at` 及逐字段的
`field_metadata`。缺失元数据为 null 或 `unknown`；工具来源信息将缺失的发布/更新时间
显示为 `unavailable`。`timestamp` 仍表示获取时间，不替代财报期间或发布日期。

- AkShare 保留东方财富所选年报的 `REPORT_DATE`，以及明确存在且无歧义的
  `NOTICE_DATE`。资产负债表为时点数据（`instant`），利润表和现金流量表为年度数据；
  财务来源更正为 `akshare/eastmoney`。百度市值日期及反推 EPS/股数的输入日期与新浪
  行情分别保留。
- yfinance 通用 `info` 财务数据不再被整体标记为 TTM；明确的 trailing EPS 字段保留
  TTM 语义，未指明的期间和日期维持 unknown。Mock 利润表/现金流为模拟 TTM，不伪造
  财报期末或发布日期。
- `get_eps` 与 P/E 共用按缺失值回退的规则：零 EPS 会被保留，不会替换成正值。回退到
  利润表时，使用该表的来源、获取时间和期间。新增 `eps` 字段保存选中值；原有
  `eps_ttm` 只有期间确认为 TTM 时才有值，否则为 null。Revenue 描述也不再把年度
  数据称为 TTM。
- 估值保留原有 metric/value/inputs 契约，增补 `input_provenance`、`data_period`、
  `period_status` 和可用状态。期间类型/期末未知时标 unknown；已知期间或期末不一致
  时标 mixed-period。当前市值与年度盈利的比值可以有参考意义，但必须披露口径，不能
  冒充同步 TTM。不会合成 TTM 序列。这些标签不验证历史发布可得性，也不能排除所有
  未来数据泄漏。
- EV/EBITDA 仍为简化的 `(市值 + 债务 − 现金) / EBITDA`，未调整少数股东/优先股等
  请求权。FCF 收益率仍为已报告 FCF / 市值，并披露现金流性质，不声称其是已验证的
  FCFE 收益率。每项输入均保留来源，不再仅引用其中一个数据源；确定性报告继续保留
  这些信息和警示。

工具名、参数及最终报告 Schema 不变。DCF 计算函数增加可选的
`cash_flow_basis`/`net_debt` 关键字参数；但旧调用未明确现金流性质时现在返回 `None`。
该行为及年度 `eps_ttm` 返回 null，是对原有误导性语义的必要修正。

## 依赖安装

`pyproject.toml` 是依赖版本的统一来源，核心依赖保持不变。可选组分别为测试
（`dev`：pytest）、持久化（`sqlite`：aiosqlite 和 langgraph-checkpoint-sqlite）、
界面（`web`：Gradio）及金融数据源（`real-data`：yfinance 和 AkShare）。

```bash
pip install -e .                 # 仅核心功能
pip install -e ".[dev,sqlite]"   # 完整离线测试与 checkpoint 示例
pip install -e ".[web]"          # Gradio UI
pip install -e ".[real-data]"    # 可选金融数据源
```

SQLite 最低版本 aiosqlite 0.22.1、langgraph-checkpoint-sqlite 3.1.3 与本次验证使用的
现有环境一致。单独安装 `dev` 不会安装 SQLite，完整测试的安装命令显式选择两个组。
`requirements.txt` 保留为仓库根目录下的部署安装入口，内容引用 `.[web]`，不重复维护
包版本。使用该入口需要完整项目（包括 `pyproject.toml`），不能只复制 requirements 文件。

Web UI 始终构造脚本模型，API Key 不改变模式。数据默认为 Mock，管理员的 Provider
配置仍然生效，报告保留来源标记。真实 LLM 使用 `examples/research.py`，需匹配的
Provider、模型和 API Key，Provider 可能收费。浏览器交互与外部托管需另行验证。

## 异步、Checkpoint 与追踪

公共 API 以异步为优先（`ainvoke`/`arun`），并提供同步包装供简单脚本使用。默认
checkpointer 为内存实现。SQLite checkpoint 可通过
`app.graph.checkpoint.build_checkpointer` 使用。除非配置 `STOCKMIND_TRACE_DIR` 输出
JSONL，否则 trace 仅保存在内存中。

```bash
python examples/async_research.py --ticker NVDA --lang en
```

内置异步示例显式使用 Mock 工具和 Scripted Demo 模型，即使配置了 API Key 或真实数据
Provider 也是如此。每次调用创建全新的模型与 Agent，两次共享 SQLite saver 和 thread
ID，从而避免复用耗尽的一次性迭代器，同时保留 checkpoint 消息。它展示两次成功的脚本化
研究，不代表自主追问推理或从中断的图节点恢复。模型仍为一次性，正常错误处理保持不变。

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

P1-D 离线验证（Python 3.12.8，复用现有 `.venv`）：

- 修改前基线：83 passed。
- 定向计算/Provider/估值测试：85 passed。
- `.venv/bin/python -m pytest -v`：**133 passed、0 failed、0 skipped**。
  一条沙箱警告涉及 pytest 缓存写入权限，并非测试失败。
- `.venv/bin/python -m compileall -q app tests`：允许写入 `__pycache__` 后通过；
  首次沙箱运行因权限失败。
- `git diff --check`：通过。

新增 50 个用例覆盖 FCFF/FCFE 区分、债务/股数证据、无效折现参数与数值边界、年度/TTM
及未知期间、EPS 回退与零 EPS、正常估值比率、报告警示保留和离线 Provider 元数据。
原有 DCF 冒烟测试调整为要求明确口径，或验证 mock FCF 返回不可用。未调用真实金融 API
或付费 LLM。

Cleanup-B 离线验证（2026-11-16，复用现有 Python 3.12.8 环境；未安装或升级依赖）：

- `.venv/bin/python -m pytest tests/test_cleanup_b.py -v`：**6 passed**。
- `.venv/bin/python -m pytest -v`：**139 passed、0 failed、0 skipped**。
  一条警告涉及沙箱内 pytest 缓存写入权限。
- `.venv/bin/python -m compileall -q app tests examples web`：允许写入字节码后通过；
  首次尝试受到沙箱权限限制。
- `git diff --check`：通过。

新增六个用例验证 SQLite 导入及后端操作、AMD/英文与 NVDA/中文各两次完整脚本运行
（每次 15 次工具调用、无错误、共享 checkpoint 历史）、模型的一次性耗尽错误，以及
中英文 Web 文案。异步测试明确阻止选择外部数据/LLM Provider；Web 文案通过静态检查
验证，没有导入或启动 Gradio。

依赖声明已解析，并与环境中已安装的核心、开发及 SQLite 版本核对；pip 已解析本地
`.[web]` requirements 入口。这不是全新环境安装或部署测试。环境缺少 Gradio、
yfinance 和 AkShare，未验证浏览器交互或真实 Provider 集成。Provider 测试均使用
离线 fixture/stub，未调用真实金融 API 或付费 LLM。
