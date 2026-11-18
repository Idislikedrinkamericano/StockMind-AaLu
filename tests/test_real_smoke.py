"""P3 regression tests are entirely offline, including tests of the real execution branch."""
import asyncio
import json
import socket
from types import SimpleNamespace

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

from app.evaluation import real_smoke as smoke
from app.models.demo import ScriptedReActDemoModel
from app.tools.providers import AkShareMarketDataProvider, YFinanceMarketDataProvider
from app.tools.base import ProviderRateLimitError


@pytest.fixture(autouse=True)
def no_network_or_user_configuration(monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
                "ANTHROPIC_BASE_URL", "STOCKMIND_LLM_PROVIDER", "STOCKMIND_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("External network access prohibited in offline tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-not-real")
    monkeypatch.setattr(smoke, "find_spec", lambda name: object())


@pytest.fixture
def provider(monkeypatch):
    history = pd.DataFrame(
        {"Open": range(100, 140), "High": range(100, 140), "Low": range(100, 140),
         "Close": range(100, 140), "Volume": [1000] * 40},
        index=pd.date_range("2025-01-01", periods=40))
    ticker = SimpleNamespace(fast_info=SimpleNamespace(last_price=150.0, currency="USD"),
                             info={"totalRevenue": 1000000, "netIncomeToCommon": 100000,
                                   "trailingEps": 3.0, "ebitda": 200000},
                             history=lambda **kwargs: history)
    monkeypatch.setattr(YFinanceMarketDataProvider, "_yf",
                        lambda self: SimpleNamespace(Ticker=lambda symbol: ticker))
    return ticker


def script(monkeypatch, calls, *, final="Invented price 999999 and wrong ticker NVDA"):
    messages = [AIMessage(content="", tool_calls=[
        {"name": name, "args": {"ticker": ticker}, "id": str(i)}
        for i, (name, ticker) in enumerate(calls)])] if calls else []
    messages.append(AIMessage(content=final))
    model = ScriptedReActDemoModel(messages=iter(messages))
    captured = {}
    def factory(provider, name, **kwargs):
        captured.update(provider=provider, model=name, **kwargs)
        return model
    monkeypatch.setattr(smoke, "get_chat_model", factory)
    return model, captured


def run(case=None, config=None, **kwargs):
    return asyncio.run(smoke.run_case(case or smoke.default_cases()[0],
                                     config or smoke.SmokeConfig.from_environment(), **kwargs))


def test_default_preflight_never_constructs_services(ready, monkeypatch):
    monkeypatch.setattr(smoke, "_build_agent", lambda *a: pytest.fail("must not build clients"))
    result = run()
    assert result["outcome"] == "blocked"
    assert result["execution_status"] == "not_started"
    assert result["actual_tool_calls"] == []
    assert result["final_report"] is None


@pytest.mark.parametrize("provider,key", [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")])
def test_missing_selected_credential_is_explicit(monkeypatch, provider, key):
    monkeypatch.setenv("ANTHROPIC_API_KEY" if provider == "openai" else "OPENAI_API_KEY", "other-secret")
    result = run(config=smoke.SmokeConfig.from_environment(llm_provider=provider), execute_real=True)
    assert result["outcome"] == "blocked"
    assert any(key in text for text in result["warnings"])


def test_env_and_provider_override_choose_matching_model(monkeypatch):
    monkeypatch.setenv("STOCKMIND_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("STOCKMIND_LLM_MODEL", "configured-model")
    assert smoke.SmokeConfig.from_environment().model == "configured-model"
    monkeypatch.delenv("STOCKMIND_LLM_MODEL")
    assert smoke.SmokeConfig.from_environment(llm_provider="openai").model == "gpt-4o-mini"


@pytest.mark.parametrize("values", [
    {"llm_provider": "fake"}, {"deadline": 0}, {"deadline": float("inf")},
    {"max_iterations": 0}, {"max_output_tokens": 99999},
])
def test_invalid_configuration_rejected(values):
    with pytest.raises(ValueError):
        smoke.SmokeConfig.from_environment(**values)


def test_missing_dependency_never_falls_back(ready, monkeypatch):
    monkeypatch.setattr(smoke, "find_spec", lambda name: None if name == "yfinance" else object())
    result = run(execute_real=True)
    assert result["outcome"] == "blocked"
    assert "Missing dependency: yfinance" in result["warnings"]


@pytest.mark.parametrize("url", ["file:///tmp/key", "https://user:password@host/v1",
                                "https://host/v1?token=secret", "https://host/v1#token"])
def test_unsafe_base_url_rejected(ready, monkeypatch, url):
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    reasons = smoke.preflight(smoke.default_cases()[0], smoke.SmokeConfig.from_environment(), execute_real=True)
    assert any("base URL" in reason for reason in reasons)
    assert url not in json.dumps(reasons)


def test_a_share_preflight_routes_only_to_akshare(ready):
    case = smoke.default_cases()[2]
    config = smoke.SmokeConfig.from_environment()
    assert smoke.preflight(case, config, execute_real=True) == []
    wrong_provider = case.model_copy(update={"financial_provider": "yfinance"})
    assert any("requires the AkShare" in reason for reason in
               smoke.preflight(wrong_provider, config, execute_real=True))


def test_a_share_still_blocked_without_key_or_explicit_flag(ready, monkeypatch):
    case = smoke.default_cases()[2]
    monkeypatch.setattr(smoke, "_build_agent", lambda *a: pytest.fail("must stay offline"))
    assert run(case)["outcome"] == "blocked"
    monkeypatch.delenv("OPENAI_API_KEY")
    result = run(case, execute_real=True)
    assert result["outcome"] == "blocked"
    assert "Missing credential: OPENAI_API_KEY" in result["warnings"]


def test_a_share_smoke_uses_actual_provider_routing_with_offline_boundaries(ready, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr("app.tools.providers.utc_now", lambda: datetime(2026, 11, 18, tzinfo=timezone.utc))
    calls = []
    def a_history(symbol, period, start_date, end_date, adjust):
        calls.append(symbol)
        return pd.DataFrame([{
            "日期": day.date(), "股票代码": symbol, "开盘": 10 + i, "最高": 12 + i,
            "最低": 9 + i, "收盘": 11 + i, "成交量": 1000 + i,
        } for i, day in enumerate(pd.date_range("2026-10-20", periods=25))])
    fake = SimpleNamespace(stock_zh_a_hist_em=a_history,
                           stock_us_hist_sina=lambda **kw: pytest.fail("wrong market"))
    monkeypatch.setattr(AkShareMarketDataProvider, "_ak", lambda self: fake)
    script(monkeypatch, [("get_stock_price", "600519"), ("get_historical_prices", "600519"),
                         ("calculate_rsi", "600519")])
    result = run(smoke.default_cases()[2], execute_real=True)
    assert result["outcome"] == "pass"  # Offline boundary stubs, never a real E2E claim.
    assert calls == ["600519"] * 3
    assert all(item["available"] for item in result["source_availability"])
    assert result["final_report"]["key_metrics"]["price"] == 35


@pytest.mark.parametrize("partial", [False, True])
def test_rate_limit_is_explicit_and_fails_smoke_even_with_partial_data(ready, provider, monkeypatch, partial):
    def limited(*a, **kw):
        raise ProviderRateLimitError("rate_limited: fixture upstream cooldown")
    monkeypatch.setattr(YFinanceMarketDataProvider, "get_income_statement", limited)
    calls = [("get_stock_price", "MSFT"), ("get_historical_prices", "MSFT")] if partial else []
    script(monkeypatch, [*calls, ("get_income_statement", "MSFT")])
    result = run(smoke.default_cases()[1], execute_real=True)
    assert result["outcome"] == "fail" and result["execution_status"] == "finished"
    assert not result["checks"]["no_rate_limit"]
    assert "no_rate_limit" in result["failure_reasons"]
    assert any("rate_limited:" in text for text in result["errors"])
    assert any("rate_limited:" in text for text in result["final_report"]["uncertainty"])
    assert len(result["actual_tool_calls"]) == (3 if partial else 1)
    assert json.loads(json.dumps(result, allow_nan=False))["outcome"] == "fail"


def test_real_factory_options_and_grounded_workflow(ready, provider, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    _, options = script(monkeypatch, [("get_stock_price", "AAPL"), ("get_historical_prices", "AAPL")])
    result = run(execute_real=True)
    # Simulated client/HTTP boundary, not a real-world pass claim.
    assert result["outcome"] == "pass"
    assert result["llm_attempt_count"] == 2
    assert options["base_url"] == "https://example.invalid/v1"
    assert options["max_retries"] == 0 and options["max_tokens"] == 1024 and options["timeout"] == 20
    assert result["final_report"]["key_metrics"]["price"] == 150
    assert "999999" not in json.dumps(result["final_report"])
    assert result["final_report"]["ticker"] == "AAPL"
    assert all(result["grounding"].values())
    assert len(result["actual_tool_calls"]) == 2


def test_report_without_tools_is_not_success(ready, monkeypatch):
    script(monkeypatch, [])
    result = run(execute_real=True)
    assert result["outcome"] == "fail"
    assert result["execution_status"] == "finished"
    assert not result["checks"]["sourced_data"]
    assert result["final_report"]["key_metrics"] == {}


def test_wrong_requested_ticker_never_reaches_provider(ready, monkeypatch):
    script(monkeypatch, [("get_stock_price", "NVDA")])
    monkeypatch.setattr(YFinanceMarketDataProvider, "get_stock_price",
                        lambda *a: pytest.fail("wrong ticker request reached provider"))
    result = run(execute_real=True)
    assert result["outcome"] == "fail"
    assert not result["actual_tool_calls"]
    assert any("Ticker mismatch" in text for text in result["errors"])
    assert result["final_report"]["key_metrics"] == {}


def test_wrong_observation_ticker_not_in_report(ready, provider, monkeypatch):
    original = YFinanceMarketDataProvider.get_stock_price
    monkeypatch.setattr(YFinanceMarketDataProvider, "get_stock_price",
                        lambda self, symbol: original(self, symbol).model_copy(update={"ticker": "NVDA"}))
    script(monkeypatch, [("get_stock_price", "AAPL")])
    result = run(execute_real=True)
    assert result["outcome"] == "fail"
    assert not result["checks"]["observation_tickers"]
    assert result["final_report"]["key_metrics"] == {}


@pytest.mark.parametrize("problem", ["missing", "exception"])
def test_provider_failures_produce_safe_no_data(ready, provider, monkeypatch, problem):
    if problem == "missing":
        provider.fast_info.last_price = None
        provider.info = {}
    else:
        def fail(*args):
            raise RuntimeError("provider unavailable")
        monkeypatch.setattr(YFinanceMarketDataProvider, "_yf", fail)
    script(monkeypatch, [("get_stock_price", "AAPL")])
    result = run(execute_real=True)
    assert result["outcome"] == "fail"
    assert result["errors"] and result["final_report"]["key_metrics"] == {}
    assert all(result["grounding"].values())


def test_controlled_partial_outage_requires_actual_error_and_real_price(ready, provider, monkeypatch):
    script(monkeypatch, [("get_stock_price", "AAPL"), ("get_income_statement", "AAPL")])
    result = run(smoke.default_cases()[3], execute_real=True)
    assert result["outcome"] == "pass"
    assert result["checks"]["controlled_outage_observed"]
    assert result["checks"]["missing_financials_disclosed"]
    assert result["final_report"]["key_metrics"]["price"] == 150
    assert result["final_report"]["key_metrics"]["revenue"] is None


def test_msft_requires_financial_and_technical_evidence(ready, provider, monkeypatch):
    case = smoke.default_cases()[1]
    script(monkeypatch, [("get_stock_price", "MSFT"), ("get_historical_prices", "MSFT")])
    assert run(case, execute_real=True)["outcome"] == "fail"
    script(monkeypatch, [("get_stock_price", "MSFT"), ("get_historical_prices", "MSFT"),
                         ("calculate_rsi", "MSFT"), ("get_income_statement", "MSFT")])
    assert run(case, execute_real=True)["outcome"] == "pass"


def test_optional_provider_failure_cannot_be_hidden_by_required_data(ready, provider, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("request_failed: fixture income statement unavailable")
    monkeypatch.setattr(YFinanceMarketDataProvider, "get_income_statement", unavailable)
    script(monkeypatch, [("get_stock_price", "AAPL"), ("get_historical_prices", "AAPL"),
                         ("get_income_statement", "AAPL")])
    result = run(execute_real=True)
    assert result["checks"]["required_tools"]  # The required quote/history really succeeded.
    assert result["outcome"] == "fail"
    assert not result["checks"]["no_unexpected_errors"]
    assert any("request_failed:" in text for text in result["final_report"]["uncertainty"])
    assert result["final_report"]["key_metrics"]["price"] == 150


def test_mock_sources_cannot_pass_real_smoke(ready, provider, monkeypatch):
    monkeypatch.setattr(YFinanceMarketDataProvider, "SOURCE", "mock/simulated")
    script(monkeypatch, [("get_stock_price", "AAPL"), ("get_historical_prices", "AAPL")])
    result = run(execute_real=True)
    assert result["outcome"] == "fail"
    assert not result["checks"]["sourced_data"]


class WaitingModel(ScriptedReActDemoModel):
    async def _agenerate(self, messages, **kwargs):
        await asyncio.Event().wait()


@pytest.mark.parametrize("deadline,llm_timeout,expected", [(0.02, 1, "run_timeout"), (1, 0.02, "llm_timeout")])
def test_deadline_and_llm_timeout_preserve_status(ready, monkeypatch, deadline, llm_timeout, expected):
    model = WaitingModel(messages=iter([]))
    monkeypatch.setattr(smoke, "get_chat_model", lambda *a, **kw: model)
    result = run(config=smoke.SmokeConfig.from_environment(deadline=deadline, llm_timeout=llm_timeout),
                 execute_real=True)
    assert result["outcome"] == "fail" and result["execution_status"] == "timeout"
    assert any(expected in error for error in result["errors"])
    assert result["actual_tool_calls"] == [] and result["final_report"]["key_metrics"] == {}


def test_cancellation_propagates(ready, monkeypatch):
    monkeypatch.setattr(smoke, "get_chat_model", lambda *a, **kw: WaitingModel(messages=iter([])))
    async def scenario():
        task = asyncio.create_task(smoke.run_case(smoke.default_cases()[0],
                                                smoke.SmokeConfig.from_environment(), execute_real=True))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_llm_outage_after_price_keeps_partial_report_and_error(ready, provider, monkeypatch):
    # Exhausting the scripted client raises through the real agent error path.
    message = AIMessage(content="", tool_calls=[{
        "name": "get_stock_price", "args": {"ticker": "AAPL"}, "id": "price"}])
    model = ScriptedReActDemoModel(messages=iter([message]))
    monkeypatch.setattr(smoke, "get_chat_model", lambda *a, **kw: model)
    result = run(execute_real=True)
    assert result["outcome"] == "fail" and result["execution_status"] == "error"
    assert result["final_report"]["key_metrics"]["price"] == 150
    assert all(result["grounding"].values())
    assert result["errors"] and len(result["actual_tool_calls"]) == 1


@pytest.mark.parametrize("provider,module", [("openai", "langchain_openai"), ("anthropic", "langchain_anthropic")])
def test_existing_llm_factory_is_reused_without_network(monkeypatch, provider, module):
    import importlib
    from app.models.providers import get_chat_model
    captured = {}
    def constructor(**kwargs):
        captured.update(kwargs)
        return "client-sentinel"
    monkeypatch.setattr(importlib.import_module(module),
                        "ChatOpenAI" if provider == "openai" else "ChatAnthropic", constructor)
    assert get_chat_model(provider, "configured-model", base_url="https://example.invalid/v1",
                          max_retries=0, timeout=20, max_tokens=1024) == "client-sentinel"
    assert captured["model"] == "configured-model" and captured["max_retries"] == 0
    assert captured["base_url"] == "https://example.invalid/v1"


def test_llm_failure_never_marked_success_and_secrets_redacted(ready, monkeypatch, tmp_path):
    def fail(*a, **kw):
        raise RuntimeError("fixture-secret-not-real https://user:pass@host?token=private Bearer secret")
    monkeypatch.setattr(smoke, "get_chat_model", fail)
    result = run(execute_real=True)
    directory = smoke.create_output_directory(tmp_path)
    path = smoke.save_result(result, directory)
    text = path.read_text()
    assert "fixture-secret-not-real" not in text and "user:pass" not in text and "Bearer secret" not in text
    assert json.loads(text)["execution_status"] == "error"
    assert result["outcome"] == "fail"
    with pytest.raises(FileExistsError):
        smoke.save_result(result, directory)


def test_serialization_report_paths_and_no_overwrite(ready, provider, monkeypatch, tmp_path):
    script(monkeypatch, [("get_stock_price", "AAPL"), ("get_historical_prices", "AAPL")])
    result = run(execute_real=True)
    directory = smoke.create_output_directory(tmp_path)
    saved = json.loads(smoke.save_result(result, directory).read_text())
    from pathlib import Path
    report = json.loads(Path(saved["final_report_path"]).read_text())
    assert report["ticker"] == "AAPL"
    assert saved["source_availability"][0]["timestamp"]
    assert smoke.create_output_directory(tmp_path) != directory


def test_output_protection(tmp_path):
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        smoke.create_output_directory(link)
    with pytest.raises(ValueError, match="protected"):
        smoke.create_output_directory(tmp_path / ".git" / "artifacts")
    with pytest.raises(ValueError, match="case_id"):
        smoke.save_result({"case_id": "../escape"}, tmp_path)


def test_default_artifacts_are_ignored_and_system_temp_alias_works(tmp_path):
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "check-ignore", "smoke-results/run-example/aapl-price-result.json"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0
    if str(tmp_path).startswith("/private/var/") and Path("/var").is_symlink():
        alias = Path("/var") / tmp_path.relative_to("/private/var")
        assert smoke.create_output_directory(alias).is_dir()


def test_cli_without_flag_and_custom_mock_rejection(ready, monkeypatch, tmp_path):
    from examples.real_smoke import main
    monkeypatch.setattr(smoke, "_build_agent", lambda *a: pytest.fail("live call"))
    assert asyncio.run(main(["--output-dir", str(tmp_path)])) == 2
    assert len(list(tmp_path.glob("run-*/*-result.json"))) == 4
    with pytest.raises(SystemExit):
        asyncio.run(main(["--ticker", "AAPL", "--query", "price", "--provider", "mock"]))


@pytest.mark.parametrize("fast,info,expected", [
    (float("nan"), {"regularMarketPrice": 123}, 123),
    (float("inf"), {"regularMarketPrice": 123}, 123),
    (0.0, {"regularMarketPrice": 123}, 0.0),
    (None, {"regularMarketPrice": 0.0, "currentPrice": 123}, 0.0),
    (None, {"regularMarketPrice": float("nan"), "currentPrice": 123}, 123),
    (None, {"previousClose": 90}, 90),
])
def test_yfinance_valid_fallback_and_zero(provider, fast, info, expected):
    provider.fast_info.last_price = fast
    provider.info = info
    assert YFinanceMarketDataProvider().get_stock_price("AAPL").price == expected


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "invalid"])
def test_yfinance_no_finite_price_is_unavailable(provider, bad):
    provider.fast_info.last_price = bad
    provider.info = {"regularMarketPrice": bad}
    with pytest.raises(ValueError, match="No price"):
        YFinanceMarketDataProvider().get_stock_price("AAPL")


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_yfinance_nonfinite_history_rejected(provider, bad):
    frame = provider.history()
    frame["Close"] = frame["Close"].astype(float)
    frame.iloc[-1, frame.columns.get_loc("Close")] = bad
    with pytest.raises(ValueError, match="Invalid historical OHLC"):
        YFinanceMarketDataProvider().get_historical_prices("AAPL", "1mo", "1d")


def test_yfinance_normal_history_preserved(provider):
    result = YFinanceMarketDataProvider().get_historical_prices("AAPL", "1mo", "1d")
    assert len(result.bars) == 40
    assert result.bars[-1].close == 139
    assert result.source == "yfinance"
