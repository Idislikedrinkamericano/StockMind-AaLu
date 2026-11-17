# StockMind Engineering Notes

[简体中文](engineering-notes.zh-CN.md) · [Project home](../README.md)

This page keeps contributor-oriented implementation details out of the project
home page.

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
  `final_output` is the evidence-based report. Existing non-timeout runs still
  close as `finished` after report assembly, with prior errors retained; this
  status does not guarantee that every data request succeeded.

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
reliability or financial correctness. No LLM-as-Judge or P2-B/P2-C work is included.

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
