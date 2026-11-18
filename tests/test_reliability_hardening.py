"""Production-path regressions for duplicate calls and terminal-state integrity."""

import ast
import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langsmith import tracing_context

from app.agents.react_agent import StockMindAgent
from app.graph.checkpoint import build_checkpointer
from app.graph.state import create_initial_state
from app.graph.workflow import AgentConfig, _grounded_or_no_data_report, _make_finalize_node, _make_tools_node
from app.observability.tracing import TraceCollector
from app.tools.base import tool_call_signature


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Hardening tests must not use external models, providers or network connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.agents.react_agent.get_chat_model", forbidden)
    monkeypatch.setattr("app.agents.react_agent.get_data_provider", forbidden)
    with tracing_context(enabled=False):
        yield


class ResearchModel(BaseChatModel):
    responses: list[AIMessage]
    calls: int = 0
    fail_on: int | None = None

    @property
    def _llm_type(self):
        return "offline-hardening-regression"

    def _generate(self, messages, **kwargs):
        raise AssertionError("Use async Workflow execution")

    async def _agenerate(self, messages, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("fixture reasoning failure")
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def _call(call_id, *, name="get_stock_price", **args):
    return {"name": name, "args": args, "id": call_id}


def _tool(executions, *, name="get_stock_price", fail_first=False):
    async def quote(ticker: str, window: int = 14):
        executions.append((name, ticker, window))
        if fail_first and len(executions) == 1:
            raise RuntimeError("fixture temporary tool failure")
        return json.dumps({"ticker": ticker, "price": 100 if ticker == "NVDA" else 50, "source": "fixture"})

    return StructuredTool.from_function(coroutine=quote, name=name, description="Offline test quote")


def _agent(responses, tools, *, fail_on=None, saver=None, retries=0):
    return StockMindAgent(llm=ResearchModel(responses=responses, fail_on=fail_on), tools=tools,
                          config=AgentConfig(tool_retries=retries, retry_backoff_seconds=0, retry_jitter=0,
                                             llm_timeout_seconds=1, tool_timeout_seconds=1, run_timeout_seconds=5),
                          tracer=TraceCollector(), checkpointer=saver, language="en")


def test_signature_ignores_nested_json_key_order():
    left = {"ticker": "NVDA", "filters": {"period": "1mo", "window": 14}}
    right = {"filters": {"window": 14, "period": "1mo"}, "ticker": "NVDA"}
    assert tool_call_signature("quote", left) == tool_call_signature("quote", right)


@pytest.mark.parametrize("name,args", [
    ("other_tool", {"ticker": "NVDA", "window": 14}),
    ("quote", {"ticker": "AMD", "window": 14}),
    ("quote", {"ticker": "NVDA", "window": 20}),
])
def test_signature_does_not_collapse_different_requests(name, args):
    assert tool_call_signature("quote", {"ticker": "NVDA", "window": 14}) != tool_call_signature(name, args)


def test_tools_node_dedupes_history_and_batch_without_mutating_input_state():
    calls = []
    tool = _tool(calls)
    tracer = TraceCollector()
    tracer.start_run("Offline unit test")
    state = create_initial_state("NVDA", ticker="NVDA")
    historical = tool_call_signature(tool.name, {"ticker": "NVDA", "window": 14})
    state["tool_call_signatures"] = [historical]
    state["messages"].append(AIMessage(content="", tool_calls=[
        _call("new", ticker="NVDA", window=20),
        _call("same-batch", window=20, ticker="NVDA"),
        _call("historical", ticker="NVDA", window=14),
    ]))
    updates = asyncio.run(_make_tools_node([tool], AgentConfig(tool_retries=0), tracer)(state))
    assert calls == [(tool.name, "NVDA", 20)]
    assert state["tool_call_signatures"] == [historical] and state["observations"] == []
    assert len(updates["tool_call_signatures"]) == 2 and len(updates["observations"]) == 1
    assert updates["errors"] == [] and len(updates["tool_calls"]) == 3
    messages = {message.tool_call_id: json.loads(message.content) for message in updates["messages"]}
    assert messages["new"]["price"] == 100
    assert messages["same-batch"]["duplicate"] and messages["historical"]["duplicate"]
    skipped = [event for event in tracer.events if event.event_type == "tool_skipped"]
    assert len(skipped) == 2 and all("requested" in json.loads(event.tool_output)["message"] for event in skipped)


def test_workflow_keeps_distinct_requests_and_skips_duplicates_across_steps():
    executions = []
    tools = [_tool(executions), _tool(executions, name="test_other_quote")]
    agent = _agent([
        AIMessage(content="", tool_calls=[
            _call("first", ticker="NVDA", window=14),
            _call("same-batch", window=14, ticker="NVDA"),
            _call("other-args", ticker="NVDA", window=20),
            _call("other-tool", name="test_other_quote", ticker="NVDA", window=14),
        ]),
        AIMessage(content="", tool_calls=[_call("later-step", window=14, ticker="NVDA")]),
        AIMessage(content="Unverified price 999999 and P/E 777777 must not become evidence."),
    ], tools)
    state = agent.run("NVDA fixture only", ticker="NVDA")
    assert sorted(executions) == sorted([("get_stock_price", "NVDA", 14),
                                         ("get_stock_price", "NVDA", 20), ("test_other_quote", "NVDA", 14)])
    assert state["status"] == "finished" and state["errors"] == []
    assert len(state["observations"]) == len(state["tool_call_signatures"]) == 3
    assert len(state["tool_calls"]) == 5
    messages = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert {message.tool_call_id for message in messages} == {"first", "same-batch", "other-args", "other-tool", "later-step"}
    assert len([e for e in agent.tracer.events if e.event_type == "tool_call"]) == 3
    assert len([e for e in agent.tracer.events if e.event_type == "tool_skipped"]) == 2
    assert "skip_duplicate" in agent.tracer.summarize()
    assert state["final_output"].key_metrics["price"] == 100
    assert state["final_output"].key_metrics["pe"] is None
    assert "999999" not in state["final_output"].model_dump_json()


def test_deduplication_does_not_replace_bounded_retries():
    executions = []
    tool = _tool(executions, fail_first=True)
    agent = _agent([
        AIMessage(content="", tool_calls=[_call("first", ticker="NVDA"), _call("duplicate", ticker="NVDA")]),
        AIMessage(content="Collected available evidence."),
    ], [tool], retries=1)
    state = agent.run("NVDA", ticker="NVDA")
    attempts = [event for event in agent.tracer.events if event.event_type == "tool_call"]
    assert len(executions) == len(attempts) == 2
    assert "temporary tool failure" in attempts[0].error and attempts[1].error is None
    assert len(state["observations"]) == 1 and state["errors"] == [] and state["status"] == "finished"
    assert len([event for event in agent.tracer.events if event.event_type == "tool_skipped"]) == 1


def test_separate_tickers_threads_and_new_runs_do_not_share_dedupe_state():
    executions = []
    saver = build_checkpointer("memory")

    async def run():
        for ticker, thread in (("NVDA", "first"), ("AMD", "other"), ("NVDA", "first")):
            agent = _agent([
                AIMessage(content="", tool_calls=[_call("price", ticker=ticker), _call("duplicate", ticker=ticker)]),
                AIMessage(content="Done"),
            ], [_tool(executions)], saver=saver)
            state = await agent.arun("Fixture", ticker=ticker, thread_id=thread)
            assert state["status"] == "finished" and state["errors"] == []
            assert len(state["observations"]) == len(state["tool_call_signatures"]) == 1
            assert state["final_output"].ticker == ticker
            assert state["final_output"].key_metrics["price"] == (100 if ticker == "NVDA" else 50)
        return await agent.aget_state("other")

    other = asyncio.run(run())
    assert [entry[1] for entry in executions] == ["NVDA", "AMD", "NVDA"]
    assert other.values["ticker"] == "AMD" and other.values["final_output"].ticker == "AMD"


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_llm_exception_keeps_error_report_and_checkpoint(backend, partial):
    async def check(saver):
        executions = []
        responses = [AIMessage(content="", tool_calls=[_call("price", ticker="NVDA")])] if partial else []
        agent = _agent(responses, [_tool(executions)], fail_on=2 if partial else 1, saver=saver)
        # Report-only API remains compatible; status is available in trace/checkpoint.
        report = await agent.ainvoke("NVDA", ticker="NVDA", thread_id="failed-research")
        snapshot = await agent.aget_state("failed-research")
        state = snapshot.values
        assert snapshot.next == () and state["status"] == "error"
        assert state["errors"] == ["RuntimeError: fixture reasoning failure"]
        assert len(state["observations"]) == len(executions) == int(partial)
        assert state["final_output"] == report and report.ticker == "NVDA"
        expected = _grounded_or_no_data_report(state, "NVDA")
        assert report.model_dump(exclude={"generated_at"}) == expected.model_dump(exclude={"generated_at"})
        assert report.key_metrics.get("price") == (100 if partial else None)
        if not partial:
            assert report.key_metrics == {} and "no evidence" in report.summary
        events = agent.tracer.events
        assert [e.final_status for e in events if e.event_type in {"finalize", "run_end"}] == ["error", "error"]
        assert [e.final_status for e in events if e.event_type == "agent_step"][-1] == "error"
        assert "run_end (status=error)" in agent.tracer.summarize()
        assert agent.llm.calls == (2 if partial else 1)

    async def run():
        if backend == "sqlite":
            async with build_checkpointer("sqlite", ":memory:") as saver:
                await check(saver)
        else:
            await check(build_checkpointer("memory"))

    asyncio.run(run())


@pytest.mark.parametrize("status", ["finished", "error", "timeout", "max_iterations"])
@pytest.mark.parametrize("expired", [False, True])
def test_finalize_preserves_terminal_reason_and_deadline(status, expired):
    tracer = TraceCollector()
    tracer.start_run("Fixture")
    state = create_initial_state("NVDA", ticker="NVDA")
    state["status"] = status
    state["errors"] = ["fixture existing error"]
    state["metadata"]["run_deadline_monotonic"] = time.monotonic() + (-1 if expired else 10)
    result = asyncio.run(_make_finalize_node(tracer)(state))
    assert result["status"] == ("timeout" if expired else status)
    assert tracer.events[-1].final_status == result["status"]
    assert result["final_output"].key_metrics == {}
    assert state["errors"] == ["fixture existing error"]
    if expired and status != "timeout":
        assert result["errors"][0] == "fixture existing error"
        assert result["errors"][1].startswith("run_timeout")
    else:
        assert "errors" not in result


@pytest.mark.parametrize("language", ["en", "zh"])
def test_web_status_and_duplicate_messages_do_not_claim_success(language):
    path = Path(__file__).resolve().parents[1] / "web" / "app.py"
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "describe_event")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    describe = namespace["describe_event"]
    assert "✅" in describe("run_end", {"final_status": "finished"}, language)
    for status in ("error", "timeout", "max_iterations"):
        text = describe("run_end", {"final_status": status}, language)
        assert status in text and "✅" not in text
    assert "✅" not in describe("run_end", {}, language)
    text = describe("tool_skipped", {"tool_name": "get_stock_price"}, language)
    assert "get_stock_price" in text and ("重复" if language == "zh" else "duplicate") in text
    # Inspect only the UI's final text binding; no Gradio import or browser claim.
    handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_research")
    strings = [n.value for n in ast.walk(handler) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert all("✅" not in value for value in strings)
