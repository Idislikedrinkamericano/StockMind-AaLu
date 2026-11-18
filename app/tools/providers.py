"""Data-provider abstraction.

Tools depend only on the :class:`MarketDataProvider` interface. Concrete
providers are swappable via configuration:

* :class:`MockMarketDataProvider` — deterministic, offline, clearly labelled as
  simulated data. Used for tests and demos.
* :class:`YFinanceMarketDataProvider` — real data via ``yfinance`` (optional
  dependency, requires network access).
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import re
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.tools.base import ProviderRateLimitError, utc_now


def normalize_stock_symbol(ticker: str) -> tuple[str, str]:
    """Recognize supported stock syntax, not listing validity or instrument type."""
    if not isinstance(ticker, str):
        raise ValueError("unsupported_symbol: stock codes must be strings; preserve leading zeros")
    symbol = ticker.strip().upper()
    if re.fullmatch(r"(?:00[0-3]|30[01]|60[0135]|688)[0-9]{3}", symbol):
        return symbol, "CN"
    if re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", symbol):
        return symbol, "US"
    raise ValueError(f"unsupported_symbol: {symbol!r}; use a US stock symbol or supported six-digit Shanghai/Shenzhen A-share code")


def _us_stock_symbol(ticker: str) -> str:
    symbol, market = normalize_stock_symbol(ticker)
    if market != "US":
        raise ValueError("unsupported_market: this endpoint is US-only; use AkShare for A-share daily prices")
    return symbol


def _as_float(value: Any) -> float | None:
    """Coerce a provider value to float, returning None for missing/NaN values."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


class StockPriceResult(BaseModel):
    ticker: str
    price: float
    currency: str
    source: str
    timestamp: datetime
    data_period: str | None = Field(default=None)
    as_of: str | None = None
    note: str | None = Field(default=None)


class PriceBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class HistoricalPricesResult(BaseModel):
    ticker: str
    period: str
    interval: str
    bars: list[PriceBar]
    source: str
    timestamp: datetime
    data_period: str | None = Field(default=None)
    note: str | None = Field(default=None)


class NewsItem(BaseModel):
    title: str
    source: str
    published_at: str | None = Field(default=None)
    url: str | None = Field(default=None)
    summary: str | None = Field(default=None)


class NewsResult(BaseModel):
    ticker: str
    query: str
    items: list[NewsItem]
    source: str
    timestamp: datetime
    note: str | None = Field(default=None)


class FinancialMetadata(BaseModel):
    """Source periods, never inferred from retrieval time; overrides are per field."""

    period: str = "unknown"
    period_end: str | None = None
    published_at: str | None = None
    source_updated_at: str | None = None
    field_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)


class IncomeStatement(FinancialMetadata):
    ticker: str
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    ebitda: float | None = None
    net_income: float | None = None
    eps_diluted: float | None = None
    source: str
    timestamp: datetime
    note: str | None = None


class BalanceSheet(FinancialMetadata):
    ticker: str
    total_assets: float | None = None
    total_liabilities: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    total_equity: float | None = None
    source: str
    timestamp: datetime
    note: str | None = None


class CashFlow(FinancialMetadata):
    ticker: str
    operating_cash_flow: float | None = None
    investing_cash_flow: float | None = None
    financing_cash_flow: float | None = None
    free_cash_flow: float | None = None
    cash_flow_basis: str = "unknown"
    source: str
    timestamp: datetime
    note: str | None = None


class KeyStats(FinancialMetadata):
    ticker: str
    eps_ttm: float | None = None
    shares_outstanding: float | None = None
    shares_basis: str = "unknown"
    market_cap: float | None = None
    beta: float | None = None
    source: str
    timestamp: datetime
    note: str | None = None


class MarketDataProvider(ABC):
    """Interface all market/news data providers must implement."""

    @abstractmethod
    def get_stock_price(self, ticker: str) -> StockPriceResult:
        """Return the latest available price for ``ticker``."""

    @abstractmethod
    def get_historical_prices(
        self, ticker: str, period: str, interval: str
    ) -> HistoricalPricesResult:
        """Return historical price bars for ``ticker``."""

    @abstractmethod
    def search_company_news(self, ticker: str, query: str, limit: int) -> NewsResult:
        """Return recent company/market news for ``ticker``."""

    @abstractmethod
    def search_market_news(self, query: str, limit: int) -> NewsResult:
        """Return recent market-wide news (not tied to a single ticker)."""

    @abstractmethod
    def get_income_statement(self, ticker: str) -> IncomeStatement:
        """Return the latest income statement for ``ticker``."""

    @abstractmethod
    def get_balance_sheet(self, ticker: str) -> BalanceSheet:
        """Return the latest balance sheet for ``ticker``."""

    @abstractmethod
    def get_cash_flow(self, ticker: str) -> CashFlow:
        """Return the latest cash flow statement for ``ticker``."""

    @abstractmethod
    def get_key_stats(self, ticker: str) -> KeyStats:
        """Return key valuation/ownership statistics for ``ticker``."""


class MockMarketDataProvider(MarketDataProvider):
    """Deterministic, offline provider that is *explicitly* simulated."""

    SOURCE = "mock"
    NOTE = "Simulated data for demonstration only — not real market data."

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    def _rng(self, ticker: str, salt: str) -> random.Random:
        digest = hashlib.sha256(f"{ticker.upper()}:{salt}:{self.seed}".encode()).hexdigest()
        return random.Random(int(digest, 16) % (2**32))

    def _base_price(self, ticker: str) -> float:
        rng = self._rng(ticker, "base")
        return round(rng.uniform(20.0, 800.0), 2)

    def _current_price(self, ticker: str) -> float:
        rng = self._rng(ticker, "price")
        return round(self._base_price(ticker) * rng.uniform(0.98, 1.02), 2)

    def get_stock_price(self, ticker: str) -> StockPriceResult:
        symbol = ticker.upper().strip()
        price = self._current_price(symbol)
        return StockPriceResult(
            ticker=symbol,
            price=price,
            currency="USD",
            source=self.SOURCE,
            timestamp=utc_now(),
            data_period="latest close",
            note=self.NOTE,
        )

    def get_historical_prices(
        self, ticker: str, period: str, interval: str
    ) -> HistoricalPricesResult:
        symbol = ticker.upper().strip()
        rng = self._rng(symbol, "history")
        days = self._period_to_days(period)
        current = self._current_price(symbol)
        today = datetime.now(timezone.utc).date()

        # Build closes backwards from the current price so the final bar equals
        # get_stock_price(), keeping the simulated dataset internally consistent.
        closes = [0.0] * days
        closes[-1] = current
        for i in range(days - 2, -1, -1):
            drift = rng.uniform(-0.03, 0.03)
            closes[i] = round(max(1.0, closes[i + 1] / (1 + drift)), 2)

        bars: list[PriceBar] = []
        for i, close in enumerate(closes):
            open_ = close * rng.uniform(0.99, 1.01)
            high = max(open_, close) * rng.uniform(1.0, 1.02)
            low = min(open_, close) * rng.uniform(0.98, 1.0)
            volume = int(rng.uniform(1_000_000, 80_000_000))
            bars.append(
                PriceBar(
                    date=(today - timedelta(days=days - i - 1)).isoformat(),
                    open=round(open_, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=close,
                    volume=volume,
                )
            )
        return HistoricalPricesResult(
            ticker=symbol,
            period=period,
            interval=interval,
            bars=bars,
            source=self.SOURCE,
            timestamp=utc_now(),
            data_period=f"last {days} calendar days",
            note=self.NOTE,
        )

    def search_company_news(self, ticker: str, query: str, limit: int) -> NewsResult:
        symbol = ticker.upper().strip()
        rng = self._rng(symbol, "news")
        templates = [
            "{t} reports quarterly results in line with market expectations",
            "Analysts adjust price targets for {t} on demand outlook",
            "{t} announces new product roadmap and capex plans",
            "Regulatory review adds uncertainty to {t} outlook",
            "Macro concerns weigh on semiconductor names including {t}",
        ]
        items: list[NewsItem] = []
        for i in range(max(1, min(limit, len(templates)))):
            title = rng.choice(templates).format(t=symbol)
            items.append(
                NewsItem(
                    title=f"[MOCK] {title}",
                    source="simulated-feed",
                    published_at=(datetime.now(timezone.utc) - timedelta(days=i)).isoformat(),
                    url=None,
                    summary="Simulated headline for demonstration only.",
                )
            )
        return NewsResult(
            ticker=symbol,
            query=query or "",
            items=items,
            source=self.SOURCE,
            timestamp=utc_now(),
            note=self.NOTE,
        )

    def search_market_news(self, query: str, limit: int) -> NewsResult:
        rng = self._rng("MARKET", "news")
        templates = [
            "Equity indices mixed as investors weigh inflation data",
            "Treasury yields edge higher ahead of central bank meeting",
            "Sector rotation continues as growth names lag value",
            "Oil prices move on supply outlook",
            "Markets focus on upcoming earnings season",
        ]
        items: list[NewsItem] = []
        for i in range(max(1, min(limit, len(templates)))):
            title = rng.choice(templates)
            items.append(
                NewsItem(
                    title=f"[MOCK] {title}",
                    source="simulated-feed",
                    published_at=(datetime.now(timezone.utc) - timedelta(days=i)).isoformat(),
                    url=None,
                    summary="Simulated market headline for demonstration only.",
                )
            )
        return NewsResult(
            ticker="MARKET",
            query=query or "",
            items=items,
            source=self.SOURCE,
            timestamp=utc_now(),
            note=self.NOTE,
        )

    def _financials(self, ticker: str) -> dict[str, Any]:
        """Deterministic, internally-consistent simulated financial statements."""
        symbol = ticker.upper().strip()
        rng = self._rng(symbol, "financials")
        revenue = round(rng.uniform(3e9, 120e9), 2)
        net_margin = rng.uniform(0.08, 0.35)
        net_income = round(revenue * net_margin, 2)
        shares = round(rng.uniform(5e8, 3e10), 0)
        eps = round(net_income / shares, 2)
        price = self._current_price(symbol)
        market_cap = round(price * shares, 2)
        ebitda = round(revenue * rng.uniform(0.20, 0.50), 2)
        gross_profit = round(revenue * rng.uniform(0.40, 0.75), 2)
        operating_income = round(revenue * rng.uniform(0.15, 0.40), 2)
        total_assets = round(revenue * rng.uniform(0.80, 2.00), 2)
        total_liabilities = round(total_assets * rng.uniform(0.30, 0.70), 2)
        total_debt = round(total_assets * rng.uniform(0.05, 0.25), 2)
        cash = round(total_assets * rng.uniform(0.05, 0.30), 2)
        total_equity = round(total_assets - total_liabilities, 2)
        operating_cash_flow = round(net_income * rng.uniform(1.0, 1.6), 2)
        capex = round(revenue * rng.uniform(0.02, 0.08), 2)
        free_cash_flow = round(operating_cash_flow - capex, 2)
        beta = round(rng.uniform(0.8, 1.8), 2)
        return {
            "revenue": revenue,
            "gross_profit": gross_profit,
            "operating_income": operating_income,
            "ebitda": ebitda,
            "net_income": net_income,
            "eps_diluted": eps,
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "total_debt": total_debt,
            "cash_and_equivalents": cash,
            "total_equity": total_equity,
            "operating_cash_flow": operating_cash_flow,
            "investing_cash_flow": -capex,
            "financing_cash_flow": round(-net_income * rng.uniform(0.2, 0.8), 2),
            "free_cash_flow": free_cash_flow,
            "eps_ttm": eps,
            "shares_outstanding": shares,
            "market_cap": market_cap,
            "beta": beta,
        }

    def get_income_statement(self, ticker: str) -> IncomeStatement:
        symbol = ticker.upper().strip()
        f = self._financials(symbol)
        return IncomeStatement(
            ticker=symbol,
            period="TTM",
            revenue=f["revenue"],
            gross_profit=f["gross_profit"],
            operating_income=f["operating_income"],
            ebitda=f["ebitda"],
            net_income=f["net_income"],
            eps_diluted=f["eps_diluted"],
            source=self.SOURCE,
            timestamp=utc_now(),
            note=self.NOTE,
        )

    def get_balance_sheet(self, ticker: str) -> BalanceSheet:
        symbol = ticker.upper().strip()
        f = self._financials(symbol)
        return BalanceSheet(
            ticker=symbol,
            total_assets=f["total_assets"],
            total_liabilities=f["total_liabilities"],
            total_debt=f["total_debt"],
            cash_and_equivalents=f["cash_and_equivalents"],
            total_equity=f["total_equity"],
            source=self.SOURCE,
            timestamp=utc_now(),
            note=self.NOTE,
        )

    def get_cash_flow(self, ticker: str) -> CashFlow:
        symbol = ticker.upper().strip()
        f = self._financials(symbol)
        return CashFlow(
            ticker=symbol,
            period="TTM",
            operating_cash_flow=f["operating_cash_flow"],
            investing_cash_flow=f["investing_cash_flow"],
            financing_cash_flow=f["financing_cash_flow"],
            free_cash_flow=f["free_cash_flow"],
            source=self.SOURCE,
            timestamp=utc_now(),
            note=self.NOTE,
        )

    def get_key_stats(self, ticker: str) -> KeyStats:
        symbol = ticker.upper().strip()
        f = self._financials(symbol)
        return KeyStats(
            ticker=symbol,
            shares_basis="simulated_outstanding",
            field_metadata={"eps_ttm": {"period": "TTM"}},
            eps_ttm=f["eps_ttm"],
            shares_outstanding=f["shares_outstanding"],
            market_cap=f["market_cap"],
            beta=f["beta"],
            source=self.SOURCE,
            timestamp=utc_now(),
            note=self.NOTE,
        )

    @staticmethod
    def _period_to_days(period: str) -> int:
        mapping = {
            "1d": 1,
            "5d": 5,
            "1mo": 30,
            "3mo": 90,
            "6mo": 180,
            "1y": 365,
            "2y": 730,
            "5y": 1825,
        }
        return mapping.get(period, 30)


class YFinanceMarketDataProvider(MarketDataProvider):
    """Real market data via ``yfinance``. Lazily imported so the base install
    has no network dependency."""

    SOURCE = "yfinance"

    def _yf(self) -> Any:
        try:
            import yfinance  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "yfinance is not installed. Install it with: pip install -e '.[real-data]'"
            ) from exc
        return yfinance

    def get_stock_price(self, ticker: str) -> StockPriceResult:
        symbol = _us_stock_symbol(ticker)
        t = self._yf().Ticker(symbol)
        price = None
        currency = "USD"
        # fast_info hits a single, lighter endpoint and is usually more reliable.
        try:
            fast = t.fast_info
            price = _as_float(fast.last_price)
            currency = fast.currency or "USD"
        except Exception:
            price = None
        if price is None:
            info = t.info or {}
            # Missing/non-finite quotes are not prices; a genuine zero is.
            price = next((value for key in ("regularMarketPrice", "currentPrice", "previousClose")
                          if (value := _as_float(info.get(key))) is not None), None)
            currency = info.get("currency", "USD")
        if price is None:
            raise ValueError(f"No price found for ticker {symbol!r}")
        return StockPriceResult(
            ticker=symbol,
            price=float(price),
            currency=currency,
            source=self.SOURCE,
            timestamp=utc_now(),
            data_period="latest close",
        )

    def get_historical_prices(
        self, ticker: str, period: str, interval: str
    ) -> HistoricalPricesResult:
        symbol = _us_stock_symbol(ticker)
        df = self._yf().Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"No historical data found for ticker {symbol!r}")
        bars = []
        for index, row in df.iterrows():
            values = [_as_float(row.get(key)) for key in ("Open", "High", "Low", "Close")]
            if any(value is None for value in values):
                raise ValueError(f"Invalid historical OHLC data for ticker {symbol!r}")
            bars.append(PriceBar(
                date=str(index.date()),
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume=int(row["Volume"]),
            ))
        return HistoricalPricesResult(
            ticker=symbol,
            period=period,
            interval=interval,
            bars=bars,
            source=self.SOURCE,
            timestamp=utc_now(),
            data_period=period,
        )

    def search_company_news(self, ticker: str, query: str, limit: int) -> NewsResult:
        symbol = ticker.upper().strip()
        raw = self._yf().Search(symbol, news_count=limit, include_research=False).news or []
        items = [
            NewsItem(
                title=str(item.get("title", "Untitled")),
                source=str(item.get("publisher", "unknown")),
                published_at=item.get("providerPublishTime"),
                url=item.get("link"),
                summary=None,
            )
            for item in raw[:limit]
        ]
        return NewsResult(
            ticker=symbol,
            query=query or "",
            items=items,
            source=self.SOURCE,
            timestamp=utc_now(),
        )

    def search_market_news(self, query: str, limit: int) -> NewsResult:
        raw = self._yf().Search("stock market", news_count=limit, include_research=False).news or []
        items = [
            NewsItem(
                title=str(item.get("title", "Untitled")),
                source=str(item.get("publisher", "unknown")),
                published_at=item.get("providerPublishTime"),
                url=item.get("link"),
                summary=None,
            )
            for item in raw[:limit]
        ]
        return NewsResult(
            ticker="MARKET",
            query=query or "",
            items=items,
            source=self.SOURCE,
            timestamp=utc_now(),
        )

    def _info(self, ticker: str) -> dict[str, Any]:
        symbol = _us_stock_symbol(ticker)
        return self._yf().Ticker(symbol).info or {}

    def get_income_statement(self, ticker: str) -> IncomeStatement:
        symbol = ticker.upper().strip()
        info = self._info(symbol)
        return IncomeStatement(
            ticker=symbol,
            period="unknown",
            field_metadata={"eps_diluted": {"period": "TTM"}},
            revenue=_as_float(info.get("totalRevenue")),
            gross_profit=_as_float(info.get("grossProfits")),
            operating_income=_as_float(info.get("operatingIncome")),
            ebitda=_as_float(info.get("ebitda")),
            net_income=_as_float(info.get("netIncomeToCommon")),
            eps_diluted=_as_float(info.get("trailingEps") if info.get("trailingEps") is not None else info.get("dilutedEPSTtm")),
            source=self.SOURCE,
            timestamp=utc_now(),
        )

    def get_balance_sheet(self, ticker: str) -> BalanceSheet:
        symbol = ticker.upper().strip()
        info = self._info(symbol)
        return BalanceSheet(
            ticker=symbol,
            total_assets=_as_float(info.get("totalAssets")),
            total_liabilities=_as_float(info.get("totalLiab")),
            total_debt=_as_float(info.get("totalDebt")),
            cash_and_equivalents=_as_float(info.get("totalCash")),
            total_equity=_as_float(info.get("totalStockholderEquity")),
            source=self.SOURCE,
            timestamp=utc_now(),
        )

    def get_cash_flow(self, ticker: str) -> CashFlow:
        symbol = ticker.upper().strip()
        info = self._info(symbol)
        return CashFlow(
            ticker=symbol,
            operating_cash_flow=_as_float(info.get("operatingCashflow")),
            investing_cash_flow=_as_float(info.get("investingCashflow")),
            financing_cash_flow=_as_float(info.get("financingCashflow")),
            free_cash_flow=_as_float(info.get("freeCashflow")),
            source=self.SOURCE,
            timestamp=utc_now(),
        )

    def get_key_stats(self, ticker: str) -> KeyStats:
        symbol = ticker.upper().strip()
        info = self._info(symbol)
        return KeyStats(
            ticker=symbol,
            shares_basis="reported_outstanding",
            field_metadata={"eps_ttm": {"period": "TTM"}},
            eps_ttm=_as_float(info.get("trailingEps")),
            shares_outstanding=_as_float(info.get("sharesOutstanding")),
            market_cap=_as_float(info.get("marketCap")),
            beta=_as_float(info.get("beta")),
            source=self.SOURCE,
            timestamp=utc_now(),
        )


class AkShareMarketDataProvider(MarketDataProvider):
    """US daily data via Sina, Shanghai/Shenzhen A-share daily data via Eastmoney.

    Financial endpoints remain US-only. Rate limits latch per provider instance
    and source; callers must wait for upstream recovery before creating a new one.
    """

    SOURCE = "akshare/sina"
    NOTE = "US stock daily data via akshare (Sina); not a live quote."
    A_SOURCE = "akshare/eastmoney"
    A_NOTE = ("A-share daily OHLCV via akshare (Eastmoney); price currency CNY, not a live quote. "
              "Volume retained in endpoint units; no share/lot conversion inferred.")

    def __init__(self) -> None:
        self._rate_limits: dict[str, ProviderRateLimitError] = {}
        # Serialize Eastmoney requests so queued tools do not hit a known ban.
        self._eastmoney_lock = threading.Lock()

    def _request(self, ak: Any, function: Any, operation: str, source: str, **kwargs) -> Any:
        def request():
            if source in self._rate_limits:
                raise self._rate_limits[source]
            try:
                return function(**kwargs)
            except Exception as exc:
                # Push2RateLimitError inherits the exported AkShare RateLimitError.
                if isinstance(exc, getattr(ak, "RateLimitError", ())):
                    error = ProviderRateLimitError(
                        f"rate_limited: {source} {operation}: {type(exc).__name__}; "
                        "stop requests and wait for upstream cooldown; automatic retry disabled"
                    )
                    self._rate_limits[source] = error
                    raise error from exc
                raise RuntimeError(
                    f"request_failed: {source} {operation}: {type(exc).__name__}: {exc}"
                ) from exc
        if source == self.A_SOURCE:
            with self._eastmoney_lock:
                return request()
        return request()

    def _ak(self) -> Any:
        try:
            import akshare  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "akshare is not installed. Install it with: pip install -e '.[real-data]'"
            ) from exc
        return akshare

    @staticmethod
    def _period_days(period: str) -> int:
        return {
            "1d": 1,
            "5d": 5,
            "1mo": 30,
            "3mo": 90,
            "6mo": 180,
            "1y": 365,
            "2y": 730,
            "5y": 1825,
        }.get(period, 180)

    def _daily(self, ticker: str, adjust: str) -> Any:
        symbol = _us_stock_symbol(ticker)
        ak = self._ak()
        function = getattr(ak, "stock_us_hist_sina", None)
        if function is not None:
            return self._request(ak, function, "stock_us_hist_sina", self.SOURCE,
                                 symbol=symbol, period="daily", adjust=adjust)
        # Explicit legacy name only. Never retry a failed new API using the old one.
        function = getattr(ak, "stock_us_daily", None)
        if function is None:
            raise RuntimeError("unsupported_api: AkShare exposes neither stock_us_hist_sina nor stock_us_daily")
        return self._request(ak, function, "stock_us_daily", self.SOURCE,
                             symbol=symbol, adjust=adjust)

    @staticmethod
    def _valid_bar(open_, high, low, close, volume) -> bool:
        if any(value is None for value in (open_, high, low, close, volume)):
            return False
        return (0 <= low <= min(open_, close) <= max(open_, close) <= high
                and volume >= 0 and volume.is_integer())

    def _a_share_bars(self, symbol: str, period: str, adjust: str) -> list[PriceBar]:
        periods = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"}
        if period not in periods:
            raise ValueError(f"unsupported_period: {period!r}; daily A-share lookback required")
        end = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date()
        start = end - timedelta(days=self._period_days(period))
        ak = self._ak()
        function = getattr(ak, "stock_zh_a_hist_em", None)
        operation = "stock_zh_a_hist_em"
        if function is None:
            function = getattr(ak, "stock_zh_a_hist", None)
            operation = "stock_zh_a_hist"
        if function is None:
            raise RuntimeError("unsupported_api: AkShare exposes neither stock_zh_a_hist_em nor stock_zh_a_hist")
        df = self._request(ak, function, operation, self.A_SOURCE, symbol=symbol,
                           period="daily", start_date=start.strftime("%Y%m%d"),
                           end_date=end.strftime("%Y%m%d"), adjust=adjust)
        if df is None or df.empty:
            raise ValueError(f"empty_data: {self.A_SOURCE} has no daily OHLCV for {symbol}")
        required = {"日期", "开盘", "最高", "最低", "收盘", "成交量"}
        if not required <= set(df.columns):
            raise ValueError(f"invalid_data: {self.A_SOURCE} missing OHLCV columns for {symbol}")
        bars = []
        seen_dates = set()
        for _, row in df.iterrows():
            if "股票代码" in df.columns and row["股票代码"] != symbol:
                raise ValueError(f"invalid_data: {self.A_SOURCE} observation ticker mismatch for {symbol}")
            try:
                day = datetime.strptime(str(row["日期"])[:10], "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError(f"invalid_data: {self.A_SOURCE} invalid OHLCV date for {symbol}") from exc
            values = [_as_float(row[key]) for key in ("开盘", "最高", "最低", "收盘", "成交量")]
            if not self._valid_bar(*values) or not start <= day <= end or day in seen_dates:
                raise ValueError(f"invalid_data: {self.A_SOURCE} invalid OHLCV row for {symbol} on {day}")
            seen_dates.add(day)
            bars.append(PriceBar(date=day.isoformat(), open=values[0], high=values[1],
                                 low=values[2], close=values[3], volume=int(values[4])))
        return sorted(bars, key=lambda bar: bar.date)

    def get_stock_price(self, ticker: str) -> StockPriceResult:
        symbol, market = normalize_stock_symbol(ticker)
        if market == "CN":
            # A calendar month accommodates weekends/holidays; disclose the actual last date.
            latest = self._a_share_bars(symbol, "1mo", adjust="")[-1]
            return StockPriceResult(
                ticker=symbol, price=latest.close, currency="CNY", source=self.A_SOURCE,
                timestamp=utc_now(), data_period="latest available unadjusted daily close",
                as_of=latest.date, note=self.A_NOTE,
            )
        df = self._daily(symbol, adjust="")
        if df is None or getattr(df, "empty", True):
            raise ValueError(f"No price found for ticker {symbol!r}")
        price = _as_float(df.iloc[-1].get("close"))
        if price is None:
            raise ValueError(f"No price found for ticker {symbol!r}")
        return StockPriceResult(
            ticker=symbol,
            price=round(price, 2),
            currency="USD",
            source=self.SOURCE,
            timestamp=utc_now(),
            data_period="latest daily close",
            as_of=str(df.iloc[-1]["date"]) if "date" in df.columns else None,
            note=self.NOTE,
        )

    def get_historical_prices(
        self, ticker: str, period: str, interval: str
    ) -> HistoricalPricesResult:
        symbol, market = normalize_stock_symbol(ticker)
        if interval != "1d":
            raise ValueError("unsupported_interval: AkShare adapter supports daily (1d) bars only")
        if market == "CN":
            bars = self._a_share_bars(symbol, period, adjust="qfq")
            return HistoricalPricesResult(
                ticker=symbol, period=period, interval=interval, bars=bars, source=self.A_SOURCE,
                timestamp=utc_now(), data_period=f"{period} calendar lookback; {bars[0].date} to {bars[-1].date}",
                note=self.A_NOTE + " Prices are forward-adjusted (qfq).",
            )
        df = self._daily(symbol, adjust="qfq")
        if df is None or getattr(df, "empty", True):
            raise ValueError(f"No historical data found for ticker {symbol!r}")
        df = df.tail(self._period_days(period))
        bars: list[PriceBar] = []
        skipped_rows = 0
        for _, row in df.iterrows():
            open_ = _as_float(row.get("open"))
            high = _as_float(row.get("high"))
            low = _as_float(row.get("low"))
            close = _as_float(row.get("close"))
            date_val = row.get("date") or row.get("index")
            # PriceBar has non-optional OHLC fields. Never replace a missing or
            # invalid value with zero: omit the entire unusable bar instead.
            volume = _as_float(row.get("volume"))
            if not self._valid_bar(open_, high, low, close, volume) or date_val is None:
                skipped_rows += 1
                continue
            bars.append(
                PriceBar(
                    date=str(date_val)[:10],
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=int(volume),
                )
            )
        if not bars:
            raise ValueError(f"No valid historical OHLC data found for ticker {symbol!r}")
        note = self.NOTE
        if skipped_rows:
            note += f" Skipped {skipped_rows} row(s) with missing or invalid OHLCV values."
        return HistoricalPricesResult(
            ticker=symbol,
            period=period,
            interval=interval,
            bars=bars,
            source=self.SOURCE,
            timestamp=utc_now(),
            data_period=period,
            note=note,
        )

    def _report_data(self, ticker: str, statement: str) -> tuple[dict[str, float | None], dict[str, Any]]:
        """Latest annual report as ``{ITEM_NAME: AMOUNT}`` (best-effort)."""
        symbol = _us_stock_symbol(ticker)
        ak = self._ak()
        df = self._request(ak, ak.stock_financial_us_report_em, "stock_financial_us_report_em",
                           self.A_SOURCE, stock=symbol, symbol=statement, indicator="年报")
        if df is None or getattr(df, "empty", True):
            return {}, {}
        if "REPORT_DATE" not in df or df["REPORT_DATE"].dropna().empty:
            return {}, {}
        latest = df["REPORT_DATE"].max()
        sub = df[df["REPORT_DATE"] == latest]
        metadata = {"period": "annual", "period_end": str(latest)}
        if "NOTICE_DATE" in sub:
            dates = sub["NOTICE_DATE"].dropna().unique()
            if len(dates) == 1:
                metadata["published_at"] = str(dates[0])
        return (
            {str(row["ITEM_NAME"]): _as_float(row["AMOUNT"]) for _, row in sub.iterrows()},
            metadata,
        )

    @staticmethod
    def _find(items: dict[str, float | None], *keywords: str) -> float | None:
        for name, value in items.items():
            if value is not None and any(keyword in name for keyword in keywords):
                return value
        return None

    @staticmethod
    def _total_debt(items: dict[str, float | None]) -> float | None:
        """Return a reliable interest-bearing debt total, never a partial component.

        Eastmoney labels vary by issuer. A reported total is preferred. Without
        one, both explicit short-term and long-term debt components are required
        before calculating a total; total liabilities are deliberately excluded.
        """
        exact_total_labels = {"总债务", "债务合计", "有息负债合计"}
        for name, value in items.items():
            if name.strip() in exact_total_labels and value is not None:
                return value

        def component(labels: set[str]) -> float | None:
            for name, value in items.items():
                if name.strip() in labels and value is not None:
                    return value
            return None

        short_term = component({"短期借款", "短期债务", "一年内到期的长期债务", "一年内到期的非流动负债"})
        long_term = component({"长期借款", "长期债务", "应付债券"})
        if short_term is None or long_term is None:
            return None
        return short_term + long_term

    def _valuation_point(self, ticker: str, indicator: str) -> tuple[float | None, str | None]:
        symbol = _us_stock_symbol(ticker)
        ak = self._ak()
        df = self._request(ak, ak.stock_us_valuation_baidu, "stock_us_valuation_baidu",
                           "akshare/baidu", symbol=symbol, indicator=indicator, period="近一年")
        if df is not None and not getattr(df, "empty", True):
            date = df.iloc[-1].get("date")
            return _as_float(df["value"].iloc[-1]), str(date) if date is not None else None
        return None, None

    def get_key_stats(self, ticker: str) -> KeyStats:
        symbol = _us_stock_symbol(ticker)
        quote = self.get_stock_price(symbol)
        price = quote.price
        market_cap, cap_date = self._valuation_point(symbol, "总市值")
        pe, pe_date = self._valuation_point(symbol, "市盈率(TTM)")
        eps = round(price / pe, 2) if (price and pe and pe > 0) else None
        shares = round(market_cap / price, 0) if (market_cap and price) else None
        return KeyStats(
            ticker=symbol,
            shares_basis="derived_market_cap_over_price",
            field_metadata={
                "market_cap": {"source": "akshare/baidu", "period": "instant", "period_end": cap_date},
                "eps_ttm": {"period": "TTM", "note": "Derived from Sina price / Baidu TTM P/E; alignment unverified.",
                            "input_dates": {"price": quote.as_of, "pe": pe_date}},
                "shares_outstanding": {"note": "Derived from market cap / price; not reported share count.",
                                       "input_dates": {"price": quote.as_of, "market_cap": cap_date}},
            },
            eps_ttm=eps,
            shares_outstanding=shares,
            market_cap=market_cap,
            beta=None,
            source="akshare/baidu + akshare/sina",
            timestamp=utc_now(),
            note="Market cap / PE via Baidu; EPS/shares derived from price.",
        )

    def get_income_statement(self, ticker: str) -> IncomeStatement:
        symbol = ticker.upper().strip()
        items, metadata = self._report_data(symbol, "综合损益表")
        return IncomeStatement(
            ticker=symbol,
            **metadata,
            revenue=self._find(items, "营业总收入", "营业收入", "总收入"),
            gross_profit=self._find(items, "毛利"),
            operating_income=self._find(items, "营业利润"),
            ebitda=self._find(items, "EBITDA", "息税折旧摊销前利润"),
            net_income=self._find(items, "归母净利润", "净利润"),
            eps_diluted=self._find(items, "稀释每股收益", "基本每股收益", "每股收益"),
            source="akshare/eastmoney",
            timestamp=utc_now(),
            note="Parsed from Eastmoney income statement (latest annual, best-effort).",
        )

    def get_balance_sheet(self, ticker: str) -> BalanceSheet:
        symbol = ticker.upper().strip()
        items, metadata = self._report_data(symbol, "资产负债表")
        total_debt = self._total_debt(items)
        note = "Parsed from Eastmoney balance sheet (latest annual, best-effort)."
        if items and total_debt is None:
            note += " Total debt unavailable: no reported total or complete short-/long-term debt components."
        return BalanceSheet(
            ticker=symbol,
            **{**metadata, "period": "instant" if metadata else "unknown"},
            total_assets=self._find(items, "资产总计", "资产合计"),
            total_liabilities=self._find(items, "负债合计", "负债总计"),
            total_debt=total_debt,
            cash_and_equivalents=self._find(items, "现金及现金等价物", "货币资金"),
            total_equity=self._find(items, "股东权益合计", "所有者权益合计"),
            source="akshare/eastmoney",
            timestamp=utc_now(),
            note=note,
        )

    def get_cash_flow(self, ticker: str) -> CashFlow:
        symbol = ticker.upper().strip()
        items, metadata = self._report_data(symbol, "现金流量表")
        ocf = self._find(items, "经营活动产生的现金流量净额", "经营活动现金流量净额")
        capex = self._find(items, "购建固定资产", "资本支出")
        investing = self._find(items, "投资活动产生的现金流量净额", "投资活动现金流量净额")
        financing = self._find(items, "筹资活动产生的现金流量净额", "筹资活动现金流量净额")
        fcf = self._find(items, "自由现金流")
        if fcf is None and ocf is not None and capex is not None:
            fcf = ocf - abs(capex)
        return CashFlow(
            ticker=symbol,
            **metadata,
            operating_cash_flow=ocf,
            investing_cash_flow=investing,
            financing_cash_flow=financing,
            free_cash_flow=fcf,
            source="akshare/eastmoney",
            timestamp=utc_now(),
            note="Parsed from Eastmoney cash flow statement (latest annual, best-effort).",
        )

    def search_company_news(self, ticker: str, query: str, limit: int) -> NewsResult:
        return NewsResult(
            ticker=ticker.upper().strip(),
            query=query or "",
            items=[],
            source=self.SOURCE,
            timestamp=utc_now(),
            note="Company news not available via akshare.",
        )

    def search_market_news(self, query: str, limit: int) -> NewsResult:
        return NewsResult(
            ticker="MARKET",
            query=query or "",
            items=[],
            source=self.SOURCE,
            timestamp=utc_now(),
            note="Market news not available via akshare.",
        )


def get_data_provider(provider: str | None = None) -> MarketDataProvider:
    """Resolve the active data provider from an argument or environment."""
    name = (provider or os.getenv("STOCKMIND_DATA_PROVIDER") or "mock").strip().lower()
    if name == "mock":
        return MockMarketDataProvider()
    if name == "yfinance":
        return YFinanceMarketDataProvider()
    if name == "akshare":
        return AkShareMarketDataProvider()
    raise ValueError(f"Unsupported data provider: {name!r} (expected 'mock', 'yfinance' or 'akshare')")
