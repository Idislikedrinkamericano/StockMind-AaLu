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
