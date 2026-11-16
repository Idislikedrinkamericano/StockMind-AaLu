import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agents.react_agent import StockMindAgent
from app.graph.workflow import AgentConfig
from app.models.demo import GROUNDED_REPORT_MARKER
from app.reporting.assembler import build_grounded_report
from app.schemas.report import StockResearchReport
from app.tools import build_tools
from app.tools.providers import MockMarketDataProvider


def _make_agent(messages, max_iterations=8):
    llm = GenericFakeChatModel(messages=iter(messages))
    tools = build_tools(MockMarketDataProvider(seed=1))
    return StockMindAgent(
        llm=llm,
        tools=tools,
        config=AgentConfig(max_iterations=max_iterations, tool_timeout_seconds=5, tool_retries=0),
    )


def _final_json(ticker="NVDA"):
    import json

    from app.schemas.report import StockResearchReport

    return json.dumps(
        StockResearchReport(ticker=ticker, summary="ok", conclusion="ok").model_dump()
    )


def test_agent_calls_tool_then_finalizes():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}],
        ),
        AIMessage(content=_final_json()),
    ]
    agent = _make_agent(messages)
    state = agent.run("Analyze NVDA price", ticker="NVDA")
    assert state["final_output"].ticker == "NVDA"
    assert len(state["observations"]) == 1
    assert state["observations"][0]["tool"] == "get_stock_price"
    assert state["status"] == "finished"


def test_max_iterations_terminates_gracefully():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": f"c{i}"}],
        )
        for i in range(20)
    ]
    agent = _make_agent(messages, max_iterations=3)
    state = agent.run("Analyze NVDA", ticker="NVDA")
    assert state["status"] == "finished"  # finalize always closes the run
    assert state["final_output"].ticker == "NVDA"
    assert state["iteration_count"] == 4  # one past the limit triggers termination


def test_duplicate_tool_call_skipped():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c2"}],
        ),
        AIMessage(content=_final_json()),
    ]
    agent = _make_agent(messages)
    state = agent.run("Analyze NVDA", ticker="NVDA")
    # Only the first identical call produced an observation.
    assert len(state["observations"]) == 1


def test_unknown_tool_is_recorded_as_error_but_does_not_crash():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "no_such_tool", "args": {}, "id": "c1"}],
        ),
        AIMessage(content=_final_json()),
    ]
    agent = _make_agent(messages)
    state = agent.run("Analyze NVDA", ticker="NVDA")
    assert state["final_output"].ticker == "NVDA"
    assert any("Unknown tool" in e for e in state["errors"])


def test_no_observations_does_not_leak_internal_marker():
    llm = GenericFakeChatModel(messages=iter([AIMessage(content=GROUNDED_REPORT_MARKER)]))
    agent = StockMindAgent(
        llm=llm,
        tools=build_tools(MockMarketDataProvider(seed=1)),
        config=AgentConfig(tool_timeout_seconds=5, tool_retries=0),
    )
    report = agent.invoke("Analyze NVDA", ticker="NVDA")
    assert GROUNDED_REPORT_MARKER not in report.summary
    assert GROUNDED_REPORT_MARKER not in report.conclusion
    assert report.summary
    assert "no evidence" in report.summary.lower()


def test_hallucinated_llm_metric_is_replaced_by_grounded_observation():
    hallucinated = StockResearchReport(
        ticker="NVDA",
        summary="NVDA is worth $999999.",
        conclusion="Buy based on the invented price.",
        key_metrics={"price": 999999.0, "pe": 0.01},
    )
    agent = _make_agent(
        [
            AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}]),
            AIMessage(content=hallucinated.model_dump_json()),
        ]
    )
    report = agent.invoke("Analyze NVDA", ticker="NVDA")
    tool_event = next(event for event in agent.tracer.events if event.tool_name == "get_stock_price")
    observed_price = json.loads(tool_event.tool_output)["price"]
    assert report.key_metrics["price"] == observed_price
    assert report.key_metrics["price"] != 999999.0
    assert "999999" not in report.summary


def test_wrong_llm_report_ticker_is_rejected_and_state_ticker_is_preserved():
    agent = _make_agent(
        [
            AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}]),
            AIMessage(content=_final_json("AMD")),
        ]
    )
    report = agent.invoke("Analyze NVDA", ticker="NVDA")
    assert report.ticker == "NVDA"
    assert any("report ticker" in (event.error or "") for event in agent.tracer.events)


def test_wrong_tool_ticker_is_blocked_in_single_stock_mode():
    agent = _make_agent(
        [
            AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "AMD"}, "id": "c1"}]),
            AIMessage(content=_final_json("NVDA")),
        ]
    )
    state = agent.run("Analyze NVDA", ticker="NVDA")
    assert state["observations"] == []
    assert any("Ticker mismatch" in error for error in state["errors"])
    assert state["final_output"].ticker == "NVDA"
    assert "no evidence" in state["final_output"].summary.lower()


def _observation(tool: str, payload: dict, ticker: str = "NVDA") -> dict:
    return {"tool": tool, "args": {"ticker": ticker}, "result": json.dumps(payload)}


def test_assembler_only_price_marks_other_sections_unavailable():
    state = {
        "ticker": "NVDA",
        "observations": [_observation("get_stock_price", {"ticker": "NVDA", "price": 100.0, "currency": "USD", "source": "fixture"})],
    }
    report = build_grounded_report(state)
    assert report is not None
    assert report.key_metrics["price"] == 100.0
    assert report.financial_analysis.startswith("Unavailable")
    assert report.valuation_analysis.startswith("Unavailable")
    assert report.bull_case == []


def test_assembler_partial_technical_evidence_does_not_create_valuation_or_news_claims():
    state = {
        "ticker": "NVDA",
        "observations": [
            _observation("calculate_rsi", {"metric": "rsi", "value": 50.0, "source": "fixture"}),
        ],
    }
    report = build_grounded_report(state)
    assert report is not None
    assert "RSI 50.0" in report.technical_analysis
    assert report.valuation_analysis.startswith("Unavailable")
    assert report.news_analysis.startswith("Unavailable")
    assert all("P/E" not in item for item in report.bull_case)


def test_assembler_news_failure_is_reported_as_unavailable_without_a_news_claim():
    state = {
        "ticker": "NVDA",
        "errors": ["search_company_news: timeout"],
        "observations": [_observation("get_stock_price", {"ticker": "NVDA", "price": 100.0, "currency": "USD", "source": "fixture"})],
    }
    report = build_grounded_report(state)
    assert report is not None
    assert report.news_analysis == "Unavailable: no valid company-news observation."
    assert all("News flow" not in item for item in report.bear_case)


def test_assembler_ignores_observation_for_the_wrong_ticker():
    state = {
        "ticker": "NVDA",
        "observations": [_observation("get_stock_price", {"ticker": "AMD", "price": 100.0, "source": "fixture"}, ticker="AMD")],
    }
    assert build_grounded_report(state) is None
