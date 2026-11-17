"""Evaluation-only synthetic fixtures. Production providers and tools are unchanged."""

import json
from datetime import datetime, timedelta

from langchain_core.messages import AIMessage

from app.agents.react_agent import StockMindAgent
from app.graph.workflow import AgentConfig
from app.models.demo import ScriptedReActDemoModel
from app.observability.tracing import TraceCollector
from app.tools import build_tools
from app.tools.providers import (
    BalanceSheet, CashFlow, HistoricalPricesResult, IncomeStatement, KeyStats,
    MarketDataProvider, NewsItem, NewsResult, PriceBar, StockPriceResult,
)

from app.evaluation.dataset import EvaluationCase, FIXTURE_PATH


class FixtureProvider(MarketDataProvider):
    """Reads fixed synthetic inputs; failures are explicit case scenarios."""

    def __init__(self, scenario: str = "standard"):
        self.data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.scenario = scenario
        self.timestamp = datetime.fromisoformat(self.data["timestamp"])

    def _metadata(self, ticker: str) -> dict:
        if ticker != "MARKET" and ticker not in self.data["symbols"]:
            raise ValueError(f"No synthetic fixture for ticker {ticker!r}")
        return {"ticker": ticker, "source": self.data["source"],
                "timestamp": self.timestamp, "note": self.data["note"]}

    def _financial_metadata(self, ticker: str, period: str = "TTM") -> dict:
        return {**self._metadata(ticker), "period": period,
                "period_end": self.data["period_end"], "published_at": self.data["published_at"]}

    def get_stock_price(self, ticker: str) -> StockPriceResult:
        if self.scenario == "price_unavailable":
            raise RuntimeError("fixture price unavailable")
        payload_ticker = "AMD" if self.scenario == "wrong_observation_ticker" else ticker
        return StockPriceResult(**self._metadata(payload_ticker),
                                price=self.data["symbols"][payload_ticker]["price"],
                                currency="USD", as_of=self.timestamp.date().isoformat())

    def get_historical_prices(self, ticker: str, period: str, interval: str) -> HistoricalPricesResult:
        metadata = self._metadata(ticker)
        closes = [self.data["symbols"][ticker]["price"] + offset for offset in self.data["close_offsets"]]
        bars = [PriceBar(date=(self.timestamp.date() - timedelta(days=len(closes) - i - 1)).isoformat(),
                         open=close, high=close, low=close, close=close, volume=1000)
                for i, close in enumerate(closes)]
        return HistoricalPricesResult(**metadata, period=period, interval=interval, bars=bars,
                                      data_period="40 fixed synthetic daily bars; not a live lookback window")

    def search_company_news(self, ticker: str, query: str, limit: int) -> NewsResult:
        if self.scenario == "news_failure":
            raise RuntimeError("fixture news unavailable")
        return NewsResult(**self._metadata(ticker), query=query, items=[
            NewsItem(title=f"{ticker} synthetic fixture news", source=self.data["source"],
                     published_at=self.data["timestamp"])
        ][:limit])

    def search_market_news(self, query: str, limit: int) -> NewsResult:
        return self.search_company_news("MARKET", query, limit)

    def get_income_statement(self, ticker: str) -> IncomeStatement:
        metadata = self._financial_metadata(ticker)
        if self.scenario == "missing_financials":
            return IncomeStatement(**metadata)
        values = self.data["financials"]
        return IncomeStatement(**metadata, revenue=values["revenue"], net_income=values["net_income"],
                               ebitda=values["ebitda"], eps_diluted=self.data["symbols"][ticker]["eps"])

    def get_balance_sheet(self, ticker: str) -> BalanceSheet:
        metadata = self._financial_metadata(ticker, "instant")
        values = self.data["financials"]
        return BalanceSheet(**metadata, total_debt=values["total_debt"], cash_and_equivalents=values["cash"])

    def get_cash_flow(self, ticker: str) -> CashFlow:
        return CashFlow(**self._financial_metadata(ticker), free_cash_flow=(
            None if self.scenario == "missing_financials" else self.data["financials"]["free_cash_flow"]))

    def get_key_stats(self, ticker: str) -> KeyStats:
        metadata = self._financial_metadata(ticker, "instant")
        metadata.update(period_end=self.timestamp.date().isoformat(), published_at=None)
        return KeyStats(**metadata,
                        field_metadata={"eps_ttm": {"period": "TTM", "period_end": self.data["period_end"],
                                                    "published_at": self.data["published_at"]}},
                        eps_ttm=None if self.scenario == "missing_financials" else self.data["symbols"][ticker]["eps"],
                        shares_outstanding=self.data["symbols"][ticker]["shares"],
                        shares_basis="simulated_outstanding", market_cap=self.data["financials"]["market_cap"])


def build_offline_agent(case: EvaluationCase, language: str = "en") -> StockMindAgent:
    # One call per step gives a fixed execution order without changing graph concurrency.
    messages = [AIMessage(content="", tool_calls=[{
        "name": call.name, "args": call.args, "id": f"{case.case_id}-{index}",
    }]) for index, call in enumerate(case.script)]
    messages.append(AIMessage(content=case.final_text))
    return StockMindAgent(
        llm=ScriptedReActDemoModel(messages=iter(messages)),
        tools=build_tools(FixtureProvider(case.scenario)),
        config=AgentConfig(max_iterations=8, tool_retries=0, retry_jitter=0.0, language=language),
        language=language, tracer=TraceCollector(), checkpointer="memory",
    )
