"""Financial-statement tools (data retrieval, Phase 2)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.tools.providers import MarketDataProvider


def financial_provenance(record: BaseModel, field: str) -> dict[str, Any]:
    """Keep retrieval time separate from fiscal period and publication date."""
    result = {
        "field": field,
        "source": getattr(record, "source", None) or "unavailable",
        "timestamp": record.timestamp.isoformat() if getattr(record, "timestamp", None) else "unavailable",
        "period": getattr(record, "period", None) or ("instant" if field == "price" else "unknown"),
        "period_end": getattr(record, "period_end", None) or getattr(record, "as_of", None) or "unknown",
        "published_at": getattr(record, "published_at", None) or "unavailable",
        "source_updated_at": getattr(record, "source_updated_at", None) or "unavailable",
        "note": getattr(record, "note", None),
    }
    result.update(getattr(record, "field_metadata", {}).get(field, {}))
    for name, missing in (("source", "unavailable"), ("timestamp", "unavailable"),
                          ("period", "unknown"), ("period_end", "unknown"), ("published_at", "unavailable"),
                          ("source_updated_at", "unavailable")):
        result[name] = result.get(name) or missing
    return result


def select_eps(provider: MarketDataProvider, ticker: str) -> tuple[float | None, dict[str, Any]]:
    """Select on absence, not truthiness; use the provenance of the chosen field."""
    stats = provider.get_key_stats(ticker)
    if stats.eps_ttm is not None:
        return stats.eps_ttm, financial_provenance(stats, "eps_ttm")
    income = provider.get_income_statement(ticker)
    return income.eps_diluted, financial_provenance(income, "eps_diluted")


class TickerInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol, e.g. NVDA")


def build_financial_tools(provider: MarketDataProvider) -> list[BaseTool]:
    def get_income_statement(ticker: str) -> str:
        """Get the latest income statement (revenue, EBITDA, net income, EPS)."""
        return provider.get_income_statement(ticker).model_dump_json()

    def get_balance_sheet(ticker: str) -> str:
        """Get the latest balance sheet (assets, liabilities, debt, cash, equity)."""
        return provider.get_balance_sheet(ticker).model_dump_json()

    def get_cash_flow(ticker: str) -> str:
        """Get the latest cash flow statement (operating, investing, financing, FCF)."""
        return provider.get_cash_flow(ticker).model_dump_json()

    def get_eps(ticker: str) -> str:
        """Get available EPS, retaining its actual period and source."""
        eps, provenance = select_eps(provider, ticker)
        return json.dumps(
            {
                "ticker": ticker.upper(),
                "eps": eps,
                "eps_ttm": eps if provenance["period"] == "TTM" else None,
                **provenance,
                "data_period": provenance["period"],
                "status": "available" if eps is not None else "unavailable",
            }
        )

    def get_revenue(ticker: str) -> str:
        """Get reported revenue without relabelling annual or unknown data as TTM."""
        income = provider.get_income_statement(ticker)
        return json.dumps(
            {
                "ticker": ticker.upper(),
                "revenue": income.revenue,
                **financial_provenance(income, "revenue"),
                "data_period": financial_provenance(income, "revenue")["period"],
            }
        )

    descriptions = {
        "get_income_statement": "Get the latest income statement for a ticker (revenue, gross profit, operating income, EBITDA, net income, EPS).",
        "get_balance_sheet": "Get the latest balance sheet for a ticker (total assets, liabilities, debt, cash, equity).",
        "get_cash_flow": "Get the latest cash flow statement for a ticker (operating/investing/financing cash flow, free cash flow).",
        "get_eps": "Get EPS with its actual source and period; annual fallback is not TTM EPS.",
        "get_revenue": "Get reported revenue with its actual period (TTM, annual, or unknown).",
    }

    return [
        StructuredTool.from_function(func=get_income_statement, name="get_income_statement", description=descriptions["get_income_statement"], args_schema=TickerInput),
        StructuredTool.from_function(func=get_balance_sheet, name="get_balance_sheet", description=descriptions["get_balance_sheet"], args_schema=TickerInput),
        StructuredTool.from_function(func=get_cash_flow, name="get_cash_flow", description=descriptions["get_cash_flow"], args_schema=TickerInput),
        StructuredTool.from_function(func=get_eps, name="get_eps", description=descriptions["get_eps"], args_schema=TickerInput),
        StructuredTool.from_function(func=get_revenue, name="get_revenue", description=descriptions["get_revenue"], args_schema=TickerInput),
    ]
