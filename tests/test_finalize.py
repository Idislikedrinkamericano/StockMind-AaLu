"""Offline Finalize regression tests: call counts, evidence, tracing and deadlines."""

import ast
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

from app.agents.react_agent import StockMindAgent
from app.graph.state import create_initial_state
from app.graph.workflow import AgentConfig, _make_finalize_node
from app.observability.tracing import TraceCollector
from app.reporting.assembler import build_grounded_report
from app.schemas.report import StockResearchReport


class CountingResearchModel(BaseChatModel):
    """Native-tool-binding mock that detects any extra LLM or structured call."""

    responses: list[AIMessage]
    calls: int = 0
    structured_calls: int = 0
    bound_tools: list[str] = []
    saw_tool_result: bool = False

    @property
    def _llm_type(self) -> str:
        return "counting-research-test-model"

    def bind_tools(self, tools, **kwargs: Any):
        self.bound_tools = [tool.name for tool in tools]
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        raise AssertionError("Tests use the async research path")

    async def _agenerate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        self.calls += 1
        self.saw_tool_result |= any(isinstance(message, ToolMessage) for message in messages)
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])

    def with_structured_output(self, schema: Any, **kwargs: Any):
        self.structured_calls += 1
        raise AssertionError("No report-generation LLM is allowed")


def _invented_report() -> str:
    return StockResearchReport(
        ticker="AMD", summary="invented source: price 999999",
        conclusion="Buy based on unverified valuation 777777",
        key_metrics={"price": 999999.0, "pe": 777777.0, "data_sources": ["invented source"]},
    ).model_dump_json()


def _agent(model, tools):
    return StockMindAgent(
        llm=model, tools=tools, tracer=TraceCollector(),
        config=AgentConfig(llm_timeout_seconds=1.0, run_timeout_seconds=5.0, tool_retries=0),
    )


def test_react_binds_and_calls_tools_then_finalizes_without_another_llm_call():
    tool_calls = []

    async def price(ticker: str) -> str:
        tool_calls.append(ticker)
        return json.dumps({"ticker": ticker, "price": 100.0, "source": "fixture"})

    tool = StructuredTool.from_function(coroutine=price, name="get_stock_price", description="Fixture price")
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    model = CountingResearchModel(responses=[
        AIMessage(content="", tool_calls=[{"name": tool.name, "args": {"ticker": "NVDA"}, "id": "p1"}], usage_metadata=usage),
        AIMessage(content=_invented_report(), usage_metadata=usage),
        AIMessage(content="This extra response must never be consumed."),
    ])
    agent = _agent(model, [tool])
    state = agent.run("Research NVDA", ticker="NVDA", thread_id="finalize-regression")

    assert model.bound_tools == ["get_stock_price"]
    assert tool_calls == ["NVDA"]
    assert model.saw_tool_result
    assert model.calls == 2
    assert len(model.responses) == 1
    assert model.structured_calls == 0
    assert state["status"] == "finished" and state["errors"] == []
    report = state["final_output"]
    expected = build_grounded_report(state)
    assert report.model_dump(exclude={"generated_at"}) == expected.model_dump(exclude={"generated_at"})
    assert report.ticker == "NVDA" and report.key_metrics["price"] == 100.0
    assert report.key_metrics["pe"] is None
    assert report.key_metrics["data_sources"] == ["fixture"]
    assert report.valuation_analysis.startswith("Unavailable")
    assert all(text not in report.model_dump_json() for text in ("AMD", "999999", "777777", "invented source"))

    snapshot = agent.get_state("finalize-regression")
    assert snapshot.next == ()
    assert snapshot.values["final_output"] == report
    assert snapshot.values["final_report"] == _invented_report()  # State contract is retained.
    events = agent.tracer.events
    assert [e.event_type for e in events] == ["run_start", "agent_step", "tool_call", "agent_step", "finalize", "run_end"]
    finalized = events[-2]
    assert finalized.latency_ms is not None and finalized.latency_ms >= 0
    assert finalized.token_usage is None and finalized.error is None
    assert finalized.final_status == events[-1].final_status == "finished"
    assert (agent.tracer.total_input_tokens, agent.tracer.total_output_tokens) == (20, 4)
    assert "report_assembly" in agent.tracer.summarize()
    assert "final_llm_call" not in agent.tracer.summarize()


@pytest.mark.parametrize("reasoning", [_invented_report(), "NVDA price 999999; P/E 777777", '{"ticker":'])
def test_finalize_does_not_treat_json_or_prose_as_evidence(reasoning):
    model = CountingResearchModel(responses=[AIMessage(content=reasoning)])
    agent = _agent(model, [])
    state = agent.run("Research NVDA", ticker="NVDA")
    report = state["final_output"]
    assert report.ticker == "NVDA"
    assert report.key_metrics == {} and report.bull_case == [] and report.bear_case == []
    assert "no evidence" in report.summary.lower()
    assert all(text not in report.model_dump_json() for text in ("999999", "777777", "invented source"))
    assert state["status"] == "finished" and state["errors"] == []
    assert model.calls == 1 and model.structured_calls == 0


def test_finalize_rejects_wrong_ticker_observation_from_a_tool():
    async def mismatched_price(ticker: str) -> str:
        return json.dumps({"ticker": "AMD", "price": 999999.0, "source": "wrong-stock"})

    tool = StructuredTool.from_function(coroutine=mismatched_price, name="get_stock_price", description="Mismatched fixture")
    model = CountingResearchModel(responses=[
        AIMessage(content="", tool_calls=[{"name": tool.name, "args": {"ticker": "NVDA"}, "id": "p1"}]),
        AIMessage(content=_invented_report()),
    ])
    state = _agent(model, [tool]).run("Research NVDA", ticker="NVDA")
    assert len(state["observations"]) == 1  # The observation exists, but cannot support this report.
    report = state["final_output"]
    assert report.ticker == "NVDA" and report.key_metrics == {}
    assert "no evidence" in report.summary.lower()
    assert "999999" not in report.model_dump_json()
    assert state["status"] == "finished"
    assert model.calls == 2 and model.structured_calls == 0


def test_assembly_exception_still_returns_safe_no_data(monkeypatch):
    def fail_assembly(*args):
        raise ValueError("fixture assembly failure")

    monkeypatch.setattr("app.graph.workflow.build_grounded_report", fail_assembly)
    state = _agent(CountingResearchModel(responses=[AIMessage(content=_invented_report())]), []).run(
        "Research NVDA", ticker="NVDA"
    )
    report = state["final_output"]
    assert report.ticker == "NVDA" and report.key_metrics == {}
    assert "no evidence" in report.summary.lower()
    assert any("fixture assembly failure" in reason for reason in report.uncertainty)


@pytest.mark.parametrize("already_timed_out", [False, True])
def test_finalize_preserves_deadline_and_timeout_status(already_timed_out):
    state = create_initial_state("Research NVDA", ticker="NVDA")
    state["metadata"]["run_deadline_monotonic"] = time.monotonic() - 1
    state["status"] = "timeout" if already_timed_out else "finished"
    state["errors"] = ["llm_timeout: fixture"] if already_timed_out else []
    state["observations"] = [{
        "tool": "get_stock_price", "args": {"ticker": "NVDA"},
        "result": json.dumps({"ticker": "NVDA", "price": 100.0, "source": "fixture"}),
    }]
    tracer = TraceCollector()
    tracer.start_run("Research NVDA")
    updates = asyncio.run(_make_finalize_node(tracer)(state))
    assert updates["status"] == "timeout"
    assert updates["final_output"].key_metrics["price"] == 100.0
    assert updates["final_output"].valuation_analysis.startswith("Unavailable")
    assert len(tracer.events) == 2
    event = tracer.events[-1]
    assert event.event_type == "finalize" and event.final_status == "timeout"
    assert event.token_usage is None
    if already_timed_out:
        assert "errors" not in updates  # No duplicate timeout error.
        assert state["errors"] == ["llm_timeout: fixture"]
    else:
        assert len(updates["errors"]) == 1
        assert updates["errors"][0].startswith("run_timeout")
        assert event.error == updates["errors"][0]


def test_research_cancellation_is_not_converted_to_finalize_success(monkeypatch):
    async def run_and_cancel():
        started = asyncio.Event()
        pending = asyncio.Event()

        async def wait_for_cancellation(self, messages, **kwargs):
            started.set()
            await pending.wait()

        monkeypatch.setattr(CountingResearchModel, "_agenerate", wait_for_cancellation)
        task = asyncio.create_task(agent.arun("Research NVDA", ticker="NVDA"))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    agent = _agent(CountingResearchModel(responses=[]), [])
    asyncio.run(run_and_cancel())
    assert all(e.event_type not in ("finalize", "run_end") for e in agent.tracer.events)


def test_finalize_does_not_swallow_cancelled_error(monkeypatch):
    def cancel_assembly(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.graph.workflow.build_grounded_report", cancel_assembly)
    tracer = TraceCollector()
    tracer.start_run("Research NVDA")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_make_finalize_node(tracer)(create_initial_state("Research NVDA", ticker="NVDA")))
    assert [e.event_type for e in tracer.events] == ["run_start"]


@pytest.mark.parametrize("language,expected", [("en", "deterministic"), ("zh", "确定性")])
def test_web_finalize_event_describes_assembly_without_gradio(language, expected):
    # Execute only the pure formatter, not the UI module or its imports.
    path = Path(__file__).resolve().parents[1] / "web" / "app.py"
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "describe_event")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    describe = namespace["describe_event"]
    assert expected in describe("finalize", {"final_status": "finished"}, language)
    assert describe("finalize_fallback", {}, language) is None
