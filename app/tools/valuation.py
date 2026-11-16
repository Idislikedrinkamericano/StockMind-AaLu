"""Valuation tools (deterministic computation over financial + market data, Phase 2)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.tools import analysis
from app.tools.base import metric_json
from app.tools.financial import financial_provenance, select_eps
from app.tools.providers import MarketDataProvider


class TickerInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol, e.g. NVDA")


class DcfInput(TickerInput):
    growth_rate: float = Field(default=0.08, description="Annual FCF growth rate for projection years")
    discount_rate: float = Field(default=0.10, description="Assumed WACC for confirmed FCFF, cost of equity for confirmed FCFE")
    terminal_growth: float = Field(default=0.03, description="Perpetual terminal growth rate")
    years: int = Field(default=5, description="Number of projection years", ge=1, le=20)


def _valuation_json(
    metric: str, value: Any, inputs: dict[str, Any],
    provenance: dict[str, dict[str, Any]], note: str = "",
) -> str:
    periods = {(p["period"], p["period_end"]) for p in provenance.values()}
    unknown = any("unknown" in pair for pair in periods)
    status = "unknown" if unknown else "mixed-period" if len(periods) > 1 else "aligned"
    period_text = "; ".join(f"{key}: {p['period']} ending {p['period_end']}" for key, p in provenance.items())
    sources = " + ".join(dict.fromkeys(p["source"] for p in provenance.values()))
    detail = f"{note} Period consistency: {status}. {period_text}. Retrieval timestamps are not publication or fiscal dates."
    payload = json.loads(metric_json(metric, value, inputs, sources, detail.strip()))
    payload.update({
        "input_provenance": provenance,
        "data_period": period_text,
        "period_status": status,
        "status": "available" if value is not None else "unavailable",
    })
    return json.dumps(payload)


def build_valuation_tools(provider: MarketDataProvider) -> list[BaseTool]:
    def calculate_pe(ticker: str) -> str:
        """Calculate P/E using available EPS and disclose its actual period."""
        price_result = provider.get_stock_price(ticker)
        eps, eps_provenance = select_eps(provider, ticker)
        value = analysis.compute_pe(price_result.price, eps)
        return _valuation_json(
            "pe",
            value,
            {"ticker": ticker.upper(), "price": price_result.price, "eps": eps},
            {"price": financial_provenance(price_result, "price"), "eps": eps_provenance},
            f"P/E uses {eps_provenance['period']} EPS; annual fallback is not trailing P/E.",
        )

    def calculate_ev_ebitda(ticker: str) -> str:
        """Calculate EV/EBITDA from market cap, total debt, cash and EBITDA."""
        stats = provider.get_key_stats(ticker)
        balance = provider.get_balance_sheet(ticker)
        income = provider.get_income_statement(ticker)
        value = analysis.compute_ev_ebitda(
            stats.market_cap, balance.total_debt, balance.cash_and_equivalents, income.ebitda
        )
        return _valuation_json(
            "ev_ebitda",
            value,
            {
                "ticker": ticker.upper(),
                "market_cap": stats.market_cap,
                "total_debt": balance.total_debt,
                "cash": balance.cash_and_equivalents,
                "ebitda": income.ebitda,
            },
            {"market_cap": financial_provenance(stats, "market_cap"),
             "total_debt": financial_provenance(balance, "total_debt"),
             "cash": financial_provenance(balance, "cash_and_equivalents"),
             "ebitda": financial_provenance(income, "ebitda")},
            "Simplified EV = market cap + reported debt - cash; no minority/preferred-claim adjustment.",
        )

    def calculate_fcf_yield(ticker: str) -> str:
        """Calculate free cash flow yield (FCF / market cap)."""
        cashflow = provider.get_cash_flow(ticker)
        stats = provider.get_key_stats(ticker)
        market_cap = stats.market_cap
        value = analysis.compute_fcf_yield(cashflow.free_cash_flow, market_cap)
        return _valuation_json(
            "fcf_yield",
            value,
            {"ticker": ticker.upper(), "free_cash_flow": cashflow.free_cash_flow, "market_cap": market_cap},
            {"free_cash_flow": financial_provenance(cashflow, "free_cash_flow"),
             "market_cap": financial_provenance(stats, "market_cap")},
            f"Reported FCF / market cap; cash-flow basis: {cashflow.cash_flow_basis}. Not a verified FCFE yield.",
        )

    def calculate_dcf(
        ticker: str,
        growth_rate: float = 0.08,
        discount_rate: float = 0.10,
        terminal_growth: float = 0.03,
        years: int = 5,
    ) -> str:
        """Calculate a simplified two-stage discounted cash flow (DCF) intrinsic value."""
        cashflow = provider.get_cash_flow(ticker)
        stats = provider.get_key_stats(ticker)
        shares = stats.shares_outstanding
        provenance = {"free_cash_flow": financial_provenance(cashflow, "free_cash_flow"),
                      "shares_outstanding": financial_provenance(stats, "shares_outstanding")}
        net_debt = None
        reasons = []
        if cashflow.cash_flow_basis not in {"FCFF", "FCFE"}:
            reasons.append("cash-flow basis unknown; reported FCF cannot be assumed FCFF/FCFE")
        if provenance["free_cash_flow"]["period"] not in {"annual", "TTM"}:
            reasons.append("annual/TTM cash-flow period unavailable")
        if shares is None or stats.shares_basis not in {"reported_outstanding", "simulated_outstanding"}:
            reasons.append("confirmed outstanding shares unavailable; derived or weighted-average shares are not used")
        if cashflow.cash_flow_basis == "FCFF":
            balance = provider.get_balance_sheet(ticker)
            provenance.update({"total_debt": financial_provenance(balance, "total_debt"),
                               "cash": financial_provenance(balance, "cash_and_equivalents")})
            if balance.total_debt is None or balance.cash_and_equivalents is None:
                reasons.append("net debt unavailable; both debt and cash are required")
            else:
                net_debt = balance.total_debt - balance.cash_and_equivalents
            cf_end = provenance["free_cash_flow"]["period_end"]
            if cf_end == "unknown" or any(provenance[k]["period_end"] != cf_end for k in ("total_debt", "cash")):
                reasons.append("cash-flow and net-debt dates are unknown or inconsistent")
        value = None if reasons else analysis.compute_dcf(
            cashflow.free_cash_flow,
            shares,
            growth_rate=growth_rate,
            discount_rate=discount_rate,
            terminal_growth=terminal_growth,
            years=years,
            cash_flow_basis=cashflow.cash_flow_basis,
            net_debt=net_debt,
        )
        if value is None and not reasons:
            reasons.append("invalid or non-finite DCF inputs, rates, or non-positive equity value")
        return _valuation_json(
            "dcf",
            value,
            {"ticker": ticker.upper(), "free_cash_flow": cashflow.free_cash_flow, "shares_outstanding": shares,
             "cash_flow_basis": cashflow.cash_flow_basis, "shares_basis": stats.shares_basis,
             "net_debt": net_debt, "growth_rate": growth_rate, "discount_rate": discount_rate,
             "terminal_growth": terminal_growth, "years": years},
            provenance,
            "Unavailable: " + "; ".join(reasons) if reasons else
            "Simplified two-stage scenario; FCFF discounted at assumed WACC, FCFE at assumed cost of equity. Rates are assumptions, not facts.",
        )

    return [
        StructuredTool.from_function(
            func=calculate_pe,
            name="calculate_pe",
            description="Calculate the price-to-earnings (P/E) ratio of a stock.",
            args_schema=TickerInput,
        ),
        StructuredTool.from_function(
            func=calculate_ev_ebitda,
            name="calculate_ev_ebitda",
            description="Calculate the enterprise-value-to-EBITDA multiple of a stock.",
            args_schema=TickerInput,
        ),
        StructuredTool.from_function(
            func=calculate_fcf_yield,
            name="calculate_fcf_yield",
            description="Calculate the free cash flow yield (FCF / market cap) of a stock.",
            args_schema=TickerInput,
        ),
        StructuredTool.from_function(
            func=calculate_dcf,
            name="calculate_dcf",
            description="Calculate a simplified discounted cash flow (DCF) intrinsic value per share.",
            args_schema=DcfInput,
        ),
    ]
