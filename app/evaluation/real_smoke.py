"""Explicitly gated real-world smoke validation; no service access on import/preflight.

This runner reuses the production model factory, tools, graph, tracing and
grounding check. It does not change P2 scoring or offer an offline pass as live proof.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from langsmith import tracing_context
from pydantic import BaseModel, Field

from app.agents.react_agent import StockMindAgent
from app.evaluation.evaluator import evidence_checks
from app.graph.workflow import AgentConfig
from app.models.providers import get_chat_model
from app.observability.tracing import TraceCollector
from app.tools import build_tools, get_data_provider
from app.tools.providers import normalize_stock_symbol

TECHNICAL = ["calculate_ma", "calculate_rsi", "calculate_macd", "calculate_volatility", "calculate_drawdown"]
FINANCIAL = ["get_income_statement", "get_balance_sheet", "get_cash_flow", "get_eps"]
OUTAGE = "controlled_provider_unavailable: income statement"


class SmokeConfig(BaseModel):
    llm_provider: Literal["openai", "anthropic"] = "openai"
    model: str = Field(min_length=1)
    max_iterations: int = Field(default=4, ge=1, le=12)
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    deadline: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    llm_timeout: float = Field(default=20, gt=0, le=120, allow_inf_nan=False)
    tool_timeout: float = Field(default=15, gt=0, le=60, allow_inf_nan=False)

    @classmethod
    def from_environment(cls, **overrides):
        provider = overrides.get("llm_provider") or os.getenv("STOCKMIND_LLM_PROVIDER", "openai").strip().lower()
        default = "claude-3-5-haiku-latest" if provider == "anthropic" else "gpt-4o-mini"
        return cls(**{"llm_provider": provider,
                      "model": os.getenv("STOCKMIND_LLM_MODEL") or default, **overrides})


class SmokeCase(BaseModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.^=-]{0,24}$")
    query: str = Field(min_length=1)
    financial_provider: Literal["yfinance", "akshare"]
    # Each group requires at least one successful, sourced observation.
    required_tool_groups: list[list[str]]
    inject_income_failure: bool = False


def default_cases() -> list[SmokeCase]:
    return [
        SmokeCase(case_id="aapl-price", ticker="AAPL", financial_provider="yfinance",
                  query="分析 AAPL 最近的价格表现与趋势，并说明数据来源。",
                  required_tool_groups=[["get_stock_price"], ["get_historical_prices"]]),
        SmokeCase(case_id="msft-research", ticker="MSFT", financial_provider="yfinance",
                  query="结合价格、技术指标和可用财务数据，给出 MSFT 的结构化研究报告。",
                  required_tool_groups=[["get_stock_price"], ["get_historical_prices"], TECHNICAL, FINANCIAL]),
        SmokeCase(case_id="a-share", ticker="600519", financial_provider="akshare",
                  query="分析贵州茅台（600519）的近期行情及技术表现。",
                  required_tool_groups=[["get_stock_price"], ["get_historical_prices"], TECHNICAL]),
        SmokeCase(case_id="provider-failure", ticker="AAPL", financial_provider="yfinance",
                  query="查询 AAPL 当前价格与利润表；数据源失败时说明缺失，不要猜测财务数据。",
                  required_tool_groups=[["get_stock_price"]], inject_income_failure=True),
    ]


def _base_url(config: SmokeConfig) -> str | None:
    if config.llm_provider == "openai":
        return os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
    return os.getenv("ANTHROPIC_BASE_URL")


def preflight(case: SmokeCase, config: SmokeConfig, *, execute_real: bool) -> list[str]:
    """Read configuration presence only. Never load .env, instantiate clients or call providers."""
    reasons = []
    if not execute_real:
        reasons.append("Real requests disabled: review the plan and explicitly pass --execute-real.")
    key = "OPENAI_API_KEY" if config.llm_provider == "openai" else "ANTHROPIC_API_KEY"
    if not os.getenv(key, "").strip():
        reasons.append(f"Missing credential: {key}")
    for package in (case.financial_provider, f"langchain_{config.llm_provider}"):
        if find_spec(package) is None:
            reasons.append(f"Missing dependency: {package}")
    url = _base_url(config)
    if url:
        try:
            parts = urlsplit(url)
            if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username
                    or parts.password or parts.query or parts.fragment):
                reasons.append("Invalid base URL: use HTTP(S) without credentials, query or fragment.")
        except ValueError:
            reasons.append("Invalid base URL.")
    try:
        _, market = normalize_stock_symbol(case.ticker)
        if market == "CN" and case.financial_provider != "akshare":
            reasons.append("A-share daily data requires the AkShare provider, not a bare Yahoo symbol.")
    except ValueError as exc:
        reasons.append(str(exc))
    return reasons


class _IncomeOutage:
    """Smoke-only controlled partial outage. All other methods use the selected real provider."""
    def __init__(self, provider):
        self.provider = provider

    def get_income_statement(self, ticker):
        raise RuntimeError(OUTAGE)

    def __getattr__(self, name):
        return getattr(self.provider, name)


def _build_agent(case: SmokeCase, config: SmokeConfig) -> StockMindAgent:
    # Explicit args override ambient defaults. SDK retries are disabled to bound paid requests.
    options = dict(max_retries=0, timeout=config.llm_timeout, max_tokens=config.max_output_tokens)
    if _base_url(config):
        options["base_url"] = _base_url(config)
    llm = get_chat_model(config.llm_provider, config.model, **options)
    provider = get_data_provider(case.financial_provider)
    if case.inject_income_failure:
        provider = _IncomeOutage(provider)
    return StockMindAgent(
        llm=llm, tools=build_tools(provider), tracer=TraceCollector(), checkpointer="memory",
        language="en", config=AgentConfig(
            max_iterations=config.max_iterations, llm_timeout_seconds=config.llm_timeout,
            run_timeout_seconds=config.deadline, tool_timeout_seconds=config.tool_timeout,
            tool_retries=0, retry_jitter=0),
    )


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _available(payload: dict) -> bool:
    if "price" in payload:
        return _number(payload["price"]) and payload["price"] >= 0
    if "bars" in payload:
        bars = payload["bars"]
        return isinstance(bars, list) and len(bars) >= 2 and all(
            isinstance(bar, dict) and all(_number(bar.get(k)) and bar[k] >= 0
                                         for k in ("open", "high", "low", "close")) for bar in bars)
    if "metric" in payload:
        value = payload.get("value")
        return _number(value) or (isinstance(value, dict) and bool(value)
                                  and all(_number(v) for v in value.values()))
    return any(_number(payload.get(key)) for key in (
        "revenue", "net_income", "ebitda", "eps_diluted", "free_cash_flow",
        "total_assets", "total_debt", "cash_and_equivalents"))


def assess(case: SmokeCase, state: dict, events: list, *, llm_response_received: bool) -> dict:
    grounding = evidence_checks(state, case.ticker)
    sources, available = [], set()
    tickers_valid = True
    for obs in state.get("observations", []):
        try:
            payload = json.loads(obs.get("result", "{}"))
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        tickers = [obs.get("args", {}).get("ticker"), payload.get("ticker"),
                   (payload.get("inputs") or {}).get("ticker")]
        matched = all(t is None or str(t).strip().upper() == case.ticker for t in tickers)
        tickers_valid &= matched
        source = payload.get("source") or ""
        prefix = "computed from "
        raw_source = source[len(prefix):-5] if source.startswith(prefix) and source.endswith(" data") else source
        valid_source = raw_source in (
            {"yfinance"} if case.financial_provider == "yfinance" else
            {"akshare/sina", "akshare/baidu", "akshare/eastmoney", "akshare/baidu + akshare/sina"})
        if re.fullmatch(r"[0-9]{6}", case.ticker):
            valid_source = case.financial_provider == "akshare" and raw_source == "akshare/eastmoney"
        usable = matched and valid_source and bool(payload.get("timestamp")) and _available(payload)
        if usable:
            available.add(obs["tool"])
        sources.append({"tool": obs.get("tool"), "source": source or "unavailable",
                        "timestamp": payload.get("timestamp"), "period": payload.get("period", "unknown"),
                        "period_end": payload.get("period_end"), "as_of": payload.get("as_of"),
                        "available": usable})
    attempts = [event for event in events if event.event_type == "tool_call"]
    executed = {event.tool_name for event in attempts if not event.error}
    checks = {
        "real_llm_response": llm_response_received,
        "finished": state.get("status") == "finished",
        "grounded": all(grounding.values()),
        "observation_tickers": tickers_valid,
        "sourced_data": bool(available),
        "required_tools": all(set(group) & available & executed for group in case.required_tool_groups),
        "no_rate_limit": not any("rate_limited:" in error for error in state.get("errors", [])),
        "no_unexpected_errors": all(
            case.inject_income_failure and error == f"RuntimeError: {OUTAGE}"
            for error in state.get("errors", [])
        ),
    }
    if case.inject_income_failure:
        errors = state.get("errors", [])
        report = state.get("final_output")
        checks["controlled_outage_observed"] = any(OUTAGE in error for error in errors)
        checks["missing_financials_disclosed"] = bool(
            report and report.financial_analysis.startswith("Unavailable"))
    return {"checks": checks, "grounding": grounding, "source_availability": sources,
            "failure_reasons": [name for name, passed in checks.items() if not passed]}


def _redact(value: Any) -> Any:
    """Sanitize local artifacts, including exception text; never persist the environment."""
    secrets = [os.getenv(name) for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY")]
    def clean(text):
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)((?:api[_-]?key|token|password|authorization)\s*[=:]\s*)[^\s,;&]+",
                      r"\1[REDACTED]", text)
        text = re.sub(r"https?://[^\s<>\"']+", "[URL REDACTED]", text)
        return text
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(str(k)): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


async def run_case(case: SmokeCase, config: SmokeConfig, *, execute_real: bool = False) -> dict:
    start = time.perf_counter()
    reasons = preflight(case, config, execute_real=execute_real)
    result = {
        "case_id": case.case_id, "ticker": case.ticker, "user_query": case.query,
        "llm_provider": config.llm_provider, "model": config.model,
        "financial_provider": case.financial_provider, "limits": config.model_dump(),
        "failure_injection": "income_statement_unavailable" if case.inject_income_failure else None,
        "outcome": "blocked", "execution_status": "not_started", "execution_seconds": 0.0,
        "real_llm_response_received": False, "actual_tool_calls": [], "source_availability": [],
        "grounding": {}, "checks": {}, "errors": [], "warnings": reasons,
        "failure_reasons": reasons, "observations": [], "final_report": None,
        "final_report_path": None,
    }
    if reasons:
        return _redact(result)
    agent = None
    try:
        # No LangSmith network telemetry, trace files, or persistent user checkpoints.
        with tracing_context(enabled=False):
            agent = _build_agent(case, config)
            state = await agent.arun(case.query, ticker=case.ticker)
        events = agent.tracer.events
        responded = any(e.event_type == "agent_step" and e.latency_ms is not None
                        and not e.error and e.final_status is None for e in events)
        result.update(assess(case, state, events, llm_response_received=responded))
        result.update(
            real_llm_response_received=responded, execution_status=state.get("status"),
            errors=state.get("errors", []), observations=state.get("observations", []),
            actual_tool_calls=[{"name": e.tool_name, "args": e.tool_input, "error": e.error,
                               "latency_ms": e.latency_ms} for e in events if e.event_type == "tool_call"],
            llm_attempt_count=sum(e.event_type == "agent_step" and e.latency_ms is not None for e in events),
            final_report=state["final_output"].model_dump(mode="json") if state.get("final_output") else None,
        )
        result["outcome"] = "pass" if all(result["checks"].values()) else "fail"
    except Exception as exc:
        # CancelledError intentionally propagates. No automatic retry or mock fallback.
        result.update(outcome="fail", execution_status="error",
                      errors=[f"{type(exc).__name__}: {exc}"],
                      failure_reasons=["runner_or_client_failure"])
    result["execution_seconds"] = time.perf_counter() - start
    result["warnings"] = [
        "Grounding checks assembler consistency, not independent financial truth.",
        "Synchronous provider worker threads may outlive asyncio cancellation; no hard process kill.",
        "No cost estimate: endpoint tariffs, input tokens and tool fan-out are unknown.",
    ]
    return _redact(result)


def create_output_directory(base: Path) -> Path:
    base = base.absolute()
    # macOS exposes standard temporary directories through these system aliases.
    def system_alias(path):
        return path in (Path("/tmp"), Path("/var")) and path.resolve() == Path("/private") / path.name
    if any(p.is_symlink() and not system_alias(p) for p in (base, *base.parents)):
        raise ValueError("Output directory must not contain symlinks.")
    base = base.resolve()
    if any(part in {".git", ".env", ".venv"} for part in base.parts):
        raise ValueError("Refusing a protected output directory.")
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    run_dir.mkdir(mode=0o700)
    return run_dir


def save_result(result: dict, directory: Path) -> Path:
    # Never derive paths from ticker, query, remote errors, or a model response.
    case_id = result["case_id"]
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", case_id) is None:
        raise ValueError("Invalid case_id")
    result = _redact(result)
    report = result.pop("final_report")
    if report is not None:
        path = directory / f"{case_id}-report.json"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        result["final_report_path"] = str(path)
    path = directory / f"{case_id}-result.json"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return path
