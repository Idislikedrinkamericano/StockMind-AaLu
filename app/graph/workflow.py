"""The LangGraph ReAct execution loop (async-first).

Nodes:
  agent     -> LLM reasons and either emits tool calls or a final answer.
  tools     -> Executes tool calls concurrently with timeout + retry + backoff.
  finalize  -> Assembles :class:`StockResearchReport` from valid observations.

The loop is bounded by ``max_iterations``. Tool failures are recorded as
observations rather than crashing the run (graceful degradation). The graph is
compiled with a checkpointer so runs can be persisted and resumed by
``thread_id``.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph

from app.agents.prompts import get_system_prompt
from app.graph.state import StockAgentState
from app.observability.tracing import TraceCollector
from app.reporting.assembler import build_grounded_report
from app.schemas.report import StockResearchReport
from app.tools.base import ProviderRateLimitError, tool_call_signature


@dataclass
class AgentConfig:
    """Runtime limits for the ReAct loop."""

    max_iterations: int = 8
    llm_timeout_seconds: float = 20.0
    run_timeout_seconds: float = 60.0
    tool_timeout_seconds: float = 15.0
    tool_retries: int = 1
    retry_backoff_seconds: float = 0.5
    retry_max_backoff_seconds: float = 8.0
    retry_jitter: float = 0.1
    language: str = "en"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _precise_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _extract_token_usage(message: BaseMessage) -> dict[str, Any] | None:
    usage = getattr(message, "usage_metadata", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    return getattr(usage, "model_dump", lambda: None)()


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _infer_ticker(state: StockAgentState) -> str:
    if state.get("ticker"):
        return state["ticker"].upper()
    for call in reversed(state.get("tool_calls", [])):
        args = call.get("args") or {}
        ticker = args.get("ticker")
        if ticker:
            return str(ticker).upper()
    return "UNKNOWN"


def _build_agent_messages(state: StockAgentState, language: str) -> list[BaseMessage]:
    return [SystemMessage(content=get_system_prompt(language)), *list(state.get("messages", []))]


def _backoff_delay(attempt: int, config: AgentConfig) -> float:
    """Exponential backoff with jitter for the ``attempt``-th retry."""
    base = config.retry_backoff_seconds * (2 ** attempt)
    capped = min(base, config.retry_max_backoff_seconds)
    return capped + random.uniform(0.0, config.retry_jitter * capped)


def _remaining_run_seconds(state: StockAgentState) -> float | None:
    """Return the remaining run budget, or ``None`` when no deadline is set."""
    deadline = state.get("metadata", {}).get("run_deadline_monotonic")
    if deadline is None:
        return None
    return max(0.0, float(deadline) - time.monotonic())


def _bounded_timeout(configured_timeout: float, state: StockAgentState) -> float:
    """Bound an operation timeout by the remaining run deadline."""
    remaining = _remaining_run_seconds(state)
    if remaining is None:
        return configured_timeout
    if remaining <= 0:
        raise TimeoutError("run_timeout: total Agent run deadline reached")
    return min(configured_timeout, remaining)


def _timeout_error(kind: str) -> str:
    return f"{kind}: execution stopped before collecting further evidence"


def _make_agent_node(
    llm: BaseChatModel,
    tools: list[BaseTool],
    config: AgentConfig,
    tracer: TraceCollector,
):
    # Real providers support native tool binding; fake/test models do not, so we
    # fall back to the bare model (the fake returns pre-scripted tool calls).
    try:
        llm_with_tools = llm.bind_tools(tools)
    except NotImplementedError:
        llm_with_tools = llm

    async def agent_node(state: StockAgentState) -> dict[str, Any]:
        iteration = state.get("iteration_count", 0) + 1
        updates: dict[str, Any] = {"iteration_count": iteration}

        if iteration > config.max_iterations:
            final_report = state.get("final_report") or (
                "Research stopped after reaching the maximum number of iterations. "
                "The answer below is based on the evidence collected so far."
            )
            tracer.record(
                event_type="agent_step",
                agent_step=iteration,
                final_status="max_iterations",
            )
            return {
                **updates,
                "messages": [AIMessage(content=final_report)],
                "final_report": final_report,
                "status": "max_iterations",
            }

        try:
            llm_timeout = _bounded_timeout(config.llm_timeout_seconds, state)
        except TimeoutError:
            error = _timeout_error("run_timeout")
            tracer.record(event_type="agent_step", agent_step=iteration, error=error, final_status="timeout")
            return {
                **updates,
                "messages": [AIMessage(content="Run deadline reached; returning verified evidence only.")],
                "errors": state.get("errors", []) + [error],
                "status": "timeout",
            }

        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                llm_with_tools.ainvoke(_build_agent_messages(state, config.language)),
                timeout=llm_timeout,
            )
            latency_ms = (time.perf_counter() - start) * 1000.0
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error = _timeout_error(
                "run_timeout" if (_remaining_run_seconds(state) or 0.0) <= 0 else "llm_timeout"
            )
            latency_ms = (time.perf_counter() - start) * 1000.0
            tracer.record(
                event_type="agent_step", agent_step=iteration, latency_ms=latency_ms,
                error=error, final_status="timeout",
            )
            return {
                **updates,
                "messages": [AIMessage(content="LLM timed out; returning verified evidence only.")],
                "errors": state.get("errors", []) + [error],
                "status": "timeout",
            }
        except Exception as exc:  # LLM outage -> degrade gracefully
            error = f"{type(exc).__name__}: {exc}"
            latency_ms = (time.perf_counter() - start) * 1000.0
            tracer.record(
                event_type="agent_step",
                agent_step=iteration,
                latency_ms=latency_ms,
                error=error,
                final_status="error",
            )
            final_report = (
                f"The reasoning model failed at step {iteration} ({error}). "
                "Returning the evidence collected so far."
            )
            return {
                **updates,
                "messages": [AIMessage(content=final_report)],
                "final_report": final_report,
                "errors": state.get("errors", []) + [error],
                "status": "error",
            }

        tracer.record(
            event_type="agent_step",
            agent_step=iteration,
            latency_ms=latency_ms,
            token_usage=_extract_token_usage(response),
        )

        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            return {**updates, "messages": [response], "status": "running"}

        content = _stringify(getattr(response, "content", ""))
        return {
            **updates,
            "messages": [response],
            "final_report": content,
            "status": "finished",
        }

    return agent_node


async def _ainvoke_tool_with_timeout(tool: BaseTool, args: dict[str, Any], timeout_seconds: float) -> Any:
    return await asyncio.wait_for(tool.ainvoke(args), timeout=timeout_seconds)


def _make_tools_node(
    tools: list[BaseTool],
    config: AgentConfig,
    tracer: TraceCollector,
):
    tool_map = {tool.name: tool for tool in tools}

    async def _run_tool_with_retry(
        tool: BaseTool, args: dict[str, Any], agent_step: int, state: StockAgentState
    ) -> tuple[str | None, str | None]:
        last_error: str | None = None
        for attempt in range(config.tool_retries + 1):
            retry_allowed = True
            try:
                tool_timeout = _bounded_timeout(config.tool_timeout_seconds, state)
            except TimeoutError:
                return None, _timeout_error("run_timeout")
            start = time.perf_counter()
            try:
                raw = await _ainvoke_tool_with_timeout(tool, args, tool_timeout)
                output = _stringify(raw)
                tracer.record(
                    event_type="tool_call",
                    agent_step=agent_step,
                    tool_name=tool.name,
                    tool_input=args,
                    tool_output=output,
                    latency_ms=(time.perf_counter() - start) * 1000.0,
                )
                return output, None
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if (_remaining_run_seconds(state) or 0.0) <= 0:
                    return None, _timeout_error("run_timeout")
                last_error = _timeout_error("tool_timeout")
            except ProviderRateLimitError as exc:
                last_error = f"{type(exc).__name__}: {tool.name}: {exc}"
                retry_allowed = False
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            tracer.record(
                event_type="tool_call",
                agent_step=agent_step,
                tool_name=tool.name,
                tool_input=args,
                latency_ms=(time.perf_counter() - start) * 1000.0,
                error=last_error,
            )
            if not retry_allowed:
                break
            if attempt < config.tool_retries:
                remaining = _remaining_run_seconds(state)
                delay = _backoff_delay(attempt, config)
                if remaining is not None and remaining <= delay:
                    return None, _timeout_error("run_timeout")
                await asyncio.sleep(delay)
        tracer.record(
            event_type="tool_result",
            agent_step=agent_step,
            tool_name=tool.name,
            error=last_error,
        )
        return None, last_error

    async def tools_node(state: StockAgentState) -> dict[str, Any]:
        last = state.get("messages", [])[-1] if state.get("messages") else None
        calls = getattr(last, "tool_calls", None) or []
        agent_step = state.get("iteration_count", 0)

        tool_messages: list[ToolMessage] = []
        observations: list[dict[str, Any]] = []
        errors: list[str] = []
        new_signatures: list[str] = []
        new_tool_calls: list[dict[str, Any]] = []
        existing_signatures = set(state.get("tool_call_signatures", []))
        state_ticker = str(state.get("ticker") or "").strip().upper()

        # Jobs that actually need to be executed (concurrently).
        jobs: list[tuple[str, str, BaseTool, dict[str, Any]]] = []

        for call in calls:
            name = call.get("name", "")
            args = call.get("args") or {}
            call_id = call.get("id") or f"call_{agent_step}_{len(tool_messages)}"
            signature = tool_call_signature(name, args)
            new_tool_calls.append({"name": name, "args": args, "id": call_id, "step": agent_step})

            if signature in existing_signatures:
                skipped = json.dumps({
                    "duplicate": True,
                    "message": "Identical tool call already requested in this run; skipped.",
                })
                tool_messages.append(
                    ToolMessage(
                        content=skipped,
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                tracer.record(
                    event_type="tool_skipped", agent_step=agent_step,
                    tool_name=name, tool_input=args, tool_output=skipped,
                )
                continue

            # Include requests scheduled in this batch before considering the
            # next call. This set is local to this run/node, never a global cache.
            existing_signatures.add(signature)
            new_signatures.append(signature)
            requested_ticker = args.get("ticker")
            if state_ticker and requested_ticker is not None and str(requested_ticker).strip().upper() != state_ticker:
                error = (
                    f"Ticker mismatch for {name!r}: requested {requested_ticker!r}, "
                    f"but this single-stock run is for {state_ticker!r}."
                )
                errors.append(error)
                tool_messages.append(
                    ToolMessage(content=json.dumps({"error": error}), tool_call_id=call_id, name=name)
                )
                continue
            tool = tool_map.get(name)
            if tool is None:
                error = f"Unknown tool requested: {name!r}"
                errors.append(error)
                tool_messages.append(
                    ToolMessage(content=json.dumps({"error": error}), tool_call_id=call_id, name=name)
                )
                continue

            jobs.append((call_id, name, tool, args))

        results = await asyncio.gather(
            *(_run_tool_with_retry(tool, args, agent_step, state) for _, _, tool, args in jobs)
        )

        for (call_id, name, _, args), (output, error) in zip(jobs, results):
            if error:
                errors.append(error)
                tool_messages.append(
                    ToolMessage(
                        content=json.dumps({"tool": name, "error": error}),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
            else:
                observations.append(
                    {
                        "tool": name,
                        "args": args,
                        "result": output,
                        "timestamp": _utc_now_iso(),
                    }
                )
                tool_messages.append(ToolMessage(content=output, tool_call_id=call_id, name=name))

        timed_out = any(error and error.startswith("run_timeout") for error in errors)
        return {
            "messages": tool_messages,
            "tool_calls": state.get("tool_calls", []) + new_tool_calls,
            "observations": state.get("observations", []) + observations,
            "errors": state.get("errors", []) + errors,
            "tool_call_signatures": state.get("tool_call_signatures", []) + new_signatures,
            **({"status": "timeout"} if timed_out else {}),
        }

    return tools_node


def _no_data_report(ticker: str, language: str, errors: list[str]) -> StockResearchReport:
    """Honest report when no tool observation was collected (e.g. all tools failed)."""
    if language == "zh":
        summary = "无法生成研究报告：所有数据工具均失败，未收集到任何可验证的数据。"
        detail = "数据源不可用（例如被限流、网络不可达或无权限）。请检查数据源后重试。"
    else:
        summary = "Unable to produce a research report: every data tool failed and no evidence was collected."
        detail = "The data source is unavailable (rate-limited, unreachable, or unauthenticated). Please check it and retry."

    reasons = errors[:5] if errors else [detail]
    return StockResearchReport(
        ticker=ticker,
        summary=summary,
        market_analysis=detail,
        financial_analysis=detail,
        technical_analysis=detail,
        news_analysis=detail,
        valuation_analysis=detail,
        risk_analysis=detail,
        bull_case=[],
        bear_case=[],
        key_metrics={},
        uncertainty=reasons,
        conclusion=summary,
    )


def _grounded_or_no_data_report(state: StockAgentState, ticker: str) -> StockResearchReport:
    """Return only a report assembled from valid observations, or an honest no-data report."""
    try:
        grounded = build_grounded_report(state, state.get("language", "en"))
    except Exception as exc:
        errors = state.get("errors", []) + [f"Grounded report assembly failed: {type(exc).__name__}: {exc}"]
        return _no_data_report(ticker, state.get("language", "en"), errors)
    if grounded is not None:
        return grounded
    return _no_data_report(ticker, state.get("language", "en"), state.get("errors", []))


def _make_finalize_node(tracer: TraceCollector):
    async def finalize_node(state: StockAgentState) -> dict[str, Any]:
        start = time.perf_counter()
        report = _grounded_or_no_data_report(state, _infer_ticker(state))
        report = report.model_copy(update={"generated_at": _precise_now_iso()})
        # Producing a safe report does not turn a failed/limited run into success.
        status = state.get("status", "finished")
        updates: dict[str, Any] = {"final_output": report, "status": status}
        error = None
        # Safe local assembly still runs after a timeout. Never turn an expired
        # run into success merely because no additional LLM call is needed.
        remaining = _remaining_run_seconds(state)
        if status != "timeout" and remaining is not None and remaining <= 0:
            error = _timeout_error("run_timeout")
            updates["errors"] = state.get("errors", []) + [error]
            updates["status"] = "timeout"
        tracer.record(
            event_type="finalize",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            final_status=updates["status"],
            error=error,
        )
        return updates

    return finalize_node


def _route_after_agent(state: StockAgentState) -> str:
    if state.get("status") in ("finished", "max_iterations", "error", "timeout"):
        return "finalize"
    return "tools"


def _route_after_tools(state: StockAgentState) -> str:
    """Do not start another LLM step after tools exhaust the run deadline."""
    return "finalize" if state.get("status") == "timeout" else "agent"


def build_agent_graph(
    llm: BaseChatModel,
    tools: list[BaseTool],
    config: AgentConfig | None = None,
    tracer: TraceCollector | None = None,
    checkpointer: Any | None = None,
):
    """Compile the Phase 3 async ReAct graph."""
    config = config or AgentConfig()
    tracer = tracer or TraceCollector()

    graph = StateGraph(StockAgentState)
    graph.add_node("agent", _make_agent_node(llm, tools, config, tracer))
    graph.add_node("tools", _make_tools_node(tools, config, tracer))
    graph.add_node("finalize", _make_finalize_node(tracer))

    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        _route_after_agent,
        {"tools": "tools", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "tools", _route_after_tools, {"agent": "agent", "finalize": "finalize"}
    )
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
