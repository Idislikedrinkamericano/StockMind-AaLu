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

`app/reporting/assembler.py` deterministically builds the final report from
valid observations. The workflow may attempt structured LLM output for tracing
and validation, but candidate LLM reports never replace that result. A wrong
ticker, unsupported metric, raw prose, or a claim without supporting
observations is rejected. With no valid observations, the report explicitly
states that evidence is unavailable.

## Runtime Safety

`AgentConfig` supplies iteration and LLM, tool, and whole-run time budgets.
`STOCKMIND_` environment variables configure their defaults; see
[`.env.example`](../.env.example). A deadline is propagated through run
metadata, so retries, backoff, LLM calls, and new tool calls cannot exceed the
remaining budget. Timeout runs retain validated observations, record a timeout
error, and return a partial or no-data report rather than invent conclusions.

`asyncio.CancelledError` is re-raised; cancellation is not converted into an
ordinary tool failure.

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

## Async, Checkpoints, and Tracing

The public API is async-first (`ainvoke`/`arun`), with sync wrappers for simple
scripts. The default checkpointer is in memory. SQLite checkpointing is
available through `app.graph.checkpoint.build_checkpointer`. Traces are kept in
memory unless `STOCKMIND_TRACE_DIR` enables JSONL output.

```bash
python examples/async_research.py --ticker NVDA --lang en
```

The bundled async example uses the Scripted Demo model. It demonstrates the
workflow and checkpointing, not a live LLM.

## Evaluation and Prompt Journal

`examples/evaluate.py` and `app/evaluation/` provide a local evaluation
harness. The repository publishes no benchmark or performance claim: results
depend on the model, provider, fixtures, and run configuration.

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
