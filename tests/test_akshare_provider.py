"""Offline contract tests for AkShare parsing; no AkShare package or network needed."""

import pandas as pd
import pytest

from app.tools.providers import AkShareMarketDataProvider


def _daily_df():
    return pd.DataFrame([
        {"date": pd.Timestamp("2026-08-14"), "open": 960.0, "high": 975.0, "low": 955.0, "close": 971.66, "volume": 1_000_000.0},
        {"date": pd.Timestamp("2026-08-15"), "open": 971.66, "high": 985.0, "low": 968.0, "close": 980.0, "volume": 1_200_000.0},
    ])


def _report_df(symbol):
    if symbol == "综合损益表":
        return pd.DataFrame([
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "营业总收入", "AMOUNT": 30e9},
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "归母净利润", "AMOUNT": 4e9},
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "EBITDA", "AMOUNT": 8e9},
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "稀释每股收益", "AMOUNT": 4.0},
        ])
    if symbol == "资产负债表":
        return pd.DataFrame([
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "资产总计", "AMOUNT": 50e9},
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "总债务", "AMOUNT": 10e9},
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "现金及现金等价物", "AMOUNT": 5e9},
        ])
    if symbol == "现金流量表":
        return pd.DataFrame([
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "经营活动产生的现金流量净额", "AMOUNT": 7e9},
            {"REPORT_DATE": "2025-12-31", "ITEM_NAME": "购建固定资产、无形资产和其他长期资产支付的现金", "AMOUNT": -2e9},
        ])
    return pd.DataFrame()


class _FakeAkShare:
    def __init__(self, daily=None, balance=None):
        self.daily = _daily_df() if daily is None else daily
        self.balance = balance

    def stock_us_daily(self, **kwargs):
        return self.daily

    def stock_financial_us_report_em(self, **kwargs):
        if kwargs["symbol"] == "资产负债表" and self.balance is not None:
            return self.balance
        return _report_df(kwargs["symbol"])

    def stock_us_valuation_baidu(self, *, indicator, **kwargs):
        return pd.DataFrame({"date": ["2026-08-15"], "value": [100e9 if indicator == "总市值" else 20.0]})


def _provider(monkeypatch, *, daily=None, balance=None):
    fake = _FakeAkShare(daily=daily, balance=balance)
    monkeypatch.setattr(AkShareMarketDataProvider, "_ak", lambda self: fake)
    return AkShareMarketDataProvider()


def _balance(*items):
    return pd.DataFrame([
        {"REPORT_DATE": "2025-12-31", "ITEM_NAME": name, "AMOUNT": amount} for name, amount in items
    ])


def test_akshare_price_uses_last_close(monkeypatch):
    result = _provider(monkeypatch).get_stock_price("MU")
    assert result.price == 980.0
    assert result.source == "akshare/sina"


def test_akshare_price_keeps_a_real_zero_distinct_from_missing(monkeypatch):
    daily = _daily_df()
    daily.loc[daily.index[-1], "close"] = 0.0
    assert _provider(monkeypatch, daily=daily).get_stock_price("MU").price == 0.0


def test_akshare_price_rejects_missing_or_invalid_last_close(monkeypatch):
    daily = _daily_df()
    daily.loc[daily.index[-1], "close"] = float("nan")
    with pytest.raises(ValueError, match="No price found"):
        _provider(monkeypatch, daily=daily).get_stock_price("MU")


def test_akshare_historical_skips_invalid_ohlc_without_synthetic_zero(monkeypatch):
    daily = pd.DataFrame([
        {"date": "2026-08-13", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1},
        {"date": "2026-08-14", "open": 10.0, "high": None, "low": 9.0, "close": 10.0, "volume": 1},
        {"date": "2026-08-15", "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 1},
    ])
    result = _provider(monkeypatch, daily=daily).get_historical_prices("MU", "1mo", "1d")
    assert [bar.close for bar in result.bars] == [10.0, 0.0]
    assert "Skipped 1 row" in result.note


def test_akshare_historical_rejects_all_invalid_ohlc(monkeypatch):
    daily = pd.DataFrame([
        {"date": "2026-08-15", "open": None, "high": None, "low": None, "close": None, "volume": 1}
    ])
    with pytest.raises(ValueError, match="No valid historical OHLC"):
        _provider(monkeypatch, daily=daily).get_historical_prices("MU", "1mo", "1d")


def test_akshare_income_statement(monkeypatch):
    result = _provider(monkeypatch).get_income_statement("MU")
    assert result.revenue == 30e9
    assert result.eps_diluted == 4.0
    assert result.ebitda == 8e9


def test_akshare_balance_sheet_prefers_reported_total_debt(monkeypatch):
    result = _provider(monkeypatch).get_balance_sheet("MU")
    assert result.total_debt == 10e9
    assert result.cash_and_equivalents == 5e9


def test_akshare_balance_sheet_sums_complete_short_and_long_term_debt(monkeypatch):
    balance = _balance(("短期借款", 3e9), ("长期借款", 7e9), ("负债合计", 30e9))
    assert _provider(monkeypatch, balance=balance).get_balance_sheet("MU").total_debt == 10e9


def test_akshare_balance_sheet_does_not_use_partial_debt_or_total_liabilities(monkeypatch):
    balance = _balance(("短期借款", 3e9), ("负债合计", 30e9))
    result = _provider(monkeypatch, balance=balance).get_balance_sheet("MU")
    assert result.total_debt is None
    assert "Total debt unavailable" in result.note


def test_akshare_cash_flow_fcf(monkeypatch):
    result = _provider(monkeypatch).get_cash_flow("MU")
    assert result.free_cash_flow == 5e9


def test_akshare_key_stats(monkeypatch):
    result = _provider(monkeypatch).get_key_stats("MU")
    assert result.market_cap == 100e9
    assert result.eps_ttm == 49.0
