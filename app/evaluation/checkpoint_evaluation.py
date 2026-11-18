"""Exercise native LangGraph pause/resume with the existing in-memory saver."""

import time

from app.evaluation.dataset import ScriptCall
from app.evaluation.failure_injection import InjectionJournal, build_fault_agent
from app.graph.state import create_initial_state


async def _restored_snapshot(agent, thread_id):
    # Separate boundary permits evaluation tests to inject a failed saver read.
    return await agent.aget_state(thread_id)


async def run_checkpoint_case(agent, case, journal: InjectionJournal, language: str):
    thread_id = f"eval-{case.case_id}"
    config = {"configurable": {"thread_id": thread_id}}
    kind = case.failure.kind
    if kind == "checkpoint_terminal_replay":
        state = await agent.arun(case.question, ticker=case.ticker, thread_id=thread_id)
        before = await agent.aget_state(thread_id)
        restored = build_fault_agent(case, journal, language, checkpointer=agent.checkpointer,
                                     tracer=agent.tracer, resume=True)
        journal.checkpoint["recovery_attempted"] = True
        snapshot = await _restored_snapshot(restored, thread_id)
        event_count = len(agent.tracer.events)
        replay = await restored.graph.ainvoke(None, config=config)
        checks = {
            "no_pending_nodes": before.next == snapshot.next == (),
            "terminal_status_preserved": replay["status"] == state["status"] == "timeout",
            "report_preserved": replay["final_output"] == state["final_output"],
            "no_new_execution": restored.llm.calls == 0 and len(agent.tracer.events) == event_count,
        }
        journal.checkpoint.update(checks=checks, pending_before=list(before.next),
                                  resume_model_calls=restored.llm.calls, replay_status=replay["status"])
        if all(checks.values()):
            journal.checkpoint["unsupported_reason"] = (
                "Handled timeout reached END with no pending nodes; ainvoke(None) reads the terminal "
                "checkpoint but does not retry the failed research. A new arun is a new run, not recovery."
            )
        return replay

    # Public arun always constructs fresh state. For a real paused-node recovery,
    # invoke the unchanged compiled graph with its native interrupt_after option.
    run_id = agent.tracer.start_run(case.question)
    initial = create_initial_state(case.question, ticker=case.ticker, language=language,
                                   metadata={"run_id": run_id,
                                             "run_deadline_monotonic": time.monotonic() + agent.config.run_timeout_seconds})
    await agent.graph.ainvoke(initial, config=config, interrupt_after=["tools"])
    paused = await agent.aget_state(thread_id)
    journal.record(kind, "paused", pending=list(paused.next), ticker=paused.values.get("ticker"))

    other_ticker = "AMD" if case.ticker != "AMD" else "NVDA"
    other_case = case.model_copy(update={"ticker": other_ticker, "question": f"Fixture isolation: {other_ticker}",
                                         "script": [ScriptCall(name="get_stock_price", args={"ticker": other_ticker})]})
    other = build_fault_agent(other_case, InjectionJournal(), language, checkpointer=agent.checkpointer)
    other_thread = thread_id + "-other"
    other_state = await other.arun(other_case.question, ticker=other_ticker, thread_id=other_thread)

    restored = build_fault_agent(case, journal, language, checkpointer=agent.checkpointer,
                                 tracer=agent.tracer, resume=True)
    journal.checkpoint["recovery_attempted"] = True
    saved = await _restored_snapshot(restored, thread_id)
    result = await restored.graph.ainvoke(None, config=config)
    completed = await restored.aget_state(thread_id)
    other_saved = await other.aget_state(other_thread)
    checks = {
        "paused_after_tools": paused.next == ("agent",) and bool(paused.values.get("observations")),
        "new_agent_read_same_state": saved.values == paused.values,
        "same_thread_restored": result["ticker"] == case.ticker and completed.next == (),
        "observations_preserved": result["observations"] == paused.values["observations"],
        "other_thread_isolated": other_saved.values["ticker"] == other_ticker and other_saved.values == other_state,
        "no_cross_ticker_observations": all(obs["args"].get("ticker") == case.ticker for obs in result["observations"]),
        "different_ticker_report": other_state["final_output"].ticker == other_ticker,
        "resumed_execution": restored.llm.calls == 1 and result["final_output"] is not None,
    }
    journal.checkpoint.update(checks=checks, pending_before=list(paused.next), pending_after=list(completed.next),
                              restored_ticker=result["ticker"], other_ticker=other_saved.values["ticker"],
                              restored_observations=result["observations"],
                              other_observations=other_saved.values["observations"], resume_model_calls=restored.llm.calls)
    agent.tracer.record(event_type="run_end", final_status=result["status"], latency_ms=agent.tracer.total_latency_ms())
    return result
