# StockMind 工程说明

[English](engineering-notes.md) · [项目主页](../README.zh-CN.md)

本页保留面向贡献者的实现细节，避免项目主页变成开发记录。

## 真实执行验证（P3）

### 审计与执行架构

现有 `get_chat_model` 选择 ChatOpenAI 或 ChatAnthropic，通过原生工具绑定驱动
既有 ReAct graph；显式真实 Provider 已能绑定同一工具注册表。本轮未引入第二套
Client、新金融工具、报告 Schema、公式、Graph 路由或 P2 评分规则。

旧 `research.py` 加载本地 dotenv，在两种 API Key 都缺失时切换脚本模型，数据
默认为 Mock；异步示例和 Web 也是脚本演示，不是严格的真实验证入口。
`examples/real_smoke.py` 显式选择模型与 Provider，每案例使用独立内存 checkpoint
和仅驻内存的 TraceCollector，关闭 LangSmith 遥测，不加载 dotenv 或回退 Mock。

生产执行上限、ticker 拒绝、去重和安全 Finalize 保持不变。Finalize 仅使用确定性
assembler；未恢复 LLM 异常保留 `error`，超时保留 `timeout`，已处理 Provider
失败可以返回 `finished` 部分报告。错误与成功 observations 分开记录。
END timeout checkpoint 的重读仍不能重新启动研究。

### 配置与显式执行

环境变量：`STOCKMIND_LLM_PROVIDER=openai|anthropic`、`STOCKMIND_LLM_MODEL`，
以及所选 Provider 的 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`。
未指定模型时沿用现有默认值（`gpt-4o-mini` 或 `claude-3-5-haiku-latest`），
没有验证账户是否能使用这些模型。OpenAI-compatible 端点使用
`OPENAI_API_BASE` 或 `OPENAI_BASE_URL`（前者优先）；Anthropic 可使用
`ANTHROPIC_BASE_URL`。URL 必须为 HTTP(S)，不得嵌入用户名密码、查询参数或 fragment。
变量由操作者导出；Smoke 入口不读取或修改真实 `.env`。

```bash
python examples/real_smoke.py --case aapl-price
python examples/real_smoke.py --case aapl-price --execute-real
python examples/real_smoke.py --ticker MSFT --query "Research price and trends" \
  --provider yfinance --deadline 60 --max-iterations 4 --output-dir smoke-results
```

第一、第三条仅预检。任何真实执行均需 `--execute-real`，使用前应确认案例、模型及费用。
CLI 在构造客户端前打印计划。默认每案例最多 4 次 LLM 调用，每次最多 1,024 输出 token，
LLM / Tool / Run 预算为 20 / 15 / 60 秒，SDK 和工具重试均为 0。
工具并发请求数和输入 token 总量没有严格上限，因此上述限制不是金额上限；
费率依赖所选端点，本轮不提供美元估算，也不声称零费用。

缺少凭据/依赖和不支持的裸 A 股代码会返回 `blocked`，不构造客户端。
运行时网络/认证等失败返回 `fail`，保留实际错误和状态。产物不保存凭据或 Base URL；
异常文本中的已知密钥、授权/token 模式和 URL 会被脱敏。用户仍不应在问题中放入秘密。

### 方法与结果契约

每案例记录 ID、ticker/问题、LLM Provider/模型、金融 Provider、执行限制、
注入标签、实际 trace 工具调用、来源可用性/期间、grounding 检查、状态、
耗时、错误/警告、outcome 及可选报告路径，保留成功 observations 供检查。
每次创建 `run-<UTC>-<随机后缀>/` 防止覆盖旧产物，拒绝受保护路径与用户符号链接，
允许 macOS 标准临时目录别名。默认 `smoke-results/` 已被 Git 忽略；
其他自定义目录需要自行忽略，或放在仓库外。

pass 同时要求：研究阶段收到 LLM 响应、正常终止、所需工具确实执行并取得有限且
可用的数据、认可的真实 Provider 来源和时间戳、ticker 一致，以及原 P2 grounding
一致性检查通过。所需工具类别基于成功 observations，不只是尝试调用。
只有报告文件或纯文本响应不能通过。错误 ticker、Mock、只有元数据或缺失值的
payload 不能满足来源覆盖；这不等于独立验证数据源真值或估值正确性。

| 案例 | 必需证据 / 行为 | 当前真实结果 |
| --- | --- | --- |
| AAPL 价格 | 价格 + 历史行情 | blocked |
| MSFT 综合 | 价格 + 历史 + 技术工具 + 财务工具 | blocked |
| 600519 A 股 | 价格 + 历史 + 技术工具 | blocked；适配器不支持 |
| AAPL Provider 失败 | 真实价格；受控利润表异常；财务部分 unavailable | blocked |

第四个案例仅包装 `get_income_statement` 抛出受控异常，其他请求仍走所选真实
Provider；必须实际获得价格、观察到注入错误并正确披露财务缺失才能通过。
这是局部故障实验，不能声称外部 Provider 真的宕机。自定义案例无论额外研究要求为何，
均要求价格和历史行情。

### 本轮结果与最小数据修复

2026-11-17（America/Los_Angeles）预检：进程没有支持的 LLM 凭据，本地没有 dotenv
文件，两个可选 Provider 包均未安装。四个案例全部 **blocked，真实 LLM 和金融 API
调用均为 0**，未测试网络可达性。现有 AkShare 实现使用美股
Sina/Eastmoney/Baidu 端点，仅安装 AkShare 不能验证 `600519`；本轮没有新增 A 股适配器。

离线复现确认：yfinance `fast_info.last_price=NaN` 会作为价格返回；
回退 `regularMarketPrice=0` 则因布尔 `or` 被 `currentPrice=123` 替代。
最小修改复用 `_as_float`，选择首个有限且非缺失的报价并保留零值，无可用报价则抛出异常。
历史 OHLC 缺失/非有限时拒绝该序列，不填零或静默插值。财务字段映射、
期间元数据、金融公式及 DCF 语义不变。

新增离线测试覆盖配置、缺少凭据/依赖、显式开关、两种既有 Client factory、
安全/非法 Base URL、使用 stub Client/HTTP 边界的实际 Graph 路径、缺失数据、
Provider/LLM 异常、ticker 不一致、来源检查、deadline/取消、受控部分失败、
严格 JSON、脱敏和安全输出路径。

P2 重跑：基线 completed 与 grounded 均 12/12，macro tool P/R/F1 为 1.0；
可靠性 11 pass / 0 fail / 1 unsupported，安全终止 10/10，恢复 7/8
（分母包含符合条件的 unsupported 案例），安全违规 0。以上仍是离线脚本结果。
实际命令、测试数量和中间失败记录见本轮 Codex 日志，没有降低 P2 阈值。

原工作区最终验收：**P3 定向 49 passed**（1.65 s），**完整 312 passed、
0 failed、0 skipped**（18.80 s）。app/tests/examples 的 compileall 与
git diff --check 均通过。实际仓库不在会话默认可写路径内，因此测试禁用字节码与缓存写入，
compileall 字节码写入临时目录。没有安装依赖；此前间歇性失败仍保留在下方及日志中。

### 扩大使用前的剩余限制

- 真实模型工具调用兼容性、凭据、行情新鲜度及网络可用性仍未验证；需操作者确认
  凭据与费用、安装可选依赖后再做真实验证。
- 当前 AkShare 不支持要求的 A 股案例。美股报价时间戳可能只是取回时间；
  财务期间和发布日期仍可能 unknown/mixed，现有数据仍不足以确认严格 DCF 现金流口径。
- 同步金融 Provider 在线程中运行；asyncio 超时阻止新的工作流调用，但不能强制停止
  已开始的同步请求，进程退出可能等待该线程。本轮不承诺硬性墙钟时限，
  Provider/网络取消需要进一步验证。
- Grounding 与生产 assembler 共用 oracle；长工具 payload、提示注入抵抗能力、
  真实费用/性能尚未评估。
- 两次完整测试出现 P2 重复性结果不一致，单独重跑通过。50 ms 故障注入预算可能受
  调度影响，但尚未确认根因。本轮未调整可靠性断言或预算。


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
  原始推理以兼容 State；只有 `final_output` 是基于证据的报告。Finalize 保留 `error`、
  `timeout` 和 `max_iterations`，不将组装报告等同于运行成功。`finished` 表示推理循环
  正常结束，仍可能有已处理的工具错误或数据缺失；详见下文修复后的状态语义。

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
选择、研究质量、真实 Provider 可靠性或金融正确性。P2-A 基线没有 LLM-as-Judge；
独立的可靠性扩展见下文。

### 可靠性评估

`stockmind-reliability-v1` 在
[`reliability-v1.json`](../app/evaluation/datasets/reliability-v1.json) 中新增 12 个案例，
复用 `synthetic-v1` 合成 fixtures。每个案例保留基线的问题、ticker、预期工具、有效执行
脚本和输出约束，新增故障注入、预期终止状态/处理方式、恢复规则、安全终止适用标记和
工具尝试次数要求。预期行为是测试规则，不表示生产代码已经支持。

既有保护机制审计：

- 工具异常、工具超时已有 trace 错误和有上限的重试；统一 run deadline 约束尝试与退避。
  Schema 拒绝非法参数，错误 ticker 和未知工具在执行前被拒绝，前序步骤的相同签名会跳过。
- LLM 异常保留错误和已取得观测；LLM/run timeout 保留 `timeout` 终止状态。Finalize
  只采用已验证观测，返回部分或 no-data 报告。本轮复用这些机制，没有重新实现。
- `arun()` 会初始化新运行。编译后 graph 原生的 `interrupt_after` 和 `ainvoke(None)`
  可继续暂停的 checkpoint；到达 END 的 graph 没有待重试节点。P2-A 提供了可复用的
  traces 和契约检查，但没有故障触发、恢复及 unsupported 评估。

新 `ReliabilityEvaluator` 通过两个小型构造/调用扩展点复用原有顺序 Runner。
`FaultModel` 修改指定工具请求或抛出模型故障；仅用于评估的 fixture 子类抛出 Provider
异常；工具包装器保留原有参数 Schema，注入空数据、异 ticker 数据或超时。工具仍走
未修改的 LangGraph 正常路径。Runner 不全局 patch Provider；每个案例独立创建模型、
fixture、journal、TraceCollector 和内存 saver。执行证据来自现有 TraceCollector；
journal 只记录注入和 checkpoint 操作，不重复实现 tracing。

超时故障等待可取消的 `asyncio.Event`，不使用长时间 sleep。评估配置为单次操作
0.05 秒、整次运行 2 秒、案例 watchdog 5 秒；仅两个重试案例允许重试一次，退避和
jitter 为零。生产超时默认值不变。取消继续传播，测试验证等待中的故障收到取消，
LLM 超时后不启动后续新闻工具。环境凭据/Provider 配置不会启用真实服务，telemetry
已禁用，CLI 不加载 `.env`。

指标定义与输出：

- 保留 P2-A 指标。工具 P/R/F1 比较实际执行的工具名称集合；次数另行反映重复与重试。
  Error count 是 State errors 长度，`observed_behavior.errors` 还保留重试成功前的
  trace 错误。Latency 为实测值，不是预期标签；checkpoint 耗时包含隔离探测，另一个
  thread 的工具使用独立 trace，不计入主 thread 工具次数。
- Reliability 必须满足：实际触发注入、预期状态/处理方式、全部输出与 grounding 约束、
  工具集合/尝试次数要求、无 Runner 失败、无安全违规及规定的恢复行为。P2-A 的
  `finished` 完成条件仍保留在基线字段，但不纳入可靠性输出约束，因此安全 timeout
  可通过可靠性检查，同时 `task_completed=false`。不能仅凭基线完成指标认定成功。
- Safe Termination Rate = 通过的适用案例 / 所有标记 `safe_termination_required`
  的案例。两个 checkpoint 恢复探测不纳入此分母；同批重复执行违反至多一次的约束。
- Recovery Rate = 通过且恢复成功的案例 / 所有声明恢复预期的案例。重试要求后续尝试
  真正成功；修正请求与部分报告恢复要求后续步骤工具成功且得到满足约束的 grounded
  报告；checkpoint 恢复要求实际读取/恢复、保留已存观测且 thread 隔离。捕获异常
  本身不算恢复；unsupported 恢复仍计入分母，绝不计入成功数。
- 两项比例均输出 numerator、denominator 和 unsupported 数量，零分母输出 `null`。
  分类区分正常完成、安全降级、受控失败、非预期失败、unsupported；后两者不计成功，
  不生成加权总分。
- 安全违规记录类别、字段、预期值/观测值和可检查证据。数值/报告一致性复用生产
  grounded/no-data 组装器；检查报告/State ticker 错误、无依据数值（含嵌套指标）、
  不可用部分的肯定性陈述、最后一次推理异常且未恢复但状态为 `finished` 等情况。
  这是模板/契约一致性检查，不是自由文本事实审查或独立金融真值验证。
- JSON 保留 P2-A 字段，新增每例 `reliability` 和顶层 `reliability_aggregate`。
  `reliability.failure_reasons` 解释其判定，基线失败原因单独保留；注入标签、预期行为
  与实际 trace 错误、状态、恢复证据分开记录。

P2-B 修复前结果（2026-11-17，现有 Python 3.12.8 环境）：

| Case ID | 结果 | 实际行为 |
| --- | --- | --- |
| `provider-retry` | pass | 首次异常，重试取得已验证价格 |
| `tool-timeout-retry` | pass | 首次尝试被取消，重试成功 |
| `invalid-arguments` | pass | 非法 RSI 参数被拒绝，后续价格成功 |
| `wrong-ticker-request` | pass | NVDA 研究中的 AMD 请求被拒绝，修正请求成功 |
| `duplicate-same-batch` | fail | 同批相同请求执行两次，预期一次 |
| `unknown-tool-request` | pass | 未知工具被拒绝，后续有效请求成功 |
| `llm-exception-partial` | fail | 推理错误保留，但最终状态为 `finished` |
| `llm-timeout-partial` | pass | 终止状态 `timeout`，保留价格，未调用新闻工具 |
| `invalid-observation-partial` | pass | 拒绝异 ticker 价格，保留有效财务报告 |
| `missing-data` | pass | 空 payload 返回既有 no-data 报告 |
| `checkpoint-paused-resume` | pass | 新 Agent 恢复同 saver/thread，另一个 ticker 隔离 |
| `checkpoint-terminal-retry` | unsupported | END 重读保留 timeout 报告，新模型调用数为零 |

合计 **9 pass / 2 fail / 1 unsupported**；安全终止 **8/10 = 80%**；恢复
**7/8 = 87.5%**，分母包含 1 个 unsupported。分类为正常完成 1、安全降级 7、受控
失败 1、非预期失败 2、unsupported 1。**安全违规 1 项**，即终止状态的歧义；这 12 份
报告没有混入无依据金融数值或错误 ticker 数据。Grounding 为 12/12，工具集合 macro
P/R/F1 均为 1.0，基线任务完成为 10/12，记录 17 次工具尝试、6 个 State 错误。
这些指标没有掩盖两项可靠性失败。

P2-B 基线观测到的生产行为（已在下文修复）：

1. `workflow.py` 检查来自前序 State 的 `existing_signatures`，但仅将本批签名追加至
   `new_signatures`，所以同批第二个相同请求仍进入 jobs。trace 实际记录两次成功尝试。
2. 推理异常分支设置 `status="error"`，Finalize 将所有非 timeout 状态归为 `finished`。
   错误仍可见且报告仍 grounded。P2-B 的更严格终止状态规则将其标为
   `failure_reported_success`；这不表示金融数据被编造。

Checkpoint 覆盖只使用进程内 memory saver：在 tools 后真正暂停，另开 AMD thread，
用相同 saver 创建新 NVDA Agent，验证保存状态并通过 `ainvoke(None)` 恢复，不将新
`arun()` 冒充恢复。已完成的 timeout 的 `next == ()`，重读实际返回相同报告/状态且
不执行新模型，因此 END 后自动重试标记 unsupported。另有测试模拟 checkpoint
读取失败，确认评估失败且不虚构报告。未验证跨进程恢复、持久数据库故障、真实 LLM
内部状态恢复、长暂停/deadline 续期、同步网络线程取消。恢复使用的 Fake Model 是为
待执行节点显式创建，并非恢复了模型内存。该评估建设阶段未修复生产问题，也未进入 P2-C。

验证命令与实际结果：

- `.venv/bin/python examples/evaluate.py --output /tmp/stockmind-p2-b-baseline.json`：
  原有 12/12 案例完成且 grounded，25 次尝试、3 个预期 State 错误。
- `.venv/bin/python examples/evaluate.py --suite reliability --output /tmp/stockmind-p2-b-reliability.json`：
  **9 pass / 2 fail / 1 unsupported**，保留失败原因与证据。
- `.venv/bin/python -m pytest tests/test_evaluation.py tests/test_reliability_evaluation.py -v`：
  **76 passed**（2.43 秒），其中 42 项为新增测试。
- `.venv/bin/python -m pytest -v`：**224 passed、0 failed、0 skipped**（15.33 秒）。
  一条沙箱 pytest 缓存权限警告；未跳过或削弱测试。
- `.venv/bin/python -m compileall -q app tests`：允许写入字节码后通过，首次运行因
  沙箱 `PermissionError` 失败。
- `git diff --check`：通过。没有安装或升级依赖。

两项 Evaluation 失败由通过的回归测试明确断言，不是 pytest 失败。固定运行环境中的
输入和判定可复现，但耗时和时间戳会变化；短超时模拟不是 Provider 性能 benchmark。
复用的 assembler oracle 无法独立发现其自身缺陷。CLI 默认仍为 P2-A，通过
`--suite reliability` 选择新 Schema；既有 package-data 规则已包含新增 JSON。
CLI 成功写出完整评估结果时退出码为 0，即使有 fail/unsupported；CI 应读取
`reliability_aggregate.verdict_counts`，不能把退出码当作全部案例通过。
该评估建设阶段没有改变报告/State/Checkpoint 契约或生产执行代码。

### 可靠性修复（P2-B Fix）

开始前确认提交 `7081acb` 已包含 P2-B，工作区干净。原有 12 个案例的数据集、故障
注入和评分规则保持不变。本轮只修复两项已发现的生产缺陷及必要的状态/trace 消费者，
没有改变 Provider、金融公式、报告 Schema 或图结构。

重复调用的根因是只检查前序 State 中的签名。本轮在检查下一请求前，立即将当前请求
签名加入节点内局部已见集合，复用原有 `tool_call_signature` 的规范 JSON（包括嵌套
对象的 key 排序），保留工具名和参数值的区别，不合并不同工具、ticker 或参数。
不同 ticker 仍需各自合法的 single-stock run；同次运行中的错误 ticker 继续被拒绝。
没有引入全局缓存或跨 thread 共享去重状态，新运行从空状态开始，跨轮去重保留。

被跳过请求仍保留 ToolMessage 和请求记录，并产生带工具名、参数及原因的
`tool_skipped` trace。文案说明“已经请求”，不再声称“已经执行”，因为第一份请求可能
尚在队列中或执行失败。跳过事件不是 `tool_call`、错误或 observation。CLI trace
摘要和 Web 进度均展示原因。不同调用仍走原有重试、超时和错误路径；跳过重复请求
不会替代第一份调用的有上限重试。

状态缺陷发生在 LLM handler 已设置 `error` 后：Finalize 将全部非 timeout 状态改为
`finished`。现在 Finalize 保留节点状态，同时使用相同证据规则组装报告。已有迭代上限
状态也保留，避免将达到上限误报为正常完成；原有 deadline 过期检查仍优先生效并记录
`run_timeout`。LLM 异常 trace 也明确带有 `final_status="error"`。

| Status | Finalize 后语义 |
| --- | --- |
| `finished` | 推理循环正常完成，仍允许已处理的工具错误和 unavailable 数据 |
| `error` | 未恢复的 LLM 异常，保留已验证观测及部分/no-data 报告 |
| `timeout` | LLM/run 时间预算耗尽，仅报告已验证证据 |
| `max_iterations` | 达到迭代上限，可返回安全报告，但不算正常完成 |

State 的 status 本来就是字符串，无需新增枚举、字段或修改报告 Schema；注释补齐
timeout。CLI 摘要、Agent 的 `run`/`arun`、checkpoint 序列化和 Evaluation 均可消费
返回的状态。`invoke`/`ainvoke` 继续兼容地返回安全报告；需要执行状态的调用方应使用
`run`/`arun` 或读取 trace/checkpoint。P2-A 完成指标仍要求 `finished`，Reliability
可将受控失败判为通过，同时基线任务完成为 false，原安全检查保留。Web 文案区分失败、
超时、迭代上限和成功，报告就绪提示移除成功勾号；没有修改页面布局或研究业务逻辑。

| Reliability 案例 | 修复前 | 修复后 |
| --- | --- | --- |
| `provider-retry` | pass | pass |
| `tool-timeout-retry` | pass | pass |
| `invalid-arguments` | pass | pass |
| `wrong-ticker-request` | pass | pass |
| `duplicate-same-batch` | fail：执行两次 | pass：两次请求、一次执行、一次跳过 |
| `unknown-tool-request` | pass | pass |
| `llm-exception-partial` | fail：finished | pass：error，保留有效部分报告 |
| `llm-timeout-partial` | pass | pass |
| `invalid-observation-partial` | pass | pass |
| `missing-data` | pass | pass |
| `checkpoint-paused-resume` | pass | pass |
| `checkpoint-terminal-retry` | unsupported | unsupported |

实际结果：pass/fail/unsupported 从 **9/2/1 → 11/0/1**。安全终止率
**8/10 → 10/10（100%）**；恢复率仍为 **7/8（87.5%）**，分母仍包含 1 个
unsupported；安全违规 **1 → 0**。分类为正常完成 2、安全降级 7、受控失败 2、
unsupported 1，无非预期失败。12 份报告全部保持 grounding 一致；工具尝试从 17 降至
16，State 错误仍为 6。此套件中的 P2-A 任务完成指标从 10/12 变为 9/12，因为 LLM
异常现在如实标为 `error`，并非证据或恢复回归。原有 P2-A 仍为 12/12 完成、25 次
工具尝试、3 个预期错误，macro P/R/F1=1.0。

实际验证（2026-11-17，现有 Python 3.12.8 / pytest 9.2.0）：

- 修复前后 CLI：`.venv/bin/python examples/evaluate.py --suite reliability
  --output /tmp/stockmind-p2-b-fix-before.json`，以及将路径改为
  `/tmp/stockmind-p2-b-fix-after.json` 的相同命令，分别得到 **9/2/1** 和 **11/0/1**。
- `.venv/bin/python examples/evaluate.py --output /tmp/stockmind-p2-b-fix-baseline.json`：
  原有 12 个 P2-A 案例完成且 grounded。
- `.venv/bin/python -m pytest tests/test_reliability_evaluation.py tests/test_reliability_hardening.py tests/test_workflow.py tests/test_finalize.py tests/test_phase3.py tests/test_evaluation.py -v`：
  **135 passed**（2.85 秒）。
- `.venv/bin/python -m pytest -v`：**246 passed、0 failed、0 skipped**（15.51 秒），
  一条沙箱 pytest 缓存权限警告。
- `.venv/bin/python -m compileall -q app tests`：首次沙箱字节码写入权限失败；
  获准后 `.venv/bin/python -m compileall -q app tests web` 通过，也检查了修改的 Web 代码。
- `git diff --check`：通过。未安装或升级依赖。

新增 22 项测试，覆盖签名排序/区别、工具节点及完整 Workflow 去重、重试、不同 ticker/
thread/新运行、memory 与 SQLite `:memory:` 中部分/no-data LLM 异常的 checkpoint、
终止状态与 deadline、中英文 Web 文案。原有测试更新为正确行为，撤销任一修复将使
回归失败；没有删除案例或安全断言。环境未安装 Gradio，Web 测试通过 AST 执行实际纯
格式化函数，没有导入 UI、加载 `.env` 或启动浏览器，不宣称完成浏览器验证。新增测试
阻止网络连接及真实模型/Provider 选择。

剩余限制：END timeout checkpoint 没有待执行节点，仍无法重试。本轮没有增加恢复、
改变已有规范 JSON 之外的 fingerprint 语义、测试所有依赖组合或证明真实 LLM 可靠性；
省略/显式默认参数及不同数字表示未新增归一化。共用 assembler oracle、跨进程和取消
方面的既有限制仍适用。把“有报告”视为成功的外部消费者须改为检查状态；trace 消费者
可能看到新的 `tool_skipped`。没有进入 P2-C，也没有 Commit/Push。

### 可复现离线 Benchmark（P2-C）

开始前确认 `22a6d50`，工作区干净。P2-A 的 `EvaluationReport` 和 P2-B 的
`ReliabilityReport` 已能序列化逐例报告、观测、trace、检查和汇总，CLI 已有两个
单套件 JSON 入口。P2-C 仅增加编排和展示：`benchmark.py` 顺序调用原 Evaluator，
`report_generator.py` 根据序列化结果生成报告，没有另写评分器。Dataset、fixtures、
故障注入、生产 Workflow、金融逻辑和报告 Schema 均未改动，未新增依赖。

```bash
python examples/evaluate.py --suite benchmark --output-dir benchmark-results
python -m app.evaluation.report_generator benchmark-results/evaluation-results.json --output benchmark-results/evaluation-report.md
```

第一条命令生成 `evaluation-results.json` 和 `evaluation-report.md`；第二条仅读取
保存的 JSON，不执行 Agent。默认 `benchmark-results/` 已加入 Git ignore；自定义
输出目录应放在版本控制之外或自行配置 ignore。它们是本地生成产物，不是已提交的
公开报告；再次运行会替换这两个文件。原 baseline/reliability、JSON stdout、
`--dataset` 和 `--output` 保持兼容。Benchmark 固定运行两个内置数据集，拒绝歧义的
`--dataset`/`--output`；`--output-dir` 仅供 Benchmark 使用。`--lang` 调整内部研究
报告语言；审计 Markdown 使用英文标题和稳定机器字段名。

现有 Runner 为每例新建模型、fixture、trace 和内存 checkpoint；仅 checkpoint
探测在同案例内有意共享 saver。显式脚本/Fake Model 和 fixture 不随真实 Provider
配置切换，不加载 `.env`，telemetry 保持禁用，不调用金融/LLM API。源码元数据仅
运行本地 `git rev-parse`/`git status`，不读取 remote 或凭据；非 Git 环境标为 unavailable。

`stockmind-benchmark-v1` JSON 包含：

| 字段 | 含义 |
| --- | --- |
| `benchmark_version`、`started_at`、`generated_at`、`elapsed_seconds` | 版本、UTC 执行/生成时间、实测编排耗时 |
| `source_revision` | Git commit 和工作树是否 dirty；不声称当前未提交代码等于 HEAD |
| `scope` | 两个套件、实际总案例数、Fake Model、合成数据和 checkpoint 范围 |
| `baseline`、`reliability` | 原完整 JSON，含 Dataset/fixture 哈希、运行时版本及逐例原始证据 |
| `baseline_counts` | 对已有 task_completed、grounded 布尔结果计数，供比例展示 |
| `baseline_error_accounting` | 数据集错误预期、观测 State 错误、工具错误及原有失败案例 ID |
| `metric_definitions` | 定义、JSON 来源路径及分母 |
| `historical_comparison` | 明确标注的历史转录，含来源 commit、路径、章节 |
| `status` | completed、completed_with_unsupported 或 completed_with_failures |

当前指标表直接读取这些字段，不硬编码本次分数。两个产物均保留全部 Case ID 和终止
状态，fail/unsupported 展示原因，失败案例还展示实际异常文本。Markdown 不堆积长
trace，JSON 保留完整内容。显示时对比例/耗时取舍小数位，JSON 保留原精度；同一 JSON
再次生成 Markdown 内容一致，但重跑 Benchmark 不承诺时间戳和耗时相同。

指标继续采用现有定义：

- P2-A 完成要求全部规则通过，不是仅有报告；grounding 复用生产 assembler，并非
  独立金融真值。工具评分是所有案例的无权重 macro 集合 P/R/F1，不衡量自主规划能力，
  不新增任意加权总分。
- 工具错误是 `actual_calls` 中 error 非空的实际尝试，State 错误还包含被阻止的请求。
  预期工具错误按声明的数据集错误文本匹配，匹配本身不表示案例完成。非预期失败 ID
  来自原有 completion/grounding=false 或 failure_reasons。当前基线预期/实际 State
  错误为 3，但工具错误尝试仅 2；错误 ticker 请求在执行前被拒绝。
- 安全终止仍为通过的适用案例 / 全部适用案例；恢复仍为通过且恢复的案例 / 所有声明
  恢复预期的案例，包含 unsupported。直接使用 P2-B 的分子、分母及 unsupported 数量，
  不另算排除口径；零分母仍为 null，安全违规保留可检查证据。
- 任一套件出现原有非预期失败时，整体为 completed_with_failures；否则有 unsupported
  则为 completed_with_unsupported，否则 completed。CLI 退出码 0 仅表示产物写出，
  CI 须检查 status 和套件 verdict，不能理解为全部案例通过。预期的受控失败不会被
  重新算成可靠性检查失败。

历史来源：仓库没有跟踪历史原始评估 JSON。Historical Before（9/2/1、安全终止
8/10、恢复 7/8、违规 1）和 Historical After Fix（11/0/1、10/10、7/8、违规 0）
均转录自 `22a6d5039547a1d102f9e29ebd48c4f4c96b2292` 的 `docs/codex-log.md`，
章节 `2026-11-17 — P2-B Fix：Agent Reliability Hardening`，对应同批重复执行及
LLM error 被 finished 覆盖的两项问题。两列都不是在当前 Workflow 上重新计算；
当前实测单独展示，即使它出现回归或与 Historical After 不同。新 Benchmark 不依赖
任何 `/tmp` 历史文件。

实际验证（2026-11-17 America/Los_Angeles；报告时间戳使用 UTC）：

- `.venv/bin/python examples/evaluate.py --suite benchmark --output-dir /tmp/stockmind-p2-c-benchmark`：
  24 例，整体 **completed_with_unsupported**。P2-A 完成及 grounding **12/12**，
  macro P/R/F1 **1.0**，25 次尝试、3 个预期 State 错误、2 次预期工具错误、0 个
  非预期失败。P2-B **11 pass / 0 fail / 1 unsupported**，安全终止 **10/10**，
  恢复 **7/8**，安全违规 **0**。最终本地编排耗时 0.4523 秒，不是其他机器或真实
  服务的性能承诺。
- `.venv/bin/python -m app.evaluation.report_generator /tmp/stockmind-p2-c-benchmark/evaluation-results.json --output /tmp/stockmind-p2-c-benchmark/evaluation-report-regenerated.md`：
  Markdown 与原报告逐字节相同。
- `.venv/bin/python -m pytest tests/test_benchmark.py tests/test_evaluation.py tests/test_reliability_evaluation.py -v`：
  **93 passed**（5.01 秒），其中 **17 项新增测试**。
- `.venv/bin/python -m pytest -v`：**263 passed、0 failed、0 skipped**（按继续指令最终重跑 17.69 秒；首次完整运行 18.25 秒），
  一条沙箱 pytest 缓存写入权限警告。
- `.venv/bin/python -m compileall -q app tests examples`：获准写入字节码后通过；首次
  沙箱执行因 PermissionError 失败。
- `git diff --check`：通过。实际产物位于仓库外的 `/tmp`。

测试还故意使基线和可靠性案例构造失败，确认 failed、实际错误和 unsupported 都保留
在两个产物中；扰动输入分数以识别硬编码，验证分母/来源标记、重复运行、阻止网络和
真实 Provider 选择、自定义/默认目录、无 Agent 的重生成及旧 CLI 兼容。首次定向测试
发现 Markdown 缺少具体异常文本（92 passed、1 failed），已修正 renderer，没有改评分。

这不是真实 LLM Benchmark、真实行情准确性验证、独立金融事实核查、完整真实 Provider
故障测试，也不能证明适合自动交易或投资决策。END timeout checkpoint 仍 unsupported。
机器负载、短模拟超时、依赖版本及 dirty 工作树限制精确复现；已记录输入哈希和包版本，
但未验证所有依赖组合。没有进入 P3，也没有 Commit/Push。

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
