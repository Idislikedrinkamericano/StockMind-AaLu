"""Offline P3-C provider contracts; no real AkShare/Yahoo/LLM requests."""
import asyncio
import json
import socket
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

from app.agents.react_agent import StockMindAgent
from app.graph.workflow import AgentConfig
from app.models.demo import ScriptedReActDemoModel
from app.observability.tracing import TraceCollector
from app.tools import build_tools
from app.tools.base import ProviderRateLimitError
from app.tools.providers import AkShareMarketDataProvider, YFinanceMarketDataProvider, normalize_stock_symbol


class RateLimitError(Exception):
    pass


class Push2RateLimitError(RateLimitError, ConnectionError):
    pass


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No external connections in provider regression tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("app.tools.providers.utc_now", lambda: datetime(2026, 11, 18, tzinfo=timezone.utc))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def a_frame(symbol="600519"):
    return pd.DataFrame([
        {"日期": "2026-11-16", "股票代码": symbol, "开盘": 10, "最高": 12, "最低": 9, "收盘": 11, "成交量": 1234},
        {"日期": "2026-11-17", "股票代码": symbol, "开盘": 11, "最高": 13, "最低": 10, "收盘": 12, "成交量": 2345},
    ])


class FakeAkShare:
    RateLimitError = RateLimitError

    def __init__(self, frame=None, fault=None):
        self.frame, self.fault, self.calls = frame, fault, []

    def stock_zh_a_hist_em(self, symbol="000001", period="daily", start_date="19700101",
                         end_date="20500101", adjust=""):
        self.calls.append(("CN", dict(symbol=symbol, period=period, start_date=start_date,
                                     end_date=end_date, adjust=adjust)))
        if self.fault:
            raise self.fault
        return a_frame(symbol) if self.frame is None else self.frame

    def stock_us_hist_sina(self, symbol="FB", period="daily", adjust=""):
        self.calls.append(("US", dict(symbol=symbol, period=period, adjust=adjust)))
        return pd.DataFrame([{"date": "2026-11-17", "open": 10, "high": 12, "low": 9,
                              "close": 11, "volume": 1000}])

    def stock_zh_a_hist(self, *a, **kw):
        pytest.fail("New A-share endpoint exists; legacy fallback must not run")

    def stock_us_daily(self, *a, **kw):
        pytest.fail("New US endpoint exists; legacy fallback must not run")

    def stock_financial_us_report_em(self, **kwargs):
        self.calls.append(("US-financial", kwargs))
        if self.fault:
            raise self.fault
        return pd.DataFrame()

    def stock_us_valuation_baidu(self, **kwargs):
        self.calls.append(("US-valuation", kwargs))
        if self.fault:
            raise self.fault
        return pd.DataFrame()


def make_provider(monkeypatch, **kwargs):
    fake = FakeAkShare(**kwargs)
    monkeypatch.setattr(AkShareMarketDataProvider, "_ak", lambda self: fake)
    return AkShareMarketDataProvider(), fake


@pytest.mark.parametrize("ticker,market,currency", [
    ("AAPL", "US", "USD"), ("MSFT", "US", "USD"), ("600519", "CN", "CNY"), ("000001", "CN", "CNY"),
])
@pytest.mark.parametrize("method", ["quote", "history"])
def test_symbol_routes_without_losing_leading_zero(monkeypatch, ticker, market, currency, method):
    provider, fake = make_provider(monkeypatch)
    result = (provider.get_stock_price(ticker) if method == "quote"
              else provider.get_historical_prices(ticker, "1mo", "1d"))
    assert fake.calls[0][0] == market
    assert fake.calls[0][1]["symbol"] == ticker
    assert isinstance(fake.calls[0][1]["symbol"], str)
    assert result.ticker == ticker
    assert result.source == ("akshare/eastmoney" if market == "CN" else "akshare/sina")
    if method == "quote":
        assert result.currency == currency


@pytest.mark.parametrize("bad", ["1", "123456", "600519.SH", "0700.HK", "^GSPC", "", 1, 600519, "510300"])
def test_ambiguous_or_unsupported_symbols_never_call_endpoint(monkeypatch, bad):
    provider, fake = make_provider(monkeypatch)
    with pytest.raises(ValueError, match="unsupported_symbol"):
        provider.get_stock_price(bad)
    assert fake.calls == []


def test_simple_normalization():
    assert normalize_stock_symbol(" aapl ") == ("AAPL", "US")
    assert normalize_stock_symbol("000001") == ("000001", "CN")


def test_a_share_dates_adjustment_and_ohlcv(monkeypatch):
    frame = a_frame().iloc[::-1]
    provider, fake = make_provider(monkeypatch, frame=frame)
    history = provider.get_historical_prices("600519", "1mo", "1d")
    assert fake.calls[0][1] == dict(symbol="600519", period="daily", start_date="20261019",
                                   end_date="20261118", adjust="qfq")
    assert [b.date for b in history.bars] == ["2026-11-16", "2026-11-17"]
    last = history.bars[-1]
    assert (last.open, last.high, last.low, last.close, last.volume) == (11, 13, 10, 12, 2345)
    quote = provider.get_stock_price("600519")
    assert fake.calls[-1][1]["adjust"] == ""
    assert quote.price == 12 and quote.as_of == "2026-11-17"
    assert "endpoint units" in quote.note
    assert "calendar lookback" in history.data_period


def test_valid_zero_is_preserved(monkeypatch):
    frame = a_frame()
    frame.loc[:, ["开盘", "最高", "最低", "收盘", "成交量"]] = 0
    provider, _ = make_provider(monkeypatch, frame=frame)
    assert provider.get_stock_price("600519").price == 0
    bars = provider.get_historical_prices("600519", "1mo", "1d").bars
    assert all(bar.close == 0 and bar.volume == 0 for bar in bars)


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("column", ["收盘", "成交量"])
def test_missing_or_nonfinite_data_is_not_filled(monkeypatch, bad, column):
    frame = a_frame().astype({column: "object"})
    frame.loc[1, column] = bad
    provider, _ = make_provider(monkeypatch, frame=frame)
    with pytest.raises(ValueError, match="invalid_data"):
        provider.get_historical_prices("600519", "1mo", "1d")


@pytest.mark.parametrize("changes", [
    {"最高": 10}, {"最低": 13}, {"收盘": -1}, {"开盘": 14},
    {"成交量": -1}, {"成交量": 1.5}, {"日期": "NaT"},
    {"日期": "2026-11-19"}, {"日期": "2026-11-16"}, {"股票代码": "000001"},
])
def test_invalid_bar_or_ticker_is_rejected(monkeypatch, changes):
    frame = a_frame().astype(object)
    for key, value in changes.items():
        frame.loc[1, key] = value
    provider, _ = make_provider(monkeypatch, frame=frame)
    with pytest.raises(ValueError, match="invalid_data"):
        provider.get_stock_price("600519")


@pytest.mark.parametrize("frame,expected", [(pd.DataFrame(), "empty_data"), (pd.DataFrame({"收盘": [10]}), "invalid_data")])
def test_empty_and_invalid_frames_are_distinct(monkeypatch, frame, expected):
    provider, _ = make_provider(monkeypatch, frame=frame)
    with pytest.raises(ValueError, match=expected):
        provider.get_stock_price("600519")


@pytest.mark.parametrize("kwargs", [dict(period="bad", interval="1d"), dict(period="1mo", interval="1m")])
def test_unsupported_lookback_or_interval_never_calls_endpoint(monkeypatch, kwargs):
    provider, fake = make_provider(monkeypatch)
    with pytest.raises(ValueError, match="unsupported_"):
        provider.get_historical_prices("600519", **kwargs)
    assert fake.calls == []


def test_legacy_names_only_when_new_api_absent(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        stock_zh_a_hist=lambda **kw: (calls.append(("CN", kw)) or a_frame()),
        stock_us_daily=lambda **kw: (calls.append(("US", kw)) or pd.DataFrame([{"date": "2026-11-17", "close": 10}])))
    monkeypatch.setattr(AkShareMarketDataProvider, "_ak", lambda self: fake)
    p = AkShareMarketDataProvider()
    assert p.get_stock_price("600519").currency == "CNY"
    assert p.get_stock_price("AAPL").currency == "USD"
    assert [c[0] for c in calls] == ["CN", "US"]
    assert calls[0][1]["start_date"] == "20261019" and calls[1][1] == {"symbol": "AAPL", "adjust": ""}


@pytest.mark.parametrize("ticker", ["600519", "AAPL"])
def test_missing_api_explained(monkeypatch, ticker):
    monkeypatch.setattr(AkShareMarketDataProvider, "_ak", lambda self: SimpleNamespace())
    with pytest.raises(RuntimeError, match="unsupported_api"):
        AkShareMarketDataProvider().get_stock_price(ticker)


@pytest.mark.parametrize("method", ["get_stock_price", "get_income_statement"])
def test_request_exception_is_not_an_empty_result(monkeypatch, method):
    provider, fake = make_provider(monkeypatch, fault=ConnectionError("offline fixture disconnected"))
    ticker = "600519" if method == "get_stock_price" else "AAPL"
    with pytest.raises(RuntimeError, match="request_failed:.*ConnectionError") as error:
        getattr(provider, method)(ticker)
    assert isinstance(error.value.__cause__, ConnectionError)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("method", ["get_income_statement", "get_balance_sheet", "get_cash_flow", "get_key_stats"])
def test_a_share_financials_never_use_us_endpoints(monkeypatch, method):
    provider, fake = make_provider(monkeypatch)
    with pytest.raises(ValueError, match="US-only"):
        getattr(provider, method)("000001")
    assert fake.calls == []


def test_rate_limit_is_classified_and_latched_per_instance(monkeypatch):
    provider, fake = make_provider(monkeypatch, fault=Push2RateLimitError("offline fixture limit"))
    for ticker in ("600519", "000001"):
        with pytest.raises(ProviderRateLimitError, match="rate_limited: akshare/eastmoney") as error:
            provider.get_stock_price(ticker)
        assert isinstance(error.value.__cause__, Push2RateLimitError)
    assert len(fake.calls) == 1
    # No global cooldown state, and no automatic switch: explicitly requested US path still works.
    assert provider.get_stock_price("AAPL").source == "akshare/sina"
    fresh = AkShareMarketDataProvider()
    with pytest.raises(ProviderRateLimitError):
        fresh.get_stock_price("600519")
    assert [c[0] for c in fake.calls] == ["CN", "US", "CN"]


def test_rate_limit_reaches_tool_without_fake_observation(monkeypatch):
    provider, _ = make_provider(monkeypatch, fault=Push2RateLimitError("fixture"))
    tool = next(t for t in build_tools(provider) if t.name == "get_historical_prices")
    with pytest.raises(ProviderRateLimitError):
        tool.invoke({"ticker": "600519", "period": "1mo", "interval": "1d"})


def test_us_financial_rate_limits_are_not_swallowed(monkeypatch):
    provider, fake = make_provider(monkeypatch, fault=Push2RateLimitError("fixture"))
    with pytest.raises(ProviderRateLimitError):
        provider.get_income_statement("AAPL")
    with pytest.raises(ProviderRateLimitError):
        provider.get_balance_sheet("AAPL")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("partial", [False, True])
def test_actual_workflow_never_retries_rate_limit_or_claims_successful_data(monkeypatch, partial):
    provider, fake = make_provider(monkeypatch)
    if not partial:
        fake.fault = Push2RateLimitError("fixture")
        calls = [("get_stock_price", {}), ("get_historical_prices", {"period": "1mo"})]
    else:
        original = fake.stock_zh_a_hist_em
        def fault_on_adjusted(**kw):
            if kw["adjust"] == "qfq":
                fake.calls.append(("CN-rate-limited", kw))
                raise Push2RateLimitError("fixture")
            return original(**kw)
        fake.stock_zh_a_hist_em = fault_on_adjusted
        # Sequential steps guarantee one valid quote before the injected outage.
        calls = [("get_stock_price", {}), ("get_historical_prices", {"period": "1mo"})]
    messages = [AIMessage(content="", tool_calls=[{
        "name": name, "args": {"ticker": "600519", **args}, "id": str(i)}])
        for i, (name, args) in enumerate(calls)]
    messages.append(AIMessage(content="Invented 999999 revenue"))
    agent = StockMindAgent(llm=ScriptedReActDemoModel(messages=iter(messages)), tools=build_tools(provider),
                           tracer=TraceCollector(), config=AgentConfig(tool_retries=5,
                           retry_backoff_seconds=0, retry_jitter=0))
    state = asyncio.run(agent.arun("Research 600519 price and history", ticker="600519"))
    assert state["status"] == "finished"  # handled tool failure, not evidence of data acquisition
    assert len(fake.calls) == (2 if partial else 1)
    assert len(state["observations"]) == (1 if partial else 0)
    attempts = [e for e in agent.tracer.events if e.event_type == "tool_call"]
    assert len(attempts) == 2
    assert any("rate_limited:" in e.error for e in attempts if e.error)
    report = state["final_output"]
    assert "999999" not in report.model_dump_json()
    assert any("rate_limited:" in text for text in report.uncertainty)
    assert "historical prices" in " ".join(report.uncertainty) if partial else report.key_metrics == {}
    if partial:
        assert report.key_metrics["price"] == 12


def test_concurrent_tools_do_not_repeat_eastmoney_after_limit(monkeypatch):
    provider, fake = make_provider(monkeypatch, fault=Push2RateLimitError("fixture"))
    tools = {tool.name: tool for tool in build_tools(provider)}
    async def scenario():
        results = await asyncio.gather(*[tools[name].ainvoke({"ticker": "600519"}) for name in
            ("get_stock_price", "get_historical_prices", "calculate_rsi")], return_exceptions=True)
        assert all(isinstance(e, ProviderRateLimitError) for e in results)
    asyncio.run(scenario())
    assert len(fake.calls) == 1


def test_yfinance_rejects_bare_a_share_before_network(monkeypatch):
    monkeypatch.setattr(YFinanceMarketDataProvider, "_yf", lambda *a: pytest.fail("must not access Yahoo"))
    for method, args in (("get_stock_price", ("000001",)), ("get_historical_prices", ("600519", "1mo", "1d")),
                         ("get_income_statement", ("600519",))):
        with pytest.raises(ValueError, match="US-only"):
            getattr(YFinanceMarketDataProvider(), method)(*args)


@pytest.mark.parametrize("changes", [{"low": 20}, {"close": float("inf")}, {"volume": None}, {"volume": -1}])
def test_us_history_rejects_invalid_ohlcv(monkeypatch, changes):
    fake = FakeAkShare()
    original = fake.stock_us_hist_sina
    def invalid(**kw):
        frame = original(**kw).astype(object)
        for key, value in changes.items():
            frame.loc[0, key] = value
        return frame
    fake.stock_us_hist_sina = invalid
    monkeypatch.setattr(AkShareMarketDataProvider, "_ak", lambda self: fake)
    with pytest.raises(ValueError, match="No valid historical OHLC"):
        AkShareMarketDataProvider().get_historical_prices("AAPL", "1mo", "1d")
