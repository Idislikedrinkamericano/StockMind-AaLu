"""Financial semantics with fixed offline records, never live providers."""

import json
from datetime import datetime, timezone

import pytest

from app.reporting.assembler import build_grounded_report
from app.tools import analysis, build_tools
from app.tools.providers import (
    BalanceSheet, CashFlow, IncomeStatement, KeyStats, MockMarketDataProvider,
    StockPriceResult, YFinanceMarketDataProvider,
)


STAMP = datetime(2026, 8, 15, tzinfo=timezone.utc)


@pytest.fixture
def records(monkeypatch):
    common = {"ticker": "AMD", "timestamp": STAMP, "period_end": "2025-12-31"}
    data = {
        "get_stock_price": StockPriceResult(ticker="AMD", price=100, currency="USD",
                                            source="fixture/quote", timestamp=STAMP, as_of="2026-08-14"),
        "get_key_stats": KeyStats(**common, source="fixture/stats", market_cap=1000,
                                  shares_outstanding=10, shares_basis="reported_outstanding",
                                  eps_ttm=10, period="instant",
                                  field_metadata={"eps_ttm": {"period": "TTM"}}),
        "get_income_statement": IncomeStatement(**common, source="fixture/income", period="annual",
                                                revenue=500, ebitda=100, eps_diluted=5,
                                                published_at="2026-02-01"),
        "get_balance_sheet": BalanceSheet(**common, source="fixture/balance", period="instant",
                                          total_debt=300, cash_and_equivalents=100),
        "get_cash_flow": CashFlow(**common, source="fixture/cashflow", period="annual",
                                  free_cash_flow=100, cash_flow_basis="FCFF"),
    }
    provider = MockMarketDataProvider()
    for method in data:
        monkeypatch.setattr(provider, method, lambda ticker, key=method: data[key])
    return provider, data


def invoke(provider, name, **kwargs):
    tools = {tool.name: tool for tool in build_tools(provider)}
    return json.loads(tools[name].invoke({"ticker": "AMD", **kwargs}))


def dcf(**kwargs):
    args = dict(free_cash_flow=100, shares_outstanding=10, growth_rate=0,
                discount_rate=0.1, terminal_growth=0, years=1, cash_flow_basis="FCFE")
    args.update(kwargs)
    return analysis.compute_dcf(**args)


def test_dcf_distinguishes_enterprise_and_equity_value():
    fcff = dcf(cash_flow_basis="FCFF", net_debt=200)
    fcfe = dcf(net_debt=200)
    assert fcff["enterprise_value"] == pytest.approx(1000)
    assert fcff["equity_value"] == pytest.approx(800)
    assert fcff["intrinsic_value_per_share"] == pytest.approx(80)
    assert fcff["assumptions"]["discount_rate_basis"] == "WACC"
    assert fcfe["enterprise_value"] is None
    assert fcfe["equity_value"] == pytest.approx(1000)
    assert fcfe["intrinsic_value_per_share"] == pytest.approx(100)
    assert fcfe["assumptions"]["discount_rate_basis"] == "cost_of_equity"
    assert fcfe["assumptions"]["net_debt"] is None  # no double subtraction


def test_dcf_net_cash_is_not_assumed_zero():
    assert dcf(cash_flow_basis="FCFF", net_debt=-200)["intrinsic_value_per_share"] == 120
    assert dcf(cash_flow_basis="FCFF", net_debt=0)["intrinsic_value_per_share"] == 100


@pytest.mark.parametrize("overrides", [
    {"cash_flow_basis": "unknown"}, {"cash_flow_basis": "FCFF"},
    {"cash_flow_basis": "FCFF", "net_debt": float("nan")},
    {"cash_flow_basis": "FCFF", "net_debt": 2000},
    {"shares_outstanding": None}, {"shares_outstanding": 0}, {"shares_outstanding": -10},
    {"shares_outstanding": float("inf")}, {"free_cash_flow": None},
    {"free_cash_flow": 0}, {"free_cash_flow": -1}, {"free_cash_flow": float("nan")},
    {"discount_rate": 0}, {"discount_rate": -1}, {"discount_rate": float("inf")},
    {"terminal_growth": 0.1}, {"terminal_growth": 0.2}, {"terminal_growth": -1},
    {"terminal_growth": float("nan")}, {"growth_rate": -1}, {"growth_rate": float("inf")},
    {"growth_rate": 1e308, "years": 20}, {"years": 0}, {"years": 21},
    {"years": 1.5}, {"years": True},
])
def test_dcf_rejects_missing_or_invalid_assumptions(overrides):
    assert dcf(**overrides) is None


def test_dcf_does_not_infer_basis_for_legacy_call():
    assert analysis.compute_dcf(100, 10) is None


def test_dcf_negative_growth_above_minus_one_is_valid():
    assert dcf(growth_rate=-0.1, terminal_growth=-0.02)["intrinsic_value_per_share"] > 0


def test_tool_dcf_adjusts_net_debt_and_discloses_inputs(records):
    provider, _ = records
    result = invoke(provider, "calculate_dcf", growth_rate=0, terminal_growth=0, years=1)
    assert result["value"]["intrinsic_value_per_share"] == 80
    assert result["inputs"]["net_debt"] == 200
    assert result["input_provenance"]["total_debt"]["source"] == "fixture/balance"
    assert "Simplified two-stage scenario" in result["note"]


@pytest.mark.parametrize("record,field,value,reason", [
    ("get_cash_flow", "cash_flow_basis", "unknown", "cash-flow basis unknown"),
    ("get_cash_flow", "period", "unknown", "annual/TTM"),
    ("get_balance_sheet", "total_debt", None, "net debt unavailable"),
    ("get_balance_sheet", "cash_and_equivalents", None, "net debt unavailable"),
    ("get_balance_sheet", "period_end", "2024-12-31", "dates are unknown or inconsistent"),
    ("get_key_stats", "shares_outstanding", None, "confirmed outstanding shares"),
    ("get_key_stats", "shares_basis", "derived_market_cap_over_price", "confirmed outstanding shares"),
    ("get_key_stats", "shares_basis", "weighted_average_diluted", "confirmed outstanding shares"),
])
def test_tool_dcf_requires_evidence(records, record, field, value, reason):
    provider, data = records
    setattr(data[record], field, value)
    result = invoke(provider, "calculate_dcf")
    assert result["value"] is None
    assert result["status"] == "unavailable"
    assert reason in result["note"]


def test_tool_fcfe_does_not_request_or_subtract_net_debt(records, monkeypatch):
    provider, data = records
    data["get_cash_flow"].cash_flow_basis = "FCFE"
    monkeypatch.setattr(provider, "get_balance_sheet", lambda ticker: pytest.fail("FCFE needs no debt adjustment"))
    assert invoke(provider, "calculate_dcf", growth_rate=0, terminal_growth=0, years=1)["value"]["equity_value"] == 1000


def test_normal_ratios_retain_every_source_and_period(records):
    provider, _ = records
    pe = invoke(provider, "calculate_pe")
    ev = invoke(provider, "calculate_ev_ebitda")
    fy = invoke(provider, "calculate_fcf_yield")
    assert pe["value"] == 10
    assert ev["value"] == 12
    assert fy["value"] == 0.1
    assert pe["input_provenance"]["eps"]["period"] == "TTM"
    assert ev["input_provenance"]["ebitda"]["period"] == "annual"
    assert fy["input_provenance"]["free_cash_flow"]["period"] == "annual"
    assert ev["period_status"] == "mixed-period"
    assert "fixture/balance" in ev["source"] and "fixture/income" in ev["source"]
    assert "fixture/stats" in fy["source"] and "fixture/cashflow" in fy["source"]
    assert "cash-flow basis" in fy["note"]


def test_annual_eps_fallback_changes_period_and_source(records):
    provider, data = records
    data["get_key_stats"].eps_ttm = None
    data["get_income_statement"].timestamp = datetime(2026, 2, 2, tzinfo=timezone.utc)
    result = invoke(provider, "get_eps")
    assert result["eps"] == 5
    assert result["eps_ttm"] is None
    assert result["period"] == "annual"
    assert result["source"] == "fixture/income"
    assert result["timestamp"] == "2026-02-02T00:00:00+00:00"
    assert result["published_at"] == "2026-02-01"
    pe = invoke(provider, "calculate_pe")
    assert pe["value"] == 20
    assert pe["input_provenance"]["eps"]["source"] == "fixture/income"
    assert "annual fallback is not trailing P/E" in pe["note"]
    assert invoke(provider, "get_revenue")["period"] == "annual"


def test_zero_eps_is_not_replaced_with_positive_fallback(records, monkeypatch):
    provider, data = records
    data["get_key_stats"].eps_ttm = 0
    monkeypatch.setattr(provider, "get_income_statement", lambda ticker: pytest.fail("Zero is not missing"))
    assert invoke(provider, "get_eps")["eps_ttm"] == 0
    assert invoke(provider, "calculate_pe")["value"] is None


def test_unknown_period_is_never_fabricated_as_ttm(records):
    provider, data = records
    data["get_key_stats"].eps_ttm = None
    data["get_income_statement"].period = "unknown"
    data["get_income_statement"].period_end = None
    data["get_income_statement"].published_at = None
    eps = invoke(provider, "get_eps")
    assert eps["eps_ttm"] is None
    assert eps["period_end"] == "unknown"
    assert eps["published_at"] == "unavailable"
    pe = invoke(provider, "calculate_pe")
    assert pe["period_status"] == "unknown"
    assert "ending unknown" in pe["data_period"]
    assert pe["input_provenance"]["eps"]["source_updated_at"] == "unavailable"


def test_default_income_period_is_unknown():
    assert IncomeStatement(ticker="AMD", source="fixture", timestamp=STAMP).period == "unknown"


def test_report_retains_period_warnings_and_unavailable_dcf(records):
    provider, data = records
    data["get_cash_flow"].cash_flow_basis = "unknown"
    observations = [
        {"tool": name, "args": {"ticker": "AMD"}, "result": json.dumps(invoke(provider, name))}
        for name in ("calculate_dcf", "calculate_pe", "get_income_statement")
    ]
    report = build_grounded_report({"ticker": "AMD", "observations": observations})
    assert report.key_metrics["dcf_intrinsic_value"] is None
    assert any("cash-flow basis unknown" in note for note in report.uncertainty)
    assert any("mixed-period" in note for note in report.uncertainty)
    assert report.key_metrics["financial_metadata"]["income"]["period"] == "annual"


def test_yfinance_info_is_not_blanket_ttm_and_zero_eps_survives(monkeypatch):
    provider = YFinanceMarketDataProvider()
    monkeypatch.setattr(provider, "_info", lambda ticker: {
        "totalRevenue": 500, "ebitda": 100, "trailingEps": 0, "dilutedEPSTtm": 5,
        "freeCashflow": 100, "sharesOutstanding": 10,
    })
    income = provider.get_income_statement("AMD")
    assert income.period == "unknown"
    assert income.eps_diluted == 0
    assert income.field_metadata["eps_diluted"]["period"] == "TTM"
    assert provider.get_cash_flow("AMD").cash_flow_basis == "unknown"
    assert provider.get_key_stats("AMD").shares_basis == "reported_outstanding"
