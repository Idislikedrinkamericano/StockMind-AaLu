# StockMind 工程说明

[English](engineering-notes.md) · [项目主页](../README.zh-CN.md)

本页保留面向贡献者的实现细节，避免项目主页变成开发记录。

## 工作流与报告

`StockMindAgent` 运行由 `agent`、`tools` 和 `finalize` 节点组成的异步 LangGraph
工作流。State 会保留请求 ticker、消息、工具调用、观测结果、错误、状态和运行元数据。
工作流限制迭代次数，拒绝 ticker 与单股票请求不一致的工具调用，并将工具失败记录为观测
结果，而不是悄悄当成数据处理。

`finalize` 直接调用 `app/reporting/assembler.py`，仅从有效观测结果构建最终报告。
不再调用报告生成 LLM、创建 structured-output 候选或解析 JSON 候选。工具请求和观测
结果的 ticker 检查保持不变。LLM 推理文本不作为报告证据。没有有效观测时，报告明确
说明证据不可用；组装异常时仍返回附带原因的 no-data 报告。

### Finalize 追踪与兼容性

旧路径无论候选内容如何，都返回确定性报告。额外 LLM 调用只可能增加成本、延迟、拒绝
诊断或 `llm_finalize_timeout`，不会改善最终交付的报告。仓库内没有其他功能消费候选
报告。原 JSON fallback 只解析已有推理文本，并非另一次 LLM 调用。

- 每个完成的 Finalize 节点产生一个 `finalize` 事件，记录本地组装耗时、运行结束状态，
  不计 LLM token。Trace 摘要标为 `report_assembly`，Web 进度文案说明确定性组装。
  不再产生新的 `finalize_fallback`、候选拒绝诊断或 `llm_finalize_timeout`。
- 原先没有独立的公开 Finalize 配置。`AgentConfig` 和环境变量均保持不变；
  `llm_timeout_seconds` 继续约束研究阶段的 LLM 调用，没有保留无效兼容配置。
- 删除了无调用的 `FINALIZE_PROMPT`、`get_finalize_prompt()` 和候选生成私有辅助函数。
  Prompt helper 未从 Agent 包导出，也未作为 API 写入文档，但仓库外直接导入者需要
  调整。依赖已移除事件或 `final_llm_call` 标签的 trace 消费者也需调整；历史 JSONL
  文件不修改。
- 图节点/边、报告 Schema、State 和 checkpoint 契约保持不变。`final_report` 仍保留
  原始推理以兼容 State；只有 `final_output` 是基于证据的报告。非 timeout 运行在报告
  组装后仍按既有行为关闭为 `finished`，保留此前错误；该状态不保证所有数据请求成功。

## 运行时安全

`AgentConfig` 提供迭代上限以及 LLM、工具和整次运行的时间预算。以 `STOCKMIND_` 开头的
环境变量可配置默认值，详见 [`.env.example`](../.env.example)。deadline 通过运行元数据
传递，因此重试、退避、LLM 调用和新的工具调用都不能超过剩余预算。超时运行会保留已验证
观测结果、记录超时错误，并返回部分数据或无数据报告，而不是编造结论。

`asyncio.CancelledError` 会被重新抛出；取消不会被转化为普通工具失败。

Finalize 保留已有 `timeout`，不重复添加错误；本地组装后也会检查剩余运行预算，若
结束前已到 deadline，则记录 `run_timeout`。超时后仍可安全组装部分数据或 no-data
报告，不再启动 LLM 或工具调用。同步组装无法在函数执行中被抢占，因此本地报告构建
并不具备严格的墙钟时间硬上限。

回归测试覆盖调用方取消任务，以及到达本地组装器的 `CancelledError` 传播。另一次
故障注入发现：当前安装的 LangChain 会把模型在 `_agenerate` 内主动抛出的
`CancelledError` 转为 `AttributeError`，之后才到达工作流。研究节点代码未改动；此
依赖边界仍在本轮 Finalize 范围之外，不能据此保证所有 Provider 的取消实现都可靠。

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

### 原有评估审计

既有顺序 Runner、ticker-to-agent factory、`arun()` State 和 `TraceCollector` 可以
复用。原有三条未版本化题目没有 Case ID、场景定义或输出约束。注入真实 Agent 可以
执行研究，但旧 CLI 会因存在凭据自动启用真实 LLM；默认 Demo 本身没有动态工具选择。

实际测量主要是耗时和模型提供的 token 元数据，成本使用近似单价。“工具选择准确率”
实际只有 recall，而且将请求/观测工具都计入，包括被拒绝的请求。Grounding 只检查来源
列表非空；数值准确率只比较价格，缺少 Provider 或取值失败还会通过，并可能在评分时
再次取数。质量分只是章节填充率，unavailable 文本也能得分。有报告、finished 和非空
摘要不足以证明任务完成。本轮不再输出这些过于乐观的近似分数。

### 数据集与执行

[questions.json](../app/evaluation/datasets/questions.json) 现在是经过校验的
`stockmind-eval-v1` 对象，包含 12 个案例及 `synthetic-v1` fixture 版本。每个案例明确
唯一 ID、问题、ticker、场景、预期执行工具、输出约束和独立的固定脚本。运行时不会根据
预期工具标签生成脚本。工具名和参数使用现有工具注册表及 Pydantic 输入 Schema 校验。

| 案例 | 覆盖内容 |
| --- | --- |
| price-nvda、trend-amd | 当前价格与固定历史窗口 |
| technicals-tsm、risk-amd | MA/RSI/MACD 及波动率/回撤 |
| financials-nvda、valuation-tsm | 财报、P/E、简化 EV/EBITDA、已报告 FCF 收益率 |
| combined-amd | 价格、利润表、估值和新闻组合 |
| missing-financials、news-failure、no-price-data | 利润/现金流缺失、新闻失败、唯一价格源失败 |
| wrong-tool-ticker、wrong-observation-ticker | 跨 ticker 请求拒绝、错误 ticker 观测和无依据 LLM 结论隔离 |

[fixtures-v1.json](../app/evaluation/datasets/fixtures-v1.json) 为 NVDA、AMD、TSM 提供
明确标记为合成数据的数值和日期。仅供评估的适配器将其送入现有生产工具，没有修改生产
Provider 或公式。历史数据始终是固定的 40 根合成日线，不代表实时查询窗口。本数据集
尚未覆盖全部工具或 DCF 场景。

`Evaluator()` 为每个案例创建全新的脚本模型、显式 fixture 工具、内存 checkpoint 与
tracer，按固定案例和工具顺序执行。不加载 `.env`、修改环境变量、选择外部 Provider 或
启用网络 tracing，也没有重写 Tracing。案例内未捕获异常会记为失败结果，继续后续案例；
取消会传播。注入 factory 的调用者负责提供独立 Agent 并控制数据/模型访问，其结果标为
`custom-factory-unverified`，不会标成 `offline-scripted`。

### 指标与分母

- **工具选择**：E 为预期工具名称集合，A 为实际 `tool_call` trace 事件中的工具名称
  集合。Precision = |E ∩ A| / |A|，Recall = |E ∩ A| / |E|，F1 = 2PR / (P + R)。
  两集合均空时为 1/1/1；仅一方为空时为 0/0/0。重复尝试不改变集合得分。执行失败仍算
  工具尝试，被拒绝的请求不算；请求名称单独输出。这些分数不评价参数、自主推理或金融事实。
- **任务完成**：要求有效报告、`finished`、证据检查通过、必要工具已执行，且满足全部
  输出/错误约束。约束包括有限的必需指标、fixture 预期值（相对容差 1e-9、绝对容差
  1e-12）、缺失指标、可用/不可用章节、精确预期错误数、错误文本及禁止出现的结论。
  多余工具由 precision 单独惩罚。no-data 不能完成普通价格查询，但可完成明确指定的
  安全失败处理案例。
- **证据 grounding**：复用工作流 grounded/no-data helper，从本次 observations 重建
  期望报告，校验 ticker、完整指标/来源、叙述字段与有限数值，仅忽略 `generated_at`。
  无依据数值或文本会失败，诚实 no-data 可以通过。这是确定性输出一致性检查，不是独立
  真值验证；组装器共有缺陷或不准确源数据仍可能通过。
- **执行指标**：tool call count 统计已记录的 `tool_call` 尝试；请求数包含被拒绝或
  去重的请求。Error count 是 State 错误列表长度，不重复累计 trace 错误。Latency
  使用实际 `run_end` trace 耗时；缺少该事件的异常使用包含初始化的 Runner 耗时，并
  标记 `latency_source`。次数由既有记录推导，不是对每次 Provider 请求独立插桩；尤其
  现有 trace 可能遗漏执行中因总 deadline 取消的工具调用。
- **汇总**：对每案例 P/R/F1 做不加权算术平均；完成率/grounding 率用通过数除以
  **全部案例数**，包含 Runner 失败和安全失败场景。平均耗时用同一分母；工具/错误次数
  求和，不静默从分母剔除失败案例。

### 运行与复现

```bash
python examples/evaluate.py --output /tmp/stockmind-evaluation.json
python examples/evaluate.py --lang zh   # JSON 输出到 stdout，不加载 .env
```

JSON 包含数据集/fixture 版本及哈希、运行时包版本、Case ID、预期/实际/请求工具、来自
trace 的调用记录、报告、observations、全部检查、执行指标、失败原因和聚合规则。
数据集 JSON 已加入 package data；没有新增依赖。在记录的运行环境中，输入值、顺序和
判定可以复现。耗时、报告生成时间和观测取回时间会变化，不承诺原始 JSON 字节相同。
测试对两次运行的稳定分数、次数及去除生成时间的报告进行比较。

兼容性：旧的未版本化问题列表接口改为 `load_dataset()` 和 `EvaluationDataset`。
`Evaluator(agent_factory)` 仍接受 ticker-to-agent factory，但评估输入使用新数据集
模型；`RunMetrics`/`EvaluationReport` 改为可序列化 Pydantic 模型。旧质量、数值准确率
及将 recall 称为 accuracy 的 helper 已移除，调用者应使用显式检查和 `aggregate`
字段。CLI 不再根据凭据自动启用真实 API，也不提供 live 模式参数。

P2-A 验证（2026-11-17，复用 Python 3.12.8 / pytest 9.2.0）：

- `.venv/bin/python examples/evaluate.py --output /tmp/stockmind-p2-a-baseline.json`：
  12/12 完成、12/12 grounding 一致；macro P/R/F1 均为 1.0；记录 25 次工具尝试、
  3 个预期场景错误，所有结束状态为 `finished`。最终本地运行平均 Agent 耗时 11.32 ms，
  只是一次测量，不是性能 benchmark 或成本承诺。
- `.venv/bin/python -m pytest tests/test_evaluation.py -v`：**34 passed**。
- `.venv/bin/python -m pytest -v`：**182 passed、0 failed、0 skipped**（13.42 秒）。
  一条警告涉及沙箱内 pytest 缓存写入权限。
- `.venv/bin/python -m compileall -q app tests examples`：允许写入字节码后通过；
  首次沙箱运行受到权限限制。
- `git diff --check`：通过。

测试覆盖 Schema 拒绝、空/重复工具集合、多余/缺失工具、独立重复运行、阻止联网时的
不利环境配置、篡改报告证据、no-data 完成规则、失败案例分母、取消和 JSON 输出。
原有 Workflow 测试仍在完整套件中。脚本高分只验证接线与规则，不衡量真实 LLM 的工具
选择、研究质量、真实 Provider 可靠性或金融正确性。本轮没有 LLM-as-Judge 或 P2-B/P2-C。

### Prompt 记录

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

Finalize Optimization 验证（2026-11-16，复用现有 Python 3.12.8 / pytest 9.2.0
环境；未修改依赖）：

- `.venv/bin/python -m pytest tests/test_finalize.py tests/test_workflow.py tests/test_phase3.py tests/test_language.py -v`：
  **41 passed**。
- `.venv/bin/python -m pytest -v`：**151 passed、0 failed、0 skipped**（16.71 秒）。
  一条警告涉及沙箱内 pytest 缓存写入权限。
- `.venv/bin/python -m compileall -q app tests web`：允许写入字节码后通过；
  首次尝试受到沙箱权限限制。
- `git diff --check`：通过。

新增十二个用例覆盖原生工具绑定及实际研究 LLM 调用次数、Finalize 零额外调用、token
统计、报告/观测 ticker 不匹配、不可信 JSON/文本、no-data 与组装异常、过期 deadline、
已有 timeout 状态、调用方取消、本地取消传播、checkpoint 结束和双语进度格式。原先
验证候选拒绝、Finalize LLM 超时和中文 Finalize prompt 的测试均改为验证新行为，没有
直接删除。首次定向测试有一项内部取消注入失败；调用方取消测试改为真实的
`Task.cancel()`，依赖行为已在上文披露。没有降低生产代码的取消处理要求。

Web 测试只执行纯进度格式化函数，Gradio/浏览器运行仍未验证。未调用真实金融 API 或
付费 LLM，未宣称成本或延迟 benchmark。
