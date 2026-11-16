"""Deterministic, evidence-grounded report assembly.

The workflow uses this assembler for final reports even when a structured LLM
candidate is available. Reports use valid tool observations and disclose missing
evidence; with no usable observations, the workflow supplies a no-data report.
"""

from __future__ import annotations

import json
from typing import Any

from app.schemas.report import StockResearchReport
from app.tools.analysis import project_linear_trend


def _load(obs: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(obs.get("result", "{}"))
    except (TypeError, ValueError):
        return {}


def _num(value: Any, nd: int = 2) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.{nd}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: Any, nd: int = 2) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:.{nd}f}%"
    except (TypeError, ValueError):
        return str(value)


def _source(payload: dict[str, Any], fallback: str = "unknown") -> str:
    src = payload.get("source") or fallback
    ts = payload.get("timestamp") or ""
    return f"{src} ({ts})" if ts else str(src)


def build_grounded_report(state: dict[str, Any], language: str = "en") -> StockResearchReport | None:
    """Build a report purely from collected tool observations."""
    observations = state.get("observations", [])
    if not observations:
        return None

    ticker = (state.get("ticker") or "UNKNOWN").upper()
    data: dict[str, dict[str, Any]] = {}
    for obs in observations:
        name = obs.get("tool")
        args = obs.get("args") or {}
        requested_ticker = args.get("ticker")
        if requested_ticker is not None and str(requested_ticker).strip().upper() != ticker:
            continue
        payload = _load(obs)
        payload_ticker = payload.get("ticker")
        if payload_ticker not in (None, "MARKET") and str(payload_ticker).strip().upper() != ticker:
            continue
        if name and payload and name not in data:
            data[name] = payload

    if not data:
        return None

    price = data.get("get_stock_price", {})
    hist = data.get("get_historical_prices", {})
    news = data.get("search_company_news", {})
    income = data.get("get_income_statement", {})
    balance = data.get("get_balance_sheet", {})
    cashflow = data.get("get_cash_flow", {})
    pe = data.get("calculate_pe", {})
    ev_ebitda = data.get("calculate_ev_ebitda", {})
    fcf_yield = data.get("calculate_fcf_yield", {})
    dcf = data.get("calculate_dcf", {})
    ma = data.get("calculate_ma", {})
    rsi = data.get("calculate_rsi", {})
    macd = data.get("calculate_macd", {})
    vol = data.get("calculate_volatility", {})
    dd = data.get("calculate_drawdown", {})

    closes = [float(b["close"]) for b in hist.get("bars", []) if b.get("close") is not None]
    first, last = (closes[0], closes[-1]) if closes else (None, None)
    change = (last - first) / first if (first and last) else None
    hi, lo = (max(closes), min(closes)) if closes else (None, None)
    projection = project_linear_trend(closes, horizon=20) if closes else []
    projected_price = projection[-1] if projection else None

    price_v = price.get("price")
    currency = price.get("currency", "USD")
    pe_v = pe.get("value")
    ev_v = ev_ebitda.get("value")
    fy_v = fcf_yield.get("value")
    rsi_v = rsi.get("value")
    vol_v = vol.get("value")
    dd_v = dd.get("value") or {}
    ma_v = ma.get("value")
    macd_v = macd.get("value") or {}
    dcf_v = dcf.get("value") or {}

    sources = []
    for payload in data.values():
        src = payload.get("source")
        if src and src not in sources:
            sources.append(str(src))

    key_metrics: dict[str, Any] = {
        "price": price_v,
        "period": hist.get("period"),
        "change": change,
        "revenue": income.get("revenue"),
        "net_income": income.get("net_income"),
        "ebitda": income.get("ebitda"),
        "eps": income.get("eps_diluted"),
        "free_cash_flow": cashflow.get("free_cash_flow"),
        "total_debt": balance.get("total_debt"),
        "cash": balance.get("cash_and_equivalents"),
        "pe": pe_v,
        "ev_ebitda": ev_v,
        "fcf_yield": fy_v,
        "rsi": rsi_v,
        "annualized_volatility": vol_v,
        "max_drawdown": dd_v.get("max_drawdown"),
        "dcf_intrinsic_value": dcf_v.get("intrinsic_value_per_share"),
        "linear_extrapolation_20d": projected_price,
        "linear_extrapolation_note": "naive linear extrapolation, not a prediction",
        "data_sources": sources,
    }

    has_price = price_v is not None
    has_history = bool(closes)
    has_financials = any(
        value is not None
        for value in (
            income.get("revenue"), income.get("net_income"), income.get("ebitda"),
            cashflow.get("free_cash_flow"), balance.get("total_debt"),
        )
    )
    has_technical = any(value is not None for value in (ma_v, rsi_v, macd_v.get("macd"), vol_v, dd_v.get("max_drawdown")))
    has_valuation = any(value is not None for value in (pe_v, ev_v, fy_v, dcf_v.get("intrinsic_value_per_share")))
    has_risk = vol_v is not None or dd_v.get("max_drawdown") is not None
    has_news = bool(news)
    news_titles = [item.get("title", "") for item in news.get("items", []) if item.get("title")][:3]
    news_blob = "; ".join(news_titles)

    missing = []
    if not has_price:
        missing.append("current price")
    if not has_history:
        missing.append("historical prices")
    if not has_financials:
        missing.append("financial statements")
    if not has_technical:
        missing.append("technical indicators")
    if not has_valuation:
        missing.append("valuation metrics")
    if not has_risk:
        missing.append("risk metrics")
    if not has_news:
        missing.append("company news")

    bull_case: list[str] = []
    bear_case: list[str] = []
    if rsi_v is not None and 30 <= float(rsi_v) <= 70:
        bull_case.append(f"RSI {_num(rsi_v, 1)} is in the neutral range; this is an observed indicator, not a recommendation.")
    if fy_v is not None:
        bull_case.append(f"Observed FCF yield is {_pct(fy_v)}; no attractiveness judgment is inferred from this metric alone.")
    if change is not None and change < 0:
        bear_case.append(f"The observed historical window returned {_pct(change)}.")
    if dd_v.get("max_drawdown") is not None:
        bear_case.append(f"Observed maximum drawdown is {_pct(dd_v.get('max_drawdown'))}.")

    uncertainty = [
        "This report was assembled deterministically from valid tool observations; unsupported LLM claims were not used."
    ]
    # Preserve financial semantics through the deterministic final-report path.
    key_metrics["financial_metadata"] = {
        name: {key: payload.get(key) for key in
               ("source", "timestamp", "period", "period_end", "published_at", "source_updated_at", "field_metadata")}
        for name, payload in (("income", income), ("balance", balance), ("cashflow", cashflow)) if payload
    }
    key_metrics["valuation_metadata"] = {
        name: {key: payload.get(key) for key in ("data_period", "period_status", "input_provenance", "note")}
        for name, payload in (("pe", pe), ("ev_ebitda", ev_ebitda), ("fcf_yield", fcf_yield), ("dcf", dcf)) if payload
    }
    for name, metadata in key_metrics["valuation_metadata"].items():
        if metadata.get("note"):
            uncertainty.append(f"{name}: {metadata['note']}")
    for name, metadata in key_metrics["financial_metadata"].items():
        uncertainty.append(f"{name} period: {metadata.get('period') or 'unknown'}, ending {metadata.get('period_end') or 'unknown'}; publication: {metadata.get('published_at') or 'unavailable'}.")
    if any(source == "mock" or "mock" in source.lower() for source in sources):
        uncertainty.append("The available data source is mock/simulated and must not be treated as real market data.")
    if dcf_v:
        uncertainty.append("DCF is a simplified two-stage model; assumptions are inputs, not facts.")
    if missing:
        uncertainty.append("Insufficient evidence for: " + ", ".join(missing) + ".")

    market_en = (
        f"Latest price {_num(price_v)} {currency} (source: {_source(price)})."
        if has_price else "Unavailable: no valid current-price observation."
    )
    if has_history:
        market_en += f" Session range {_num(lo)}–{_num(hi)}, cumulative change {_pct(change)}. Window: {hist.get('data_period') or hist.get('period')}."
    financial_en = (
        f"Revenue {_num(income.get('revenue'))}, net income {_num(income.get('net_income'))}, EBITDA {_num(income.get('ebitda'))}, EPS {_num(income.get('eps_diluted'))}; free cash flow {_num(cashflow.get('free_cash_flow'))}, total debt {_num(balance.get('total_debt'))}, cash {_num(balance.get('cash_and_equivalents'))}."
        if has_financials else "Unavailable: insufficient valid financial-statement observations."
    )
    technical_en = (
        f"RSI {_num(rsi_v, 1)}, moving average {_num(ma_v)}, MACD {_num(macd_v.get('macd'), 3)}/signal {_num(macd_v.get('signal'), 3)}, annualized volatility {_pct(vol_v)}."
        if has_technical else "Unavailable: insufficient valid technical-indicator observations."
    )
    valuation_en = (
        f"P/E {_num(pe_v, 1)}, EV/EBITDA {_num(ev_v, 1)}, FCF yield {_pct(fy_v)}, DCF intrinsic value per share {_num(dcf_v.get('intrinsic_value_per_share'))}."
        if has_valuation else "Unavailable: insufficient valid valuation observations."
    )
    risk_en = (
        f"Annualized volatility {_pct(vol_v)}, max drawdown {_pct(dd_v.get('max_drawdown'))}, current drawdown {_pct(dd_v.get('current_drawdown'))}."
        if has_risk else "Unavailable: insufficient valid risk observations."
    )
    news_en = (
        f"{len(news.get('items', []))} company news item(s) were returned" + (f": {news_blob}." if news_blob else ".")
        if has_news else "Unavailable: no valid company-news observation."
    )

    if language == "zh":
        return StockResearchReport(
            ticker=ticker,
            summary=f"{ticker} 的报告仅使用有效工具观测生成。" + (f" 已观测当前价格 {_num(price_v)} {currency}。" if has_price else " 当前价格不可用。"),
            market_analysis=market_en,
            financial_analysis=financial_en,
            technical_analysis=technical_en,
            news_analysis=news_en,
            valuation_analysis=valuation_en,
            risk_analysis=risk_en,
            bull_case=bull_case,
            bear_case=bear_case,
            key_metrics=key_metrics,
            uncertainty=uncertainty,
            conclusion="证据不足的领域未作投资结论；本报告不构成投资建议。",
        )

    return StockResearchReport(
        ticker=ticker,
        summary=f"{ticker} report assembled only from valid tool observations." + (f" Observed latest price: {_num(price_v)} {currency}." if has_price else " Current price is unavailable."),
        market_analysis=market_en,
        financial_analysis=financial_en,
        technical_analysis=technical_en,
        news_analysis=news_en,
        valuation_analysis=valuation_en,
        risk_analysis=risk_en,
        bull_case=bull_case,
        bear_case=bear_case,
        key_metrics=key_metrics,
        uncertainty=uncertainty,
        conclusion="No investment conclusion is made for areas with insufficient evidence. This is not investment advice.",
    )
