"""Per-case offline faults at model/tool/fixture boundaries, never production patches."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import Field

from app.agents.react_agent import StockMindAgent
from app.evaluation.fixtures import FixtureProvider
from app.evaluation.reliability_dataset import FailureInjection, ReliabilityCase
from app.graph.workflow import AgentConfig
from app.observability.tracing import TraceCollector
from app.tools import build_tools

# Evaluation budgets only; each blocking fault is cancelled by the real workflow.
OPERATION_TIMEOUT = 0.05
RUN_TIMEOUT = 2.0
CASE_TIMEOUT = 5.0


@dataclass
class InjectionJournal:
    events: list[dict[str, Any]] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    runner_error: str | None = None

    def record(self, kind: str, phase: str, **detail):
        self.events.append({"kind": kind, "phase": phase, **detail})


class FaultModel(BaseChatModel):
    responses: list[AIMessage]
    failure: FailureInjection
    journal: Any = Field(exclude=True)
    calls: int = 0
    enabled: bool = True

    @property
    def _llm_type(self):
        return "offline-reliability-fake"

    def _generate(self, messages, **kwargs):
        raise NotImplementedError("Reliability evaluation uses the async model path")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        fault = self.failure
        kind = fault.kind
        if self.enabled and self.calls == fault.step:
            if kind == "llm_exception":
                self.journal.record(kind, "raised", step=self.calls)
                raise RuntimeError("injected llm exception")
            if kind in {"llm_timeout", "checkpoint_terminal_replay"}:
                self.journal.record(kind, "waiting", step=self.calls)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.journal.record(kind, "cancelled", step=self.calls)
                    raise
        response = self.responses.pop(0)
        if self.enabled and self.calls == fault.step and response.tool_calls:
            calls = [dict(call, args=dict(call["args"])) for call in response.tool_calls]
            if kind in {"invalid_arguments", "wrong_ticker"}:
                calls[0]["args"].update(fault.replacement_args)
            elif kind == "unknown_tool":
                calls[0]["name"] = "nonexistent_reliability_tool"
            elif kind == "duplicate_batch":
                calls.append({**calls[0], "id": calls[0]["id"] + "-duplicate"})
            if kind in {"invalid_arguments", "wrong_ticker", "unknown_tool", "duplicate_batch"}:
                response = AIMessage(content=response.content, tool_calls=calls)
                self.journal.record(kind, "emitted", step=self.calls, calls=calls)
        return ChatResult(generations=[ChatGeneration(message=response)])


def build_fault_agent(case: ReliabilityCase, journal: InjectionJournal, language: str = "en",
                      *, checkpointer=None, tracer=None, resume=False) -> StockMindAgent:
    fault = case.failure

    class FailingFixture(FixtureProvider):
        price_attempts = 0

        def get_stock_price(self, ticker):
            self.price_attempts += 1
            if fault.kind == "provider_exception" and self.price_attempts == 1:
                journal.record(fault.kind, "raised", tool="get_stock_price", attempt=1)
                raise RuntimeError("injected provider exception")
            return super().get_stock_price(ticker)

    tools = build_tools(FailingFixture())
    if fault.kind in {"tool_timeout", "wrong_observation", "missing_data"}:
        original = next(tool for tool in tools if tool.name == fault.tool)
        attempts = 0

        async def injected(**kwargs):
            nonlocal attempts
            attempts += 1
            if fault.kind == "tool_timeout" and attempts == 1:
                journal.record(fault.kind, "waiting", tool=original.name, attempt=attempts)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    journal.record(fault.kind, "cancelled", tool=original.name, attempt=attempts)
                    raise
            if fault.kind in {"wrong_observation", "missing_data"}:
                output = (json.dumps({"ticker": "AMD" if case.ticker != "AMD" else "NVDA",
                                      "price": 999999.0, "source": "mock/invalid-fixture"})
                          if fault.kind == "wrong_observation" else "{}")
                journal.record(fault.kind, "returned", tool=original.name, output=output)
                return output
            return await original.ainvoke(kwargs)

        replacement = StructuredTool.from_function(coroutine=injected, name=original.name,
                                                   description=original.description, args_schema=original.args_schema)
        tools = [replacement if tool.name == fault.tool else tool for tool in tools]
    responses = [AIMessage(content="", tool_calls=[{
        "name": call.name, "args": call.args, "id": f"{case.case_id}-{index}",
    }]) for index, call in enumerate(case.script)] if not resume else []
    responses.append(AIMessage(content=case.final_text))
    model = FaultModel(responses=responses, failure=fault, journal=journal, enabled=not resume)
    return StockMindAgent(
        llm=model, tools=tools, language=language,
        config=AgentConfig(llm_timeout_seconds=OPERATION_TIMEOUT, tool_timeout_seconds=OPERATION_TIMEOUT,
                           run_timeout_seconds=RUN_TIMEOUT,
                           tool_retries=1 if fault.kind in {"provider_exception", "tool_timeout"} else 0,
                           retry_backoff_seconds=0.0, retry_jitter=0.0, language=language),
        checkpointer=checkpointer, tracer=tracer or TraceCollector(),
    )
