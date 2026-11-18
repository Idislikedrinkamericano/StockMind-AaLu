# StockMind Engineering Notes

[简体中文](engineering-notes.zh-CN.md) · [Project home](../README.md)

This page keeps contributor-oriented implementation details out of the project
home page.

## Real-World Validation (P3)

### Audit and execution architecture

The existing `get_chat_model` factory selects ChatOpenAI or ChatAnthropic; native
tool binding drives the existing ReAct graph. Explicit real providers already
bind to the same tool registry. No second client, new financial tool, report
schema, formula, graph transition or P2 scoring rule is introduced.

The legacy `research.py` loads local dotenv and selects a scripted model when
neither supported API key is present. Its data default is Mock. The async
example and Web are scripted demonstrations. These are not strict live-validation
entries. `examples/real_smoke.py` instead uses explicit model/provider arguments,
fresh in-memory checkpoint state and a memory-only TraceCollector. It disables
LangSmith telemetry and never loads dotenv or falls back to Mock.

Production limits, ticker rejection, duplicate handling and safe Finalize remain
unchanged. Finalize uses only the deterministic assembler; unrecovered LLM errors
retain `error`, timeouts retain `timeout`, and handled provider failures can
produce a `finished` partial report. Errors are separate from successful
observations. END timeout checkpoint replay still cannot restart research.

### Configuration and explicit execution

Environment: `STOCKMIND_LLM_PROVIDER=openai|anthropic`, `STOCKMIND_LLM_MODEL`,
and the selected provider's `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`.
Omitted model names use the existing factory defaults (`gpt-4o-mini` or
`claude-3-5-haiku-latest`); account availability is not verified.
OpenAI-compatible endpoints use `OPENAI_API_BASE` or `OPENAI_BASE_URL` (first wins);
Anthropic supports `ANTHROPIC_BASE_URL`. URLs must be HTTP(S) without embedded
credentials, query or fragment. These variables must be exported by the operator;
the smoke entry does not read or modify the real `.env`.

```bash
python examples/real_smoke.py --case aapl-price
python examples/real_smoke.py --case aapl-price --execute-real
python examples/real_smoke.py --ticker MSFT --query "Research price and trends" \
  --provider yfinance --deadline 60 --max-iterations 4 --output-dir smoke-results
```

The first and third commands are preflight only. Every live invocation requires
`--execute-real`; review cases, model and charges before using it. The CLI prints
the plan before constructing clients. Defaults are 4 LLM calls/case, 1,024 maximum
output tokens/call, 20 s LLM / 15 s tool / 60 s run budgets. SDK and tool retries
are zero. Tool fan-out and input-token volume are not strictly capped, so these
limits do not establish a monetary cost ceiling. Tariffs depend on the selected
endpoint; no dollar estimate or zero-cost claim is made.

Missing keys/packages and unsupported bare A-share tickers return `blocked`
without client construction. Runtime/network/authentication failures return
`fail`, retaining actual error/status information. No credentials or base URL
are written to artifacts; known keys, authorization/token patterns and URLs in
exception strings are redacted. User queries should still contain no secrets.

### Methodology and result contract

Each case stores ID, ticker/query, LLM provider/model, financial provider,
limits, injection label, actual traced tool calls, source availability/periods,
grounding checks, status, elapsed time, errors/warnings, outcome and optional
report path. Successful observations are retained for review. A new
`run-<UTC>-<random>/` directory prevents overwriting earlier artifacts. Protected
paths and user symlinks are rejected; macOS standard temporary aliases work.
Default `smoke-results/` is ignored by Git; custom directories outside this path
need their own ignore rule or should be outside the repository.

A pass requires a research LLM response, normal terminal status, actual required
tool executions with finite, available data and a recognized real-provider source
and timestamp, matching tickers, and the unchanged P2 grounding-consistency
check. Required categories are checked against successful observations, not just
attempted calls. A report file or a text-only response never suffices. Wrong-ticker,
Mock, metadata-only and missing-value payloads cannot satisfy source coverage.
This is not independent verification of source truth or valuation correctness.

| Case | Required evidence / behavior | Current live outcome |
| --- | --- | --- |
| AAPL price | Price + historical prices | blocked |
| MSFT research | Price + history + a technical + a financial tool | blocked |
| 600519 A-share | Price + history + a technical tool | blocked; adapter unsupported |
| AAPL provider failure | Real price; controlled income-statement exception; unavailable financial section | blocked |

The fourth case wraps only `get_income_statement` with a controlled exception;
other requests use the selected real provider. Its pass requires an actual price,
an observed injected error and safe missing-financial disclosure. It is a partial
outage experiment, not evidence that the external provider was down. Custom
cases require price and history regardless of extra requested analysis.

### Results and minimal data fix

On 2026-11-17 (America/Los_Angeles), preflight found no supported LLM credential
in the process, no local dotenv file and neither optional provider package.
All four cases are **blocked; zero real LLM or financial API calls were made**.
Network reachability was not tested. The existing AkShare implementation uses
US Sina/Eastmoney/Baidu endpoints, so installing AkShare alone cannot validate
`600519`; no A-share adapter was added.

Offline reproduction found that yfinance `fast_info.last_price=NaN` passed through
as a price, while fallback `regularMarketPrice=0` was replaced by
`currentPrice=123` because of boolean `or`. The minimal provider change reuses
`_as_float`, chooses the first finite nonmissing quote (preserving zero), and
raises when none exists. Historical OHLC containing missing/nonfinite values is
rejected, not zero-filled or silently interpolated. Financial mappings, period
metadata, formulas and DCF semantics are unchanged.

The new offline tests cover configuration, missing credentials/dependencies,
explicit gating, both existing client factories, safe and unsafe base URLs,
actual graph execution against stubbed client/HTTP boundaries, missing data,
provider/LLM errors, ticker mismatches, source checks, deadlines/cancellation,
controlled partial failure, strict JSON, redaction and safe output paths.

P2 rerun: baseline 12/12 completed and grounded, macro tool P/R/F1 1.0;
reliability 11 pass / 0 fail / 1 unsupported, safe termination 10/10,
recovery 7/8 (including the unsupported eligible case), safety violations 0.
These remain offline scripted results. See the P3 Codex log for exact commands,
test counts and intermediate failures; no P2 threshold was weakened.

Final verification in the original workspace: **49 P3 tests passed** (1.65 s),
**312 full-suite tests passed, 0 failed, 0 skipped** (18.80 s).
Compileall for app/tests/examples and git diff --check passed. Test bytecode/cache
writes were disabled; compileall bytecode was redirected to a temporary directory
because the actual workspace lies outside the session's default writable roots.
No dependency was installed. The earlier intermittent failures remain recorded below.

### Remaining limits before broader use

- Live model tool compatibility, credentials, market freshness and network
  availability remain unverified; these must be tested with operator-approved
  credentials, costs and installed optional dependencies.
- AkShare here does not support the requested A-share case. US quote timestamps
  may reflect retrieval time rather than exchange time. Financial periods and
  publication dates can remain unknown/mixed; current data still cannot justify
  a strict DCF cash-flow basis.
- Sync financial providers run in worker threads. An asyncio timeout stops new
  workflow work but cannot forcibly stop an already running synchronous request;
  process shutdown can wait for that thread. No hard wall-clock guarantee is
  claimed. Provider/network cancellation needs further validation.
- Grounding shares the production assembler oracle. Long tool payloads, prompt
  injection resilience and real-world cost/performance have not been evaluated.
- Two full runs observed a P2 repeatability mismatch; its isolated rerun passed.
  The 50 ms injected-operation budgets are a possible scheduling sensitivity,
  not a confirmed root cause. No reliability assertion or budget was changed.


## Workflow and Reporting

`StockMindAgent` runs an async LangGraph workflow with `agent`, `tools`, and
`finalize` nodes. State retains the request ticker, messages, tool calls,
observations, errors, status, and run metadata. The workflow limits iterations,
rejects tool calls whose ticker does not match the single-stock request, and
records tool failures as observations instead of silently treating them as data.

`finalize` directly calls `app/reporting/assembler.py` to build the final report
from valid observations. There is no report-generation LLM call, structured-output
candidate, or JSON-candidate parsing. Tool-request and observation ticker checks
remain in place. LLM reasoning text is never used as report evidence. With no
valid observations, the report explicitly states that evidence is unavailable;
assembly errors still fall back to a no-data report with an explanation.

### Finalize tracing and compatibility

The removed path already returned the deterministic report regardless of its
candidate. Its extra LLM call could add cost, latency, rejection diagnostics, or
an `llm_finalize_timeout`, without improving the delivered report. No other
in-repository feature consumed the candidate. The local JSON fallback parsed
existing reasoning text; it was not another LLM call.

- Each completed Finalize node now emits one `finalize` event measuring local
  assembly, with the resulting run status and no LLM token usage. Trace summaries
  label it `report_assembly`; the Web progress formatter describes deterministic
  assembly. No new `finalize_fallback`, candidate-rejection diagnostic, or
  `llm_finalize_timeout` is emitted.
- There was no separate public Finalize configuration. `AgentConfig` and all
  environment options are unchanged; `llm_timeout_seconds` still limits research
  LLM calls. No dead compatibility configuration is retained.
- The unused `FINALIZE_PROMPT` and `get_finalize_prompt()` were removed, along
  with private candidate-generation helpers. The prompt helper was not exported
  by the agent package or documented as an API, but external direct imports
  will need updating. Trace consumers relying on the retired events or the
  `final_llm_call` label also need updating; historical JSONL files are untouched.
- Graph nodes/edges, report schema, State, and checkpoint contracts are unchanged.
  `final_report` still retains raw reasoning for state compatibility; only
  `final_output` is the evidence-based report. Finalize preserves `error`,
  `timeout` and `max_iterations` instead of turning report assembly into success.
  `finished` means the reasoning loop completed normally; handled tool errors or
  missing data may still be present. See the hardening status semantics below.

## Runtime Safety

`AgentConfig` supplies iteration and LLM, tool, and whole-run time budgets.
`STOCKMIND_` environment variables configure their defaults; see
[`.env.example`](../.env.example). A deadline is propagated through run
metadata, so retries, backoff, LLM calls, and new tool calls cannot exceed the
remaining budget. Timeout runs retain validated observations, record a timeout
error, and return a partial or no-data report rather than invent conclusions.

`asyncio.CancelledError` is re-raised; cancellation is not converted into an
ordinary tool failure.

Finalize preserves an existing `timeout` without duplicating errors. It also
checks the remaining run deadline after local assembly and records `run_timeout`
if the budget expired before completion. Safe partial/no-data assembly still
runs after timeout, without starting another LLM or tool call. Synchronous
assembly is not preemptible mid-function; this is not a hard wall-clock bound
on local report construction.

Caller task cancellation and a `CancelledError` reaching the local assembler
are covered by regression tests. A separate fault-injection check found that
the installed LangChain turns a model's internally raised `CancelledError`
inside `_agenerate` into `AttributeError` before it reaches the workflow. The
research-node code is unchanged; this dependency edge case remains outside
the Finalize change and does not establish a cancellation guarantee for every
provider implementation.

## Data Integrity Rules

The default provider is deterministic mock data and always simulated. Optional
yfinance and AkShare providers are external, network-dependent sources that
should be evaluated for the intended market and period.

- AkShare rows with missing, NaN, or non-finite OHLC values are discarded;
  missing prices are never turned into zero. A literal provider zero can remain
  raw source data, but no valid latest close yields an unavailable/error result.
- `total_debt` uses an explicit reported total where available. Otherwise it is
  derived only when both explicit short- and long-term interest-bearing debt
  components are available. Total liabilities and partial debt are not proxies.
- MA, RSI, MACD, annualized volatility, and maximum drawdown require finite,
  strictly positive close prices. Empty, insufficient, zero, negative, NaN, and
  infinite sequences yield unavailable results, never NaN, infinity, or a
  fabricated metric.

The calculation rule is intentionally stricter than raw-source storage: a
literal zero may be preserved as source data but is invalid for price metrics.

## Valuation Semantics and Financial Periods

### DCF scope and assumptions

The existing two-stage DCF now requires an explicit `cash_flow_basis`:

| Basis | Discount-rate assumption | Value and per-share conversion |
| --- | --- | --- |
| FCFF | WACC | Discounted cash flows give enterprise value; equity value = enterprise value − net debt; divide equity by outstanding shares |
| FCFE | Cost of equity | Discounted cash flows give equity value directly; divide by outstanding shares without another debt adjustment |
| Unknown | Unavailable | No valuation is produced |

All bundled providers, including mock, currently leave FCF nature unknown.
Reported FCF or operating cash flow minus capex alone does not establish FCFF
or FCFE. Consequently, their DCF tool returns `value: null`, `status:
unavailable`, and an explanation. Confirmed-basis scenarios are tested with
offline fixtures; this is not a claim that live providers supply strict DCF inputs.

The tool requires annual or TTM cash flow and an explicitly reported outstanding
share count (or explicitly simulated outstanding shares in fixtures). It rejects
unknown, market-cap/price-derived, and weighted-average share bases. FCFF also
requires both debt and cash, with disclosed dates matching the cash-flow end
date. Missing components are never assumed zero. Net cash (negative net debt)
is allowed. FCFE does not fetch or subtract net debt.

The numeric boundary requires positive finite cash flow and shares, a positive
discount rate strictly above terminal growth, projection/terminal growth greater
than −100%, and 1–20 integer projection years. Non-finite inputs, overflow, and
non-positive resulting equity value return unavailable. Rates remain user
assumptions, including the default 8% growth, 10% discount, 3% terminal growth,
and five years. Even a successful result is labelled a simplified scenario;
other equity claims, currency/unit reconciliation, and point-in-time availability
are not comprehensively modelled.

### Periods and provenance

Financial records have additive `period`, `period_end`, `published_at`,
`source_updated_at`, and per-field `field_metadata`. Missing metadata is null
or `unknown`; tool provenance renders missing publication/update metadata as
`unavailable`. `timestamp` remains retrieval time, never a substitute for a
financial period or publication date.

- AkShare retains Eastmoney's selected annual `REPORT_DATE`, and `NOTICE_DATE`
  when explicitly present and unambiguous. Balance sheets are point-in-time
  (`instant`); income and cash flow are annual. Financial sources are correctly
  labelled `akshare/eastmoney`. Baidu market-cap dates and the input dates of
  derived EPS/shares are retained separately from the Sina quote.
- yfinance's generic `info` financial bundle no longer gets a blanket TTM label.
  Explicit trailing-EPS fields retain TTM semantics; unspecified periods and
  dates stay unknown. Mock income/cash flow are simulated TTM without invented
  fiscal or publication dates.
- `get_eps` and P/E share an absence-based selection rule: a zero EPS is kept,
  not replaced with a positive fallback. Income-statement fallback carries that
  statement's source, timestamp, and period. The added `eps` key holds the chosen
  value; the existing `eps_ttm` key is null unless its period is actually TTM.
  Revenue descriptions likewise no longer promise TTM for annual records.
- Valuation results retain the existing metric/value/inputs contract and add
  `input_provenance`, `data_period`, `period_status`, and availability status.
  Unknown period types/end dates produce `unknown`; differing known periods or
  end dates produce `mixed-period`. A current market value divided by annual
  earnings can be useful, but is disclosed rather than called synchronized TTM.
  No TTM series is synthesized. These labels do not verify publication-time
  availability or prevent all historical look-ahead bias.
- EV/EBITDA remains the simplified `(market cap + debt − cash) / EBITDA` ratio,
  excluding minority/preferred claims. FCF yield remains reported FCF / market
  cap, with cash-flow nature disclosed; it is not asserted to be verified FCFE.
  Each input source is retained instead of citing just one contributing record.
  The deterministic report carries these details and warnings forward.

Tool names and arguments and the final report schema are unchanged. The DCF
numeric function adds optional `cash_flow_basis`/`net_debt` keywords, but legacy
calls without a confirmed basis now return `None`. This and null annual
`eps_ttm` are intentional corrections to previously misleading semantics.

## Dependency Installation

`pyproject.toml` is the source of dependency versions. Core dependencies remain
unchanged. Extras separate tests (`dev`: pytest), persistence (`sqlite`:
aiosqlite and langgraph-checkpoint-sqlite), UI (`web`: Gradio), and financial
providers (`real-data`: yfinance and AkShare).

```bash
pip install -e .                 # core only
pip install -e ".[dev,sqlite]"   # full offline test suite and checkpoint example
pip install -e ".[web]"          # Gradio UI
pip install -e ".[real-data]"    # optional financial providers
```

The SQLite minimum versions, aiosqlite 0.22.1 and langgraph-checkpoint-sqlite
3.1.3, match the existing environment used for validation. The `dev` extra alone
does not install SQLite; full-suite setup explicitly selects both groups.
`requirements.txt` remains a repository-root deployment entry point containing
`.[web]`, without duplicated package versions. It requires the complete project,
including `pyproject.toml`; it is not a standalone dependency manifest.

The Web UI always constructs the scripted model. API keys do not change its mode.
It defaults to Mock data, but administrator provider settings still apply;
reports retain source labels. Real LLM usage is through `examples/research.py`
with a matching provider, model, and API key. Provider fees may apply. Browser
interaction and external hosting require separate validation.

## Async, Checkpoints, and Tracing

The public API is async-first (`ainvoke`/`arun`), with sync wrappers for simple
scripts. The default checkpointer is in memory. SQLite checkpointing is
available through `app.graph.checkpoint.build_checkpointer`. Traces are kept in
memory unless `STOCKMIND_TRACE_DIR` enables JSONL output.

```bash
python examples/async_research.py --ticker NVDA --lang en
```

The bundled async example explicitly uses Mock tools and a Scripted Demo model,
even when API keys or real-data provider settings are present. Each invocation
gets a fresh model and Agent; both share the SQLite saver and thread ID. This
avoids reusing an exhausted one-shot iterator while retaining checkpoint messages.
It demonstrates two successful scripted research runs, not autonomous follow-up
reasoning or resumption from an interrupted graph node. The model itself remains
one-shot and normal error handling is unchanged.

## Evaluation and Prompt Journal

### Audit of the previous evaluator

The existing sequential Runner, ticker-to-agent factory, `arun()` state, and
`TraceCollector` were reusable. Three unversioned questions already existed,
but had no case IDs, scenario definitions, or output constraints. A supplied
real agent could be executed, but the old CLI automatically enabled real LLMs
when credentials were present; its default demo did not select tools dynamically.

Only latency and available token metadata were actual execution measurements.
Cost used approximate pricing. “Tool selection accuracy” was recall alone and
counted requested/observed tools, including rejected requests. Grounding only
checked for a nonempty source list. Numerical accuracy checked price alone and
passed when the provider was absent or lookup failed; it could fetch data again
during scoring. Quality measured populated sections, even when they said
unavailable. Report existence, finished status and a nonempty summary were not
sufficient completion criteria. These optimistic proxies are no longer reported.

### Dataset and execution

[questions.json](../app/evaluation/datasets/questions.json) is now the validated
`stockmind-eval-v1` object, with 12 cases and a `synthetic-v1` fixture version.
Each case declares a unique ID, question, ticker, scenario, expected executed
tools, output constraints, and a separate fixed script. Scripts are not generated
from expected-tool labels during evaluation. Names and arguments are checked
against the existing tool registry and Pydantic input schemas.

| Cases | Coverage |
| --- | --- |
| price-nvda, trend-amd | Current price and fixed historical window |
| technicals-tsm, risk-amd | MA/RSI/MACD and volatility/drawdown |
| financials-nvda, valuation-tsm | Statements, P/E, simplified EV/EBITDA, reported FCF yield |
| combined-amd | Price, income, valuation and news together |
| missing-financials, news-failure, no-price-data | Missing income/cash-flow figures, failed news, failed sole price source |
| wrong-tool-ticker, wrong-observation-ticker | Blocked cross-ticker request, rejected cross-ticker evidence and unsupported LLM claims |

[fixtures-v1.json](../app/evaluation/datasets/fixtures-v1.json) contains explicitly
synthetic values/dates for NVDA, AMD and TSM. The evaluation-only adapter feeds
these through existing production tools; it changes no production provider or
formula. History always contains the same 40 synthetic daily bars, not a live
lookback window. This dataset does not cover every tool or DCF scenario.

`Evaluator()` uses a fresh scripted model, explicit fixture tools, in-memory
checkpointer and tracer for every case, and runs cases and scripted calls in
fixed order. It does not load `.env`, mutate environment settings, select an
external provider, or enable network tracing. No new tracing implementation was
added. Uncaught per-case exceptions produce failed results and the batch
continues; cancellation propagates. Factory callers remain responsible for
fresh agents and data/model access; injected factories are labelled
`custom-factory-unverified`, never `offline-scripted`.

### Metrics and denominators

- **Tool selection:** let E be unique expected names and A be unique names in
  actual `tool_call` trace events. Precision = |E ∩ A| / |A|, Recall = |E ∩ A| / |E|,
  F1 = 2PR / (P + R). Both empty yields 1/1/1; exactly one empty yields 0/0/0.
  Duplicate attempts do not change set scores. Failed executions count as tool
  attempts; rejected requests do not. Requested names are output separately.
  These scores do not assess arguments, autonomous reasoning or financial truth.
- **Task completion:** requires a report, `finished`, passing evidence checks,
  execution of required tools, and all declared output/error constraints.
  Constraints include finite required metrics, expected fixture values (relative
  tolerance 1e-9 / absolute tolerance 1e-12), absent metrics, available/unavailable
  sections, exact expected error count, error text, and forbidden claims. Extra
  tools reduce precision separately. A no-data result does not pass an ordinary
  price task, but can pass an explicitly specified safe-failure task.
- **Evidence grounding:** reuses the workflow's grounded/no-data helper and
  compares the report against assembly from that run's observations. Ticker,
  full metrics/provenance, narrative fields and finite numbers are checked;
  only `generated_at` is excluded. Unsupported values or prose fail. Honest
  no-data output can pass. This is deterministic output consistency, not an
  independent oracle: shared assembler bugs or inaccurate source data can pass.
- **Execution:** tool call count counts recorded `tool_call` attempts; request
  count includes blocked/duplicate requests. Error count is the state error list
  length, avoiding duplicate trace errors. Latency uses the measured `run_end`
  trace duration; failures without it use Runner elapsed time including setup,
  with `latency_source` disclosed. Counts are derived from existing records;
  they are not independent instrumentation of every provider request. In
  particular, the existing trace may omit an in-flight deadline cancellation.
- **Aggregation:** unweighted arithmetic mean of per-case Precision, Recall and
  F1; completion/grounding rates divide passing cases by **all cases**, including
  Runner failures and safe-failure scenarios. Latency uses the same denominator;
  tool/error totals are sums. No case is silently removed from the denominator.

### Running and reproducing the baseline

```bash
python examples/evaluate.py --output /tmp/stockmind-evaluation.json
python examples/evaluate.py --lang zh   # JSON to stdout; no .env loading
```

JSON includes dataset/fixture versions and hashes, runtime package versions,
case IDs, expected/actual/requested tools, trace-derived calls, report,
observations, all checks, execution metrics, failure reasons and aggregation
rules. Dataset JSON files are included as package data; no dependency was added.
Input values, ordering and decisions are reproducible in the recorded runtime.
Latency, report generation times and observation retrieval timestamps vary;
raw JSON is not promised to be byte-identical. Tests compare stable scores,
counts and reports excluding generation time across two runs.

Compatibility: the unversioned question-list API is replaced by `load_dataset()`
and `EvaluationDataset`. `Evaluator(agent_factory)` still accepts a ticker-to-agent
factory, but consumes the new dataset model. `RunMetrics`/`EvaluationReport` now
use serializable Pydantic models. Old quality/numerical-accuracy/recall-labelled-
accuracy helpers are removed; consumers should read the explicit checks and
`aggregate` fields. The CLI never auto-enables live APIs and has no live-mode flag.

P2-A validation (2026-11-17, existing Python 3.12.8 / pytest 9.2.0):

- `.venv/bin/python examples/evaluate.py --output /tmp/stockmind-p2-a-baseline.json`:
  12/12 completed, 12/12 grounding-consistent; macro P/R/F1 = 1.0;
  25 recorded tool attempts, 3 expected scenario errors, all final statuses
  `finished`. The final measured run averaged 11.32 ms per Agent run; this is a local
  timing observation, not a performance benchmark or cost claim.
- `.venv/bin/python -m pytest tests/test_evaluation.py -v`: **34 passed**.
- `.venv/bin/python -m pytest -v`: **182 passed, 0 failed, 0 skipped** (13.42 s).
  One warning concerned sandbox permissions for the pytest cache.
- `.venv/bin/python -m compileall -q app tests examples`: passed after permission
  was granted for bytecode writes; the initial sandbox attempt was denied.
- `git diff --check`: passed.

Tests include schema rejection, empty/duplicate tool sets, extra/missing tools,
independent repeated runs, hostile environment settings with network access
blocked, corrupted report evidence, no-data completion rules, failed cases in
the denominator, cancellation and JSON output. The original Workflow tests
remain in the full suite. High scripted scores validate plumbing and rules;
they do not measure real LLM tool choice, research quality, live provider
reliability or financial correctness. That P2-A baseline contains no LLM-as-Judge;
the separate reliability extension is described below.

### Reliability Evaluation

`stockmind-reliability-v1` adds 12 cases in
[`reliability-v1.json`](../app/evaluation/datasets/reliability-v1.json), using the
same `synthetic-v1` fixtures. Each case retains the baseline query, ticker,
expected tools, executable script and output constraints, and adds a failure
injection, expected terminal status/handling, recovery rule, safe-termination
eligibility and expected tool-attempt count. Expected behavior is a test policy,
not a claim that production already supports it.

Audit of existing protections:

- Tool exceptions and tool timeouts already produce trace errors and bounded
  retries; the shared run deadline bounds attempts and backoff. Schema validation
  rejects malformed arguments. Wrong ticker and unknown tool requests are rejected
  before tool execution. Duplicate signatures from earlier steps are skipped.
- LLM errors retain the error and accumulated observations; LLM/run timeouts keep
  terminal `timeout`. Finalize uses only verified observations and returns partial
  or no-data output. These production mechanisms were reused, not reimplemented.
- `arun()` initializes a new run. Native compiled-graph `interrupt_after` and
  `ainvoke(None)` can continue a paused checkpoint. A terminal graph at END has
  no pending node to retry. P2-A did not measure fault activation, recovery or
  unsupported behavior; it only provided reusable traces and contract checks.

The new `ReliabilityEvaluator` extends the existing sequential runner through two
small construction/invocation hooks. `FaultModel` changes selected tool requests
or raises a declared model fault; an evaluation-only fixture subclass raises a
provider error; evaluation-local tool wrappers retain the production tool schema
and inject missing data, foreign-ticker data or a timeout. Original tools still
execute through the unchanged LangGraph workflow. No global provider patch is
used by the runner. Each case gets a fresh model, fixture, journal, trace collector
and in-memory saver. Existing `TraceCollector` events supply execution evidence;
the journal records injection and checkpoint actions, not a second tracing system.

Timeout faults wait on a cancellable `asyncio.Event`, never a long sleep. The
evaluation configuration uses 0.05 s per operation, 2 s per run and a 5 s case
watchdog, with one retry only for the two retry cases and no backoff/jitter.
Production timeout defaults are unchanged. Cancellation propagates; tests verify
that cancellation reaches the waiting fault and that no subsequent news tool
starts after an LLM timeout. Environment credentials/provider settings cannot
select a live model/provider; telemetry is disabled and `.env` is not loaded.

Metric definitions and output:

- P2-A metrics remain unchanged. Tool P/R/F1 compares executed tool-name sets;
  attempt counts separately expose duplicates and retries. Error count is the
  length of State errors; `observed_behavior.errors` also retains trace errors
  from retries that subsequently succeeded. Latency is measured, not a label;
  checkpoint latency includes the isolation probe, whose tool calls use a
  separate trace and are not counted as primary-thread calls.
- Reliability checks require an observed injection, expected status and handling,
  all output constraints and grounding, exact tool-set/attempt expectations,
  no runner failure, no safety violation and the declared recovery behavior.
  The P2-A `finished` completion condition is retained in baseline fields but
  excluded from reliability output constraints: safe timeout can pass reliability
  while `task_completed` remains false. Baseline completion alone is not success.
- Safe Termination Rate = passing eligible cases / all cases labelled
  `safe_termination_required`. Both checkpoint recovery probes are outside this
  denominator. Duplicate execution fails the declared at-most-once condition.
- Recovery Rate = passing recovered cases / all cases declaring a recovery
  expectation. Retries require a later successful attempt; corrected requests
  and partial-report recovery require a successful later-step tool plus the
  constrained grounded report. Checkpoint recovery requires actual read/resume,
  unchanged saved observations and thread isolation. Catching an error is not
  recovery. Unsupported recovery stays in this denominator and never its numerator.
- Both rates expose numerator, denominator and unsupported count; a zero denominator
  yields `null`. Results distinguish normal completion, safe degradation,
  controlled failure, unexpected failure and unsupported. The last two are not
  counted as success. No weighted total score is used.
- Safety findings include category, field, expected/observed values and inspectable
  evidence. Numeric/report consistency reuses the production grounded/no-data
  assembler. Wrong report/state ticker, unsupported values (including nested
  metrics), unsupported claims in unavailable sections and `finished` after an
  unrecovered last reasoning error are checked. This is template/contract
  consistency, not general free-text fact checking or independent financial truth.
- JSON retains all P2-A fields and adds per-run `reliability` and a top-level
  `reliability_aggregate`. `failure_reasons` inside `reliability` explains its
  verdict; baseline failure reasons remain separate. Injection labels and expected
  behavior are kept separate from actual trace errors, status and recovery evidence.

P2-B pre-hardening results (2026-11-17, existing Python 3.12.8 environment):

| Case ID | Verdict | Observed behavior |
| --- | --- | --- |
| `provider-retry` | pass | First attempt throws; retry obtains verified price |
| `tool-timeout-retry` | pass | First attempt cancelled; retry succeeds |
| `invalid-arguments` | pass | Invalid RSI argument rejected; later price succeeds |
| `wrong-ticker-request` | pass | AMD request blocked in NVDA run; corrected call succeeds |
| `duplicate-same-batch` | fail | Two identical requests execute twice, expected once |
| `unknown-tool-request` | pass | Unknown tool rejected; valid subsequent call succeeds |
| `llm-exception-partial` | fail | Reasoning error retained, but final status is `finished` |
| `llm-timeout-partial` | pass | Terminal `timeout`, verified price retained, no news call |
| `invalid-observation-partial` | pass | Foreign price rejected; valid income report retained |
| `missing-data` | pass | Empty payload produces the existing no-data report |
| `checkpoint-paused-resume` | pass | New Agent resumes same saver/thread; other ticker isolated |
| `checkpoint-terminal-retry` | unsupported | END replay retains timeout report, zero new model calls |

Totals: **9 pass / 2 fail / 1 unsupported**; safe termination **8/10 = 80%**;
recovery **7/8 = 87.5%**, including one unsupported denominator case. Outcomes are
1 normal completion, 7 safe degradations, 1 controlled failure, 2 unexpected
failures and 1 unsupported. There is **1 safety finding**, the terminal-status
ambiguity; no unsupported financial value or ticker contamination entered these
12 reports. Grounding is 12/12, tool-set macro P/R/F1 is 1.0, baseline completion
is 10/12, with 17 tool attempts and 6 State errors. These values intentionally do
not hide the two reliability failures.

Production behavior observed at the P2-B baseline (fixed in the section below):

1. `workflow.py` checks `existing_signatures` from prior State but only appends
   current-batch signatures to `new_signatures`. The second same-batch call
   therefore enters the job list too. The trace contains two successful attempts.
2. The reasoning exception branch writes `status="error"`; Finalize normalizes
   every non-timeout status to `finished`. The error remains visible and the report
   remains grounded. P2-B's stricter terminal-status policy flags this as
   `failure_reported_success`; this is not evidence of fabricated financial data.

Checkpoint coverage uses only the in-process memory saver. It creates a genuine
pause after tools, runs AMD on a different thread, creates a new NVDA Agent with
the same saver, verifies saved state and resumes with `ainvoke(None)`. It does not
call a new `arun()` and relabel it recovery. A completed timeout has `next == ()`;
observed replay returns the same report/status without executing the new model,
so automatic retry from END is unsupported. A simulated checkpoint read failure
is separately tested to yield a failed evaluation with no invented report.
Cross-process recovery, durable database failures, restoring real LLM internal
state, long pauses/deadline renewal and cancellation of synchronous network
threads are not validated. The resumed fake model is explicitly constructed for
the pending node, not restored model memory. That evaluation-only stage included
no production fix or P2-C work.

Validation commands and actual results:

- `.venv/bin/python examples/evaluate.py --output /tmp/stockmind-p2-b-baseline.json`:
  original 12/12 cases completed and grounded; 25 attempts, 3 expected State errors.
- `.venv/bin/python examples/evaluate.py --suite reliability --output /tmp/stockmind-p2-b-reliability.json`:
  **9 pass / 2 fail / 1 unsupported**, including failure reasons and evidence.
- `.venv/bin/python -m pytest tests/test_evaluation.py tests/test_reliability_evaluation.py -v`:
  **76 passed** (2.43 s), including 42 new tests.
- `.venv/bin/python -m pytest -v`: **224 passed, 0 failed, 0 skipped** (15.33 s).
  One sandbox pytest-cache permission warning; no tests skipped or weakened.
- `.venv/bin/python -m compileall -q app tests`: passed after permission for
  bytecode writes; the initial attempt failed with sandbox `PermissionError`.
- `git diff --check`: passed. No dependencies installed or upgraded.

The two evaluation failures are asserted by passing regression tests; they are
not pytest failures. Inputs and decisions are reproducible in the recorded
runtime; latency and timestamps vary. Short simulated timeouts are not a real
provider latency benchmark. The shared assembler oracle can miss its own bugs.
The original CLI default remains P2-A; select `--suite reliability` for the new
schema. Existing dependency package-data rules already include the new JSON.
The CLI exits 0 when it successfully writes the complete evaluation result, even
with fail/unsupported cases; CI must inspect `reliability_aggregate.verdict_counts`.
That evaluation-only stage did not change report/State/Checkpoint contracts or
production execution code.

### Reliability Hardening (P2-B Fix)

Preflight confirmed commit `7081acb` contains P2-B and the worktree was clean.
The original 12-case dataset, failure injection and scoring rules are unchanged.
Only the two exposed production defects and necessary status/trace consumers
were adjusted; no provider, financial formula, report schema or graph structure
was changed.

The duplicate bug checked only signatures from previous State. The fix inserts
each new signature into the node-local seen set immediately, before examining
the next request. It uses the existing `tool_call_signature` canonical JSON
(sorted object keys, including nested objects), preserving tool names and argument
values. Different tool names, ticker values or arguments are not merged. A
different ticker still needs its own valid single-stock run; wrong-ticker requests
within a run remain blocked. No global cache or cross-thread state is introduced.
New runs start fresh; existing prior-step suppression remains in place.

Skipped requests retain their ToolMessage and requested-call record and emit a
`tool_skipped` trace with the name, arguments and duplicate reason. The message
says "already requested", not "already executed", because the first request
may still be queued or may fail. These events are not `tool_call` attempts, errors
or observations. CLI trace summaries and Web progress display the skip. Each
distinct invocation still uses the unchanged retry, timeout and error path;
skipping a duplicate never replaces the first invocation's bounded retry.

The status bug occurred after the LLM handler had already set `error`: Finalize
replaced all non-timeout states with `finished`. Finalize now preserves the node's
status while assembling the same evidence-only report. The existing iteration-cap
status is retained too, so hitting a limit is not presented as normal completion.
The existing expired-deadline check still takes precedence and records `run_timeout`.
The LLM error trace now also explicitly carries `final_status="error"`.

| Status | Meaning after Finalize |
| --- | --- |
| `finished` | Normal reasoning-loop completion; handled tool errors or unavailable data may remain |
| `error` | Unrecovered LLM exception; verified observations and a partial/no-data report are retained |
| `timeout` | LLM/run deadline termination; only verified evidence is reported |
| `max_iterations` | Iteration cap reached; safe report available, not normal completion |

State already stored status as a string; no enum, field or report-schema change
was needed. Its comment now includes timeout. CLI summaries, Agent `run`/`arun`,
checkpoint serializers and Evaluation already consume the returned status.
`invoke`/`ainvoke` still return a safe report without changing their API; consumers
needing execution status should use `run`/`arun` or inspect the trace/checkpoint.
P2-A completion correctly requires `finished`; Reliability can pass a controlled
failure while baseline task completion is false. Its safety check was retained.
The Web formatter now distinguishes failure, timeout and iteration limits from
successful completion; the final report-ready message no longer uses a success
checkmark. UI layout and research behavior are unchanged.

| Reliability case | Before | After |
| --- | --- | --- |
| `provider-retry` | pass | pass |
| `tool-timeout-retry` | pass | pass |
| `invalid-arguments` | pass | pass |
| `wrong-ticker-request` | pass | pass |
| `duplicate-same-batch` | fail: two executions | pass: two requests, one execution, one skip |
| `unknown-tool-request` | pass | pass |
| `llm-exception-partial` | fail: finished | pass: error with verified partial report |
| `llm-timeout-partial` | pass | pass |
| `invalid-observation-partial` | pass | pass |
| `missing-data` | pass | pass |
| `checkpoint-paused-resume` | pass | pass |
| `checkpoint-terminal-retry` | unsupported | unsupported |

Actual results: **9/2/1 → 11/0/1 pass/fail/unsupported**. Safe Termination Rate
**8/10 → 10/10 (100%)**; Recovery Rate stays **7/8 (87.5%)**, including the one
unsupported recovery in its denominator; Safety Violation Count **1 → 0**.
Outcomes are 2 normal completions, 7 safe degradations, 2 controlled failures and
1 unsupported; no unexpected failure. All 12 reports remain grounding-consistent.
Tool attempts drop from 17 to 16 and State errors stay 6. P2-A-style completion
within this suite drops from 10/12 to 9/12 because the LLM exception is now honestly
marked `error`; this is not a grounding or recovery regression. Original P2-A
remains 12/12 completed, 25 tool attempts, 3 expected errors and macro P/R/F1=1.0.

Validation on 2026-11-17, existing Python 3.12.8 / pytest 9.2.0:

- Before/after CLI: `.venv/bin/python examples/evaluate.py --suite reliability
  --output /tmp/stockmind-p2-b-fix-before.json` and the same command with
  `/tmp/stockmind-p2-b-fix-after.json`: **9/2/1** and **11/0/1**, respectively.
- `.venv/bin/python examples/evaluate.py --output /tmp/stockmind-p2-b-fix-baseline.json`:
  original 12 P2-A cases completed and grounded.
- `.venv/bin/python -m pytest tests/test_reliability_evaluation.py tests/test_reliability_hardening.py tests/test_workflow.py tests/test_finalize.py tests/test_phase3.py tests/test_evaluation.py -v`:
  **135 passed** (2.85 s).
- `.venv/bin/python -m pytest -v`: **246 passed, 0 failed, 0 skipped** (15.51 s).
  One sandbox pytest-cache permission warning.
- `.venv/bin/python -m compileall -q app tests`: initial sandbox bytecode-write
  permission failure; `.venv/bin/python -m compileall -q app tests web` passed
  after permission, also checking the modified Web code.
- `git diff --check`: passed. No dependency installation or upgrade.

The 22 new tests cover signature order/distinctions, tools-node and full Workflow
dedupe, retries, separate tickers/threads/new runs, partial/no-data LLM failure in
memory and SQLite `:memory:` checkpoints, terminal statuses versus deadlines,
and bilingual Web formatting. Existing tests now assert the corrected behavior;
reverting either fix would fail them. No cases or safety assertions were removed.
Gradio is not installed: Web checks executed the real pure formatter via AST,
without importing the UI, loading `.env`, starting a browser or claiming a browser
test. Network access and real model/provider selection are blocked in new tests.

Remaining limits: END timeout checkpoint replay still cannot retry failed research;
the graph has no pending node. This round does not add recovery, alter fingerprint
semantics beyond the existing canonical JSON, test every dependency combination,
or establish real-LLM reliability. Different omitted/default arguments or numeric
representations are not newly normalized. The shared assembler oracle and prior
cross-process/cancellation limitations still apply. Existing consumers that treated
any report as success must inspect status; trace consumers may now see
`tool_skipped`. No P2-C work or commit/push was performed.

### Reproducible Offline Benchmark (P2-C)

The preflight worktree was clean at `22a6d50`. The existing P2-A `EvaluationReport`
and P2-B `ReliabilityReport` already serialize per-case reports, observations,
traces, checks and aggregates. The existing CLI supported baseline/reliability
JSON. P2-C adds orchestration and presentation, not a new scorer: `benchmark.py`
runs the unchanged evaluators sequentially; `report_generator.py` renders the
serialized result. Dataset, fixtures, fault injection, production Workflow,
financial logic and report schema are unchanged. No dependency was added.

```bash
python examples/evaluate.py --suite benchmark --output-dir benchmark-results
python -m app.evaluation.report_generator benchmark-results/evaluation-results.json --output benchmark-results/evaluation-report.md
```

The first command writes `evaluation-results.json` and `evaluation-report.md`.
The second only reads saved JSON; it never runs the Agent. The default directory
is `benchmark-results/`, ignored by Git. Custom output directories are supported;
keep them outside version control or add an appropriate ignore rule. These files
are generated locally, not a committed published report. Repeated runs replace
these two artifact files. Existing baseline/reliability commands, JSON-to-stdout,
`--dataset` and `--output` remain supported. Benchmark uses both bundled datasets
and rejects ambiguous `--dataset`/`--output`; `--output-dir` is Benchmark-only.
`--lang` changes underlying research-report language; the audit Markdown uses
English headings and stable machine field names.

Each existing runner constructs fresh models, fixtures, traces and in-memory
checkpoints per case. Only checkpoint probes intentionally share a saver within
one case. Explicit scripted/fake models and fixtures ignore live provider settings,
no `.env` is loaded, and telemetry stays disabled. No financial/LLM API is called.
Source metadata runs local `git rev-parse`/`git status` only, without reading
remotes or credentials; outside a Git checkout it reports unavailable metadata.

`stockmind-benchmark-v1` JSON contains:

| Field | Meaning |
| --- | --- |
| `benchmark_version`, `started_at`, `generated_at`, `elapsed_seconds` | Version, UTC execution/generation times and measured orchestration duration |
| `source_revision` | Git commit and dirty-worktree flag; not a claim the current uncommitted code equals HEAD |
| `scope` | Two suites, actual combined case count, fake models, synthetic fixtures and checkpoint scope |
| `baseline`, `reliability` | Complete existing suite JSON, including dataset/fixture hashes, runtime versions and raw per-case evidence |
| `baseline_counts` | Counts of existing task_completed and grounded booleans for ratio display |
| `baseline_error_accounting` | Dataset error expectations, observed State errors, recorded tool errors and existing failed-case IDs |
| `metric_definitions` | Definitions, JSON source paths and denominators |
| `historical_comparison` | Explicitly labelled historical transcription with source commit/path/section |
| `status` | completed, completed_with_unsupported or completed_with_failures |

Current score tables read these fields; no current score is hardcoded. All cases
retain their IDs and terminal states in both outputs. Fail/unsupported cases
include reasons, and failed cases include their actual error text. The Markdown
omits long machine traces; JSON retains them. Rates/latencies are rounded for
display, while JSON retains original precision. Rendering saved JSON again
produces the same Markdown; rerunning the benchmark does not promise identical
timings or timestamps.

Metric semantics remain those of the existing evaluators:

- P2-A task completion requires all completion rules, not just a report. Grounding
  is consistency with the production assembler, not independent financial truth.
  Tool scores are unweighted macro set P/R/F1 over every case, not autonomous
  planning skill. No arbitrary weighted aggregate is introduced.
- Actual tool errors count `actual_calls` entries with an error; State errors
  include blocked requests too. Expected tool-error text matches declared dataset
  patterns; matching does not itself prove case completion. Unexpected failure
  IDs come from existing false completion/grounding or failure_reasons. In the
  current baseline there are 3 expected/observed State errors but only 2 executed
  tool error attempts: wrong ticker was rejected before execution.
- Safe termination remains passing eligible cases / all eligible cases. Recovery
  remains passing recovered cases / all declared recovery cases, including
  unsupported. Numerators, denominators and unsupported counts are copied from
  P2-B, not recalculated with a different inclusion policy. Zero denominator
  remains null; safety findings retain their inspectable evidence.
- `status` is completed_with_failures if either suite has existing unexpected
  failures; otherwise completed_with_unsupported if applicable, else completed.
  CLI exit 0 means artifacts were written, not that every case passed. CI must
  inspect status and suite verdicts. Expected controlled failures are not newly
  relabelled as unsuccessful reliability checks.

Historical provenance: no historical raw evaluation JSON is tracked in the
repository. Both the historical Before (9/2/1, safe termination 8/10, recovery 7/8,
one violation) and historical After Fix (11/0/1, 10/10, 7/8, zero violations) are
transcribed from `docs/codex-log.md`, section `2026-11-17 — P2-B Fix：Agent Reliability
Hardening`, at commit `22a6d5039547a1d102f9e29ebd48c4f4c96b2292`. They describe
same-batch duplicate execution and LLM-error status overwriting. Neither historical
column is recomputed against today's Workflow. The current measured suite is
reported separately, even if it regresses or disagrees with the historical After.
No `/tmp` historical artifact is required for the new Benchmark to work.

Actual validation (2026-11-17 America/Los_Angeles; report timestamps are UTC):

- `.venv/bin/python examples/evaluate.py --suite benchmark --output-dir /tmp/stockmind-p2-c-benchmark`:
  24 cases, status **completed_with_unsupported**. P2-A **12/12 completion and
  grounding**, macro P/R/F1 **1.0**, 25 attempts, 3 expected State errors,
  2 expected tool error attempts, 0 unexpected failures. P2-B **11 pass / 0 fail /
  1 unsupported**, safe termination **10/10**, recovery **7/8**, safety violations
  **0**. The final measured orchestration took 0.4523 s; not a benchmark claim
  about other machines or real services.
- `.venv/bin/python -m app.evaluation.report_generator /tmp/stockmind-p2-c-benchmark/evaluation-results.json --output /tmp/stockmind-p2-c-benchmark/evaluation-report-regenerated.md`:
  Markdown byte-identical to the original report.
- `.venv/bin/python -m pytest tests/test_benchmark.py tests/test_evaluation.py tests/test_reliability_evaluation.py -v`:
  **93 passed** (5.01 s), including **17 new tests**.
- `.venv/bin/python -m pytest -v`: **263 passed, 0 failed, 0 skipped** (final requested rerun: 17.69 s; initial full run: 18.25 s).
  One warning concerns sandbox permissions for pytest cache writes.
- `.venv/bin/python -m compileall -q app tests examples`: passed after permission
  to write bytecode; the initial sandbox attempt failed with PermissionError.
- `git diff --check`: passed. Outputs reside in `/tmp`, outside the repository.

Tests also deliberately fail baseline and reliability case construction to verify
that failed cases, actual errors and unsupported recovery remain in both files;
they perturb report inputs to detect hardcoded scores, check denominator/source
labels, repeat execution, block external connections/provider selection, check
custom/default directories, regenerate without Agent calls and retain old CLI
behavior. The first targeted run exposed missing concrete error text in Markdown
(92 passed, 1 failed); the renderer was corrected, without changing scoring.

This is not a real-LLM benchmark, live-market accuracy validation, independent
financial fact check, complete real-Provider fault test or evidence of suitability
for automatic trading/investment decisions. END timeout checkpoint recovery remains
unsupported. Runtime load, short simulated deadlines, dependency versions and dirty
worktrees limit reproduction; input hashes and package versions are recorded but
not all dependency combinations are tested. No P3 or commit/push is included.

### Prompt journal

`promptlog/` is a separate local prompt-recording utility:

```bash
python -m promptlog add "your prompt" --tag finance
python -m promptlog list --limit 20
python -m promptlog export
```

See [promptlog/README.md](../promptlog/README.md) for storage and export
details.

## Testing

The suite uses offline fixtures and mocks for agent, provider, timeout,
report-assembly, and calculation behavior. It does not require a real LLM,
financial API, or API key.

```bash
python -m pytest
```

Optional-provider integration behavior remains dependent on the installed
package, network availability, and upstream response format.

P1-D offline validation (Python 3.12.8, existing `.venv`):

- Pre-change baseline: 83 passed.
- Targeted calculation/provider/valuation tests: 85 passed.
- `.venv/bin/python -m pytest -v`: **133 passed, 0 failed, 0 skipped**.
  One sandbox warning concerned pytest cache write permissions, not a test failure.
- `.venv/bin/python -m compileall -q app tests`: passed after permission was
  granted to write `__pycache__`; the first sandbox attempt failed on permissions.
- `git diff --check`: passed.

The 50 added cases cover FCFF versus FCFE, debt/share evidence, invalid rates
and numeric boundaries, annual/TTM and unknown periods, EPS fallback and zero
EPS, normal ratios, retained report warnings, and offline provider metadata.
Existing DCF smoke tests were updated to require explicit semantics or expect
unavailable for mock FCF. No real financial API or paid LLM was called.

Cleanup-B offline validation (2026-11-16, existing Python 3.12.8 environment;
no packages installed or upgraded):

- `.venv/bin/python -m pytest tests/test_cleanup_b.py -v`: **6 passed**.
- `.venv/bin/python -m pytest -v`: **139 passed, 0 failed, 0 skipped**.
  One warning concerned sandbox permissions for the pytest cache.
- `.venv/bin/python -m compileall -q app tests examples web`: passed after
  permission was granted to write bytecode; the first attempt hit sandbox limits.
- `git diff --check`: passed.

The six new cases check SQLite imports and backend operation, two complete
scripted runs for AMD/English and NVDA/Chinese (15 tool calls per run, no errors,
shared checkpoint history), the model's one-shot error, and bilingual Web copy.
The async tests explicitly reject attempts to select an external data/LLM
provider. Web copy is checked statically without importing or starting Gradio.

Dependency declarations were parsed and checked against installed core, dev,
and SQLite versions; pip parsed the local `.[web]` requirements entry. This is
not a fresh-install or deployment test. Gradio, yfinance, and AkShare are absent;
browser interaction and real-provider integration were not exercised. All
provider tests use offline fixtures/stubs; no real financial API or paid LLM
was called.

Finalize Optimization validation (2026-11-16, existing Python 3.12.8 / pytest
9.2.0 environment; no dependency changes):

- `.venv/bin/python -m pytest tests/test_finalize.py tests/test_workflow.py tests/test_phase3.py tests/test_language.py -v`:
  **41 passed**.
- `.venv/bin/python -m pytest -v`: **151 passed, 0 failed, 0 skipped** (16.71 s).
  One warning concerned sandbox permissions for the pytest cache.
- `.venv/bin/python -m compileall -q app tests web`: passed after permission
  was granted to write bytecode; the first attempt hit sandbox limits.
- `git diff --check`: passed.

Twelve added cases cover native tool binding and actual research LLM call counts,
zero extra Finalize calls, token accounting, report/observation ticker mismatch,
untrusted JSON/plain text, no-data and assembly failure, expired deadlines,
existing timeout status, caller cancellation, local cancellation propagation,
checkpoint completion, and bilingual progress formatting. Prior tests for
candidate rejection, Finalize LLM timeout, and the Chinese Finalize prompt now
verify the replacement behavior instead of being deleted. The first targeted
run had one failing internal-cancellation injection; the caller-cancellation
test was corrected to use `Task.cancel()`, and the dependency behavior is
disclosed above. No production cancellation handling was weakened.

Web formatter tests execute only the pure formatter; Gradio/browser operation
remains unverified. No real financial API or paid LLM was called, and no cost or
latency benchmark is claimed.
