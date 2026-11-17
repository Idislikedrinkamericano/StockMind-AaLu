# StockMind

[简体中文](README.zh-CN.md)

StockMind is a stock-research agent built with **LangChain**, **LangGraph**, and
the **ReAct** pattern. It separates LLM reasoning from data retrieval and
deterministic financial calculations, then produces structured research reports
from provenance-aware observations.

> StockMind is an engineering project and research aid, not investment advice.

## Project Overview

StockMind uses an explicit LangGraph workflow rather than hiding the agent loop
behind a prebuilt abstraction. State, tool observations, execution limits, and
failures are therefore visible and testable.

Final report facts and metrics are assembled only from valid tool observations.
Finalize uses the deterministic assembler directly, with no additional LLM call.
Research-stage LLM reasoning and tool selection remain active; LLM prose or
schema-valid JSON is not evidence on its own.

## Features

- ReAct-style workflow with bounded iterations, tool retries, and LLM, tool,
  and whole-run timeouts.
- Market, financial-statement, news, technical-analysis, and valuation tools.
- Deterministic MA, RSI, MACD, volatility, drawdown, P/E, EV/EBITDA, and FCF-yield
  calculations; DCF scenarios require a confirmed cash-flow and share basis.
- Structured `StockResearchReport` output in English or Simplified Chinese.
- Explicit state, tracing, optional in-memory or SQLite checkpoints, and a
  repeatable offline scripted demo.
- Default deterministic **mock data**, clearly labelled as simulated; optional
  yfinance and AkShare providers for real-data experiments.

## Architecture

```text
User request
    │
    ▼
LangGraph ReAct workflow ──► LLM reasoning / tool selection
    │                                  │
    │                                  ▼
    └────────────────────────── Tools and providers
                                       │
                                       ▼
                         validated observations
                                       │
                                       ▼
                    deterministic report assembler
                                       │
                                       ▼
                        StockResearchReport
```

| Component | Responsibility |
| --- | --- |
| `app/agents` and `app/models` | Agent façade, prompts, LLM providers, scripted demo model |
| `app/graph` | LangGraph state, routing, deadlines, retries, and termination |
| `app/tools` | Provider access and deterministic calculations |
| `app/reporting` | Evidence-grounded report assembly and safe no-data output |
| `app/schemas` | Pydantic contracts for reports and observations |
| `app/observability` | Trace and cost-observation utilities |

The LLM interprets available observations and requests tools. It does not own
financial calculations, and a tool failure becomes unavailable data rather than
a fabricated value.

## Quick Start

Requires Python 3.11 or newer.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,sqlite]"
cp .env.example .env
```

This installs the core package, test tools, and SQLite checkpoint support for
the full test suite. For core-only use, run `pip install -e .`. Optional groups
are `dev` (tests), `sqlite` (persistent checkpoints), `web` (Gradio), and
`real-data` (financial providers), all declared in `pyproject.toml`.

`mock` is the default data provider. It is deterministic and simulated, not
live market data. To enable optional real-data providers, install their
dependencies and set the provider in your local `.env`:

```bash
pip install -e ".[real-data]"
# STOCKMIND_DATA_PROVIDER=yfinance  # or akshare
```

To use a real LLM agent, set `STOCKMIND_LLM_PROVIDER` to `openai` or `anthropic`,
provide that provider's API key, and choose a matching `STOCKMIND_LLM_MODEL` in
`.env`. Provider charges may apply. Do not commit `.env`; it is ignored by Git.

Run the offline test suite with:

```bash
python -m pytest
```

## Example Usage

### Scripted Demo (default without an API key)

```bash
python examples/research.py --tickers NVDA,AMD,TSM --lang zh
python examples/nvda_research.py --lang en
```

Without an LLM API key, these commands use `ScriptedReActDemoModel`. It runs a
**fixed sequence** of tool calls for the supplied ticker so the workflow can be
demonstrated offline. It is not autonomous and does not dynamically choose
tools from the request. Its default results use simulated mock data.

### Real LLM Agent

```bash
python examples/research.py --tickers AAPL --lang en
```

When an OpenAI or Anthropic API key is available, `research.py` creates
`StockMindAgent` with a real chat model. The model can request registered tools
within the workflow's iteration and time limits. Data remains mock unless
`STOCKMIND_DATA_PROVIDER` is changed.

### Async Checkpoint Demo

```bash
python examples/async_research.py --ticker AMD --lang en
```

Requires the `sqlite` extra. Runs two fresh Scripted Demo sequences with explicit
Mock tools and a shared SQLite thread. It preserves checkpoint messages; it does
not demonstrate autonomous follow-up reasoning or interrupted-run resume.

### Scripted Web Demo

```bash
pip install -e ".[web]"
python app.py
```

Open `http://127.0.0.1:7860`. The Web UI uses a fixed tool sequence with Mock data
by default. It has no real LLM mode switch; adding an API key does not enable one.
Use the research CLI above for a real LLM agent. Platforms requiring a
requirements file can run `pip install -r requirements.txt` from the repository
root; that file delegates to `.[web]`.

### Offline Evaluation

```bash
python examples/evaluate.py --output /tmp/stockmind-evaluation.json
```

Runs 12 versioned cases with fixed synthetic fixtures and scripted LLM responses,
regardless of API keys or provider settings. JSON includes tool-set Precision /
Recall / F1, rule-based completion, report/observation consistency, and execution
metrics. These are regression checks, not real-LLM performance or independent
financial accuracy. See the [metric definitions and limits](docs/engineering-notes.md#evaluation-and-prompt-journal).

## Tech Stack

- Python 3.11+
- LangChain Core and LangGraph
- Pydantic
- pandas, NumPy, and matplotlib
- OpenAI or Anthropic integrations (optional)
- yfinance or AkShare data providers (optional)
- pytest

## Current Limitations

- Default runs use simulated mock data; external providers are optional,
  network-dependent, and subject to their source coverage and availability.
- The Scripted Demo is intentionally fixed and is not LLM tool-selection
  capability.
- Real LLM output is bounded by timeouts and validated observations, but its
  qualitative interpretation is not investment advice.
- Missing, non-finite, or incomplete provider data can yield unavailable report
  sections. StockMind does not synthesize missing prices, debt, or indicators.
- Valuations disclose each input's source and period, including unknown or mixed
  periods. Current providers do not establish FCFF/FCFE, so their DCF is
  unavailable; annual EPS fallback is never labelled TTM.
- This project does not provide trading execution, portfolio optimisation, or
  historical backtesting.

## Documentation

- [Engineering notes](docs/engineering-notes.md) — reliability, data integrity,
  async/checkpoint, evaluation, and prompt-journal details.
- [Configuration template](.env.example) — supported environment variables;
  keep your populated `.env` local.
- [Examples](examples/) — CLI and async/checkpoint demonstrations.
- [Prompt journal](promptlog/README.md) — standalone prompt-recording utility.
