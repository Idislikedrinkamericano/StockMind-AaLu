import asyncio
import json
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

from app.agents.react_agent import StockMindAgent
from app.graph.checkpoint import build_checkpointer
from app.graph.workflow import AgentConfig, _backoff_delay
from app.observability.tracing import TraceCollector
from app.schemas.report import StockResearchReport
from app.tools import build_tools
from app.tools.providers import MockMarketDataProvider


class DelayedSequenceModel(BaseChatModel):
    """Async fake model used to test cancellation without an external provider."""

    responses: list[tuple[float, AIMessage]]
    calls: int = 0
    structured_delay: float | None = None

    @property
    def _llm_type(self) -> str:
        return "delayed-sequence-test-model"

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        raise NotImplementedError("Tests use the async path")

    async def _agenerate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        self.calls += 1
        delay, message = self.responses.pop(0)
        await asyncio.sleep(delay)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        if self.structured_delay is None:
            raise NotImplementedError

        delay = self.structured_delay

        class SlowStructuredOutput:
            async def ainvoke(self, messages: list[BaseMessage]) -> StockResearchReport:
                await asyncio.sleep(delay)
                return StockResearchReport(ticker="NVDA", summary="late", conclusion="late")

        return SlowStructuredOutput()


def _final_json(ticker="NVDA"):
    return json.dumps(
        StockResearchReport(ticker=ticker, summary="ok", conclusion="ok").model_dump()
    )


def _scripted_agent():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}],
        ),
        AIMessage(content=_final_json()),
    ]
    return StockMindAgent(
        llm=GenericFakeChatModel(messages=iter(messages)),
        tools=build_tools(MockMarketDataProvider(seed=1)),
        config=AgentConfig(tool_timeout_seconds=5, tool_retries=0),
    )


def test_async_invoke_matches_sync():
    agent = _scripted_agent()
    sync_report = agent.invoke("Analyze NVDA", ticker="NVDA")
    async_report = asyncio.run(agent.ainvoke("Analyze NVDA", ticker="NVDA"))
    assert sync_report.ticker == "NVDA"
    assert async_report.ticker == "NVDA"


def test_async_run_returns_state():
    agent = _scripted_agent()
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    assert state["final_output"].ticker == "NVDA"
    assert state["status"] == "finished"


def test_checkpoint_state_is_retrievable():
    agent = _scripted_agent()
    asyncio.run(agent.ainvoke("Analyze NVDA", ticker="NVDA", thread_id="ckpt-1"))
    snapshot = asyncio.run(agent.aget_state("ckpt-1"))
    assert snapshot.values["status"] == "finished"
    assert snapshot.values["final_output"].ticker == "NVDA"


def test_resume_accumulates_messages():
    agent = _scripted_agent()
    first = asyncio.run(agent.arun("First query", ticker="NVDA", thread_id="resume-1"))
    second = asyncio.run(agent.arun("Follow-up query", ticker="NVDA", thread_id="resume-1"))
    assert len(second["messages"]) > len(first["messages"])


def test_sqlite_checkpointer():
    async def _run():
        async with build_checkpointer("sqlite", ":memory:") as saver:
            agent = StockMindAgent(
                llm=GenericFakeChatModel(
                    messages=iter(
                        [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {"name": "get_stock_price", "args": {"ticker": "AMD"}, "id": "s1"}
                                ],
                            ),
                            AIMessage(content=_final_json("AMD")),
                        ]
                    )
                ),
                tools=build_tools(MockMarketDataProvider(seed=1)),
                config=AgentConfig(tool_timeout_seconds=5, tool_retries=0),
                checkpointer=saver,
            )
            report = await agent.ainvoke("Analyze AMD", ticker="AMD", thread_id="sql-1")
            assert report.ticker == "AMD"
            snapshot = await agent.aget_state("sql-1")
            assert snapshot.values["status"] == "finished"

    asyncio.run(_run())


def test_backoff_grows_and_is_capped():
    config = AgentConfig(retry_backoff_seconds=0.5, retry_max_backoff_seconds=2.0, retry_jitter=0.0)
    delays = [_backoff_delay(attempt, config) for attempt in range(6)]
    assert delays[0] == pytest.approx(0.5)
    assert delays[1] == pytest.approx(1.0)
    assert all(delay <= 2.0 for delay in delays)
    assert delays[3] == pytest.approx(2.0)


def test_trace_summary_dedupes_repeated_errors():
    tracer = TraceCollector()
    tracer.start_run("query")
    for _ in range(3):
        tracer.record(
            event_type="tool_call",
            tool_name="get_stock_price",
            error="YFRateLimitError: Too Many Requests",
        )
    summary = tracer.summarize()
    assert summary.count("YFRateLimitError") == 1


def test_normal_async_llm_call_completes_within_timeout():
    model = DelayedSequenceModel(responses=[(0.001, AIMessage(content=_final_json()))])
    agent = StockMindAgent(
        llm=model,
        tools=[],
        config=AgentConfig(llm_timeout_seconds=0.1, run_timeout_seconds=1.0),
    )
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    assert state["status"] == "finished"
    assert model.calls == 1


def test_llm_call_timeout_returns_safe_no_data_report():
    model = DelayedSequenceModel(responses=[(0.05, AIMessage(content=_final_json()))])
    agent = StockMindAgent(
        llm=model,
        tools=[],
        config=AgentConfig(llm_timeout_seconds=0.01, run_timeout_seconds=1.0),
    )
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    assert state["status"] == "timeout"
    assert state["observations"] == []
    assert "no evidence" in state["final_output"].summary.lower()
    assert any(error.startswith("llm_timeout") for error in state["errors"])


def test_run_deadline_stops_slow_tool_before_another_agent_step():
    calls = [0]

    async def slow_price(ticker: str) -> str:
        calls[0] += 1
        await asyncio.sleep(0.05)
        return json.dumps({"ticker": ticker, "price": 100.0, "source": "fixture"})

    tool = StructuredTool.from_function(
        coroutine=slow_price, name="get_stock_price", description="Slow test price tool."
    )
    model = DelayedSequenceModel(
        responses=[
            (0.001, AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}])),
            (0.001, AIMessage(content=_final_json())),
        ]
    )
    agent = StockMindAgent(
        llm=model,
        tools=[tool],
        config=AgentConfig(llm_timeout_seconds=1.0, tool_timeout_seconds=1.0, run_timeout_seconds=0.02, tool_retries=0),
    )
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    assert state["status"] == "timeout"
    assert calls == [1]
    assert model.calls == 1
    assert state["observations"] == []
    assert any(error.startswith("run_timeout") for error in state["errors"])


def test_tool_retry_does_not_outlive_the_run_deadline():
    attempts = [0]

    async def failing_price(ticker: str) -> str:
        attempts[0] += 1
        raise RuntimeError("temporary failure")

    tool = StructuredTool.from_function(
        coroutine=failing_price, name="get_stock_price", description="Failing test price tool."
    )
    model = DelayedSequenceModel(
        responses=[(0.001, AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}]))]
    )
    agent = StockMindAgent(
        llm=model,
        tools=[tool],
        config=AgentConfig(
            llm_timeout_seconds=1.0,
            tool_timeout_seconds=1.0,
            run_timeout_seconds=0.02,
            tool_retries=5,
            retry_backoff_seconds=0.05,
            retry_jitter=0.0,
        ),
    )
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    assert state["status"] == "timeout"
    assert attempts == [1]
    assert model.calls == 1


def test_timeout_after_price_keeps_only_verified_partial_evidence():
    model = DelayedSequenceModel(
        responses=[
            (0.001, AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "NVDA"}, "id": "c1"}])),
            (0.05, AIMessage(content=_final_json())),
        ]
    )
    agent = StockMindAgent(
        llm=model,
        tools=build_tools(MockMarketDataProvider(seed=1)),
        config=AgentConfig(llm_timeout_seconds=0.01, run_timeout_seconds=1.0, tool_retries=0),
    )
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    report = state["final_output"]
    assert state["status"] == "timeout"
    assert [item["tool"] for item in state["observations"]] == ["get_stock_price"]
    assert report.key_metrics["price"] is not None
    assert report.valuation_analysis.startswith("Unavailable")


def test_final_structured_output_timeout_is_recorded_without_unverified_report():
    model = DelayedSequenceModel(
        responses=[(0.001, AIMessage(content=_final_json()))], structured_delay=0.2
    )
    agent = StockMindAgent(
        llm=model,
        tools=[],
        config=AgentConfig(llm_timeout_seconds=0.05, run_timeout_seconds=1.0),
    )
    state = asyncio.run(agent.arun("Analyze NVDA", ticker="NVDA"))
    assert state["status"] == "timeout"
    assert state["final_output"].key_metrics == {}
    assert any(error.startswith("llm_finalize_timeout") for error in state["errors"])
