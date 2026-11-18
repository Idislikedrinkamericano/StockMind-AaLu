import asyncio
import json
import socket

import pytest
from pydantic import ValidationError

from app.evaluation import Evaluator, load_dataset
from app.evaluation.evaluator import evidence_checks
from app.evaluation.fixtures import FixtureProvider
from app.evaluation.reliability import (
    ReliabilityEvaluator, ReliabilityReport, aggregate_reliability, detect_safety_violations,
)
from app.evaluation.reliability_dataset import ReliabilityDataset, load_reliability_dataset
from app.graph.state import create_initial_state
from app.graph.workflow import _grounded_or_no_data_report
from app.observability.tracing import TraceCollector
from examples import evaluate as cli


def _block_external(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reliability evaluation must not access external models, providers, or networks")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.agents.react_agent.get_chat_model", forbidden)
    monkeypatch.setattr("app.agents.react_agent.get_data_provider", forbidden)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    _block_external(monkeypatch)


@pytest.fixture(scope="module")
def result():
    with pytest.MonkeyPatch.context() as patch:
        _block_external(patch)
        return asyncio.run(ReliabilityEvaluator().evaluate(load_reliability_dataset()))


def _subset(*ids):
    raw = load_reliability_dataset().model_dump()
    raw["cases"] = [case for case in raw["cases"] if case["case_id"] in ids]
    return ReliabilityDataset.model_validate(raw)


def _case(result, case_id):
    return next(run for run in result.runs if run.case_id == case_id)


@pytest.mark.parametrize("case_id,verdict", [
    ("provider-retry", "pass"), ("tool-timeout-retry", "pass"),
    ("invalid-arguments", "pass"), ("wrong-ticker-request", "pass"),
    ("duplicate-same-batch", "fail"), ("unknown-tool-request", "pass"),
    ("llm-exception-partial", "fail"), ("llm-timeout-partial", "pass"),
    ("invalid-observation-partial", "pass"), ("missing-data", "pass"),
    ("checkpoint-paused-resume", "pass"), ("checkpoint-terminal-retry", "unsupported"),
])
def test_actual_fault_results_remain_visible(result, case_id, verdict):
    run = _case(result, case_id)
    assessment = run.reliability
    assert assessment.injection_observed and assessment.injection_events
    assert assessment.verdict == verdict
    assert run.grounded
    assert run.execution.latency_ms >= 0
    assert run.execution.tool_call_count == len(run.actual_calls)
    assert assessment.terminal_status == run.execution.final_status
    assert bool(assessment.failure_reasons) == (verdict != "pass")


@pytest.mark.parametrize("case_id", ["provider-retry", "tool-timeout-retry"])
def test_retry_requires_a_later_successful_attempt(result, case_id):
    run = _case(result, case_id)
    assert run.actual_calls[0]["error"] and run.actual_calls[1]["error"] is None
    assert run.execution.tool_call_count == 2 and run.execution.observation_count == 1
    # Recovered transient errors live in the trace, not terminal State.errors.
    assert run.execution.error_count == 0 and run.reliability.observed_behavior["errors"]
    assert run.reliability.recovery_attempt and run.reliability.recovery_outcome == "recovered"
    assert run.report.key_metrics["price"] == 100
    if case_id == "tool-timeout-retry":
        assert [event["phase"] for event in run.reliability.injection_events] == ["waiting", "cancelled"]
        assert "tool_timeout" in run.actual_calls[0]["error"]


def test_invalid_arguments_wrong_ticker_and_unknown_tool_are_rejected(result):
    invalid = _case(result, "invalid-arguments")
    assert "ValidationError" in invalid.actual_calls[0]["error"]
    assert invalid.report.key_metrics["rsi"] is None
    for case_id in ("wrong-ticker-request", "unknown-tool-request"):
        run = _case(result, case_id)
        assert run.execution.requested_tool_count == 2 and run.execution.tool_call_count == 1
        assert run.actual_calls[0]["args"]["ticker"] == "NVDA"
        assert run.reliability.recovery_outcome == "recovered"
    wrong = _case(result, "wrong-ticker-request")
    emitted = wrong.reliability.injection_events[0]["calls"][0]
    assert emitted["args"]["ticker"] == "AMD" and "Ticker mismatch" in wrong.errors[0]
    assert _case(result, "unknown-tool-request").requested_tools[0] == "nonexistent_reliability_tool"


def test_same_batch_duplicate_is_not_hidden_by_set_scores(result):
    run = _case(result, "duplicate-same-batch")
    assert run.tool_selection.f1 == 1 and run.task_completed
    assert run.execution.tool_call_count == run.execution.requested_tool_count == 2
    assert run.reliability.expected_behavior.tool_attempts == 1
    assert run.reliability.verdict == "fail" and run.reliability.failure_reasons == ["tool_attempts"]
    assert not run.reliability.safety_violations


def test_llm_failure_and_timeout_have_distinct_terminal_assessments(result):
    error = _case(result, "llm-exception-partial")
    timeout = _case(result, "llm-timeout-partial")
    assert error.execution.final_status == "finished" and error.errors
    assert error.reliability.verdict == "fail"
    violation = error.reliability.safety_violations[0]
    assert violation.category == "failure_reported_success" and "injected llm exception" in violation.evidence
    assert timeout.execution.final_status == "timeout" and not timeout.task_completed
    assert timeout.reliability.outcome == "controlled_failure" and timeout.reliability.verdict == "pass"
    assert [event["phase"] for event in timeout.reliability.injection_events] == ["waiting", "cancelled"]
    for run in (error, timeout):
        assert run.actual_tools == ["get_stock_price"]
        assert run.execution.tool_call_count == 1 and run.report.key_metrics["price"] == 100
        assert run.report.news_analysis.startswith("Unavailable")
        assert run.reliability.observed_behavior["recorded_agent_steps"] == 2


def test_partial_and_missing_evidence_never_becomes_financial_claims(result):
    partial = _case(result, "invalid-observation-partial")
    assert "999999" in partial.observations[0]["result"]
    assert "999999" not in partial.report.model_dump_json()
    assert partial.report.key_metrics["price"] is None and partial.report.key_metrics["revenue"] == 1000
    assert partial.reliability.observed_behavior["rejected_observation_indices"] == [0]
    assert partial.reliability.recovery_outcome == "recovered"
    missing = _case(result, "missing-data")
    assert missing.observations[0]["result"] == "{}"
    assert missing.report.key_metrics == {} and "unavailable" in missing.report.valuation_analysis
    assert missing.report.bull_case == [] and missing.report.bear_case == []
    assert missing.reliability.outcome == "safe_degradation"


def test_native_checkpoint_resume_and_thread_isolation(result):
    run = _case(result, "checkpoint-paused-resume")
    checkpoint = run.reliability.observed_behavior["checkpoint"]
    assert all(checkpoint["checks"].values())
    assert checkpoint["pending_before"] == ["agent"] and checkpoint["pending_after"] == []
    assert checkpoint["restored_ticker"] == "NVDA" and checkpoint["other_ticker"] == "AMD"
    assert checkpoint["resume_model_calls"] == 1
    assert json.loads(checkpoint["restored_observations"][0]["result"])["ticker"] == "NVDA"
    assert json.loads(checkpoint["other_observations"][0]["result"])["ticker"] == "AMD"
    assert run.reliability.recovery_outcome == "recovered"
    assert run.execution.tool_call_count == 1  # Other thread is not counted in this run.


def test_terminal_checkpoint_replay_is_observed_unsupported_not_success(result):
    run = _case(result, "checkpoint-terminal-retry")
    checkpoint = run.reliability.observed_behavior["checkpoint"]
    assert all(checkpoint["checks"].values()) and checkpoint["pending_before"] == []
    assert checkpoint["resume_model_calls"] == 0 and checkpoint["replay_status"] == "timeout"
    assert run.reliability.recovery_attempt and run.reliability.recovery_outcome == "unsupported"
    assert not run.reliability.safe_termination_met and not run.task_completed


def test_checkpoint_read_failure_cannot_fake_recovery(monkeypatch):
    async def failed_read(agent, thread_id):
        raise OSError("injected checkpoint read failure")

    monkeypatch.setattr("app.evaluation.checkpoint_evaluation._restored_snapshot", failed_read)
    report = asyncio.run(ReliabilityEvaluator().evaluate(_subset("checkpoint-paused-resume")))
    run = report.runs[0]
    assert run.execution.final_status == "error" and run.report is None
    assert run.reliability.verdict == "fail" and run.reliability.recovery_outcome == "not_recovered"
    assert any("injected checkpoint read failure" in reason for reason in run.reliability.failure_reasons)
    assert report.reliability_aggregate["recovery"]["numerator"] == 0


def test_denominators_include_failures_and_unsupported(result):
    totals = result.reliability_aggregate
    assert totals["verdict_counts"] == {"pass": 9, "fail": 2, "unsupported": 1}
    assert totals["safe_termination"] == {"numerator": 8, "denominator": 10, "rate": 0.8,
                                           "unsupported_in_denominator": 0}
    assert totals["recovery"] == {"numerator": 7, "denominator": 8, "rate": 0.875,
                                  "unsupported_in_denominator": 1}
    assert totals["safety_violation_count"] == totals["cases_with_safety_violations"] == 1
    empty = aggregate_reliability([])
    assert empty["recovery"]["rate"] is None and empty["safe_termination"]["rate"] is None
    unsupported_only = aggregate_reliability([_case(result, "checkpoint-terminal-retry")])
    assert unsupported_only["recovery"]["rate"] == 0 and unsupported_only["recovery"]["denominator"] == 1
    assert unsupported_only["safe_termination"]["denominator"] == 0


def test_catching_exception_alone_is_not_recovery(monkeypatch):
    def fail_price(self, ticker):
        raise RuntimeError("fixture still unavailable")

    monkeypatch.setattr(FixtureProvider, "get_stock_price", fail_price)
    run = asyncio.run(ReliabilityEvaluator().evaluate(_subset("provider-retry"))).runs[0]
    assert run.reliability.recovery_attempt and run.reliability.recovery_outcome == "not_recovered"
    assert run.reliability.verdict == "fail" and run.report.key_metrics == {}


def _price_state():
    state = create_initial_state("Synthetic price", ticker="NVDA")
    state["status"] = "finished"
    state["observations"] = [{"tool": "get_stock_price", "args": {"ticker": "NVDA"},
                              "result": FixtureProvider().get_stock_price("NVDA").model_dump_json()}]
    state["final_output"] = _grounded_or_no_data_report(state, "NVDA")
    return state


@pytest.mark.parametrize("tamper,category", [
    ("price", "unverified_financial_value"), ("nested_number", "unverified_financial_value"),
    ("ticker", "wrong_ticker"), ("missing_fact", "missing_data_as_fact"),
    ("no_data_fact", "missing_data_as_fact"), ("failure_status", "failure_reported_success"),
    ("foreign_evidence", "unverified_financial_value"),
])
def test_safety_violations_have_checkable_evidence(tamper, category):
    state = _price_state()
    report = state["final_output"]
    tracer = TraceCollector()
    tracer.start_run("Synthetic check")
    assert detect_safety_violations(state, "NVDA", []) == []
    if tamper == "price":
        report.key_metrics["price"] = 999999
    elif tamper == "nested_number":
        report.key_metrics["valuation_metadata"] = {"invented": {"value": 999999}}
    elif tamper == "ticker":
        report.ticker = "AMD"
    elif tamper == "missing_fact":
        report.financial_analysis = "Financial growth is confirmed."
    elif tamper == "no_data_fact":
        state["observations"] = []
        state["final_output"] = _grounded_or_no_data_report(state, "NVDA")
        state["final_output"].market_analysis = "Price is stable."
    elif tamper == "foreign_evidence":
        payload = json.loads(state["observations"][0]["result"])
        payload["ticker"] = "AMD"
        state["observations"][0]["result"] = json.dumps(payload)
    else:
        tracer.record(event_type="agent_step", error="injected reasoning exception")
    violations = detect_safety_violations(state, "NVDA", tracer.events)
    assert any(v.category == category for v in violations)
    assert all(v.field and v.expected != v.observed and v.evidence for v in violations)
    if tamper != "failure_status":
        assert not all(evidence_checks(state, "NVDA").values())


@pytest.mark.parametrize("invalid", ["version", "kind", "duplicate", "step", "tool", "args_not_invalid",
                                    "same_ticker", "invalid_script", "terminal_step"])
def test_reliability_dataset_rejects_invalid_labels_and_scripts(invalid):
    raw = load_reliability_dataset().model_dump()
    if invalid == "version":
        raw["version"] = "unversioned"
    elif invalid == "kind":
        raw["cases"][0]["failure"]["kind"] = "arbitrary_code"
    elif invalid == "duplicate":
        raw["cases"].append(raw["cases"][0])
    elif invalid == "step":
        raw["cases"][0]["failure"]["step"] = 7
    elif invalid == "tool":
        raw["cases"][0]["failure"]["tool"] = "invented"
    elif invalid == "args_not_invalid":
        raw["cases"][2]["failure"]["replacement_args"] = {"window": 14}
    elif invalid == "same_ticker":
        raw["cases"][3]["failure"]["replacement_args"] = {"ticker": "NVDA"}
    elif invalid == "invalid_script":
        raw["cases"][2]["script"][0]["args"]["window"] = "bad"
    else:
        raw["cases"][-1]["failure"]["step"] = 1
    with pytest.raises(ValidationError):
        ReliabilityDataset.model_validate(raw)


def test_repeatability_and_json_with_hostile_environment(monkeypatch, tmp_path, result):
    monkeypatch.setenv("STOCKMIND_DATA_PROVIDER", "akshare")
    monkeypatch.setenv("STOCKMIND_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("STOCKMIND_RUN_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("STOCKMIND_TRACE_DIR", str(tmp_path / "unrequested"))
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "offline-placeholder")
    evaluator = ReliabilityEvaluator()
    for _ in range(2):
        repeated = asyncio.run(evaluator.evaluate(load_reliability_dataset()))
        assert repeated.dataset_sha256 == result.dataset_sha256
        assert repeated.fixture_sha256 == result.fixture_sha256
        assert repeated.reliability_aggregate == result.reliability_aggregate
        for a, b in zip(repeated.runs, result.runs):
            assert a.reliability.checks == b.reliability.checks
            assert a.actual_calls == b.actual_calls and a.errors == b.errors
            assert a.report.model_dump(exclude={"generated_at"}) == b.report.model_dump(exclude={"generated_at"})
        encoded = repeated.model_dump_json()
        assert ReliabilityReport.model_validate_json(encoded) == repeated
        json.dumps(repeated.model_dump(mode="json"), allow_nan=False)
    assert not (tmp_path / "unrequested").exists()


def test_case_watchdog_and_external_cancellation(monkeypatch):
    async def never_finishes(self, agent, case):
        await asyncio.Event().wait()

    monkeypatch.setattr(Evaluator, "_invoke_case", never_finishes)
    monkeypatch.setattr("app.evaluation.reliability.CASE_TIMEOUT", 0.01)
    report = asyncio.run(ReliabilityEvaluator().evaluate(_subset("provider-retry", "missing-data")))
    assert len(report.runs) == 2
    assert all(run.reliability.verdict == "fail" and run.execution.final_status == "error" for run in report.runs)
    assert all("TimeoutError" in run.errors[0] for run in report.runs)

    async def cancelled(self, agent, case):
        raise asyncio.CancelledError

    monkeypatch.setattr(Evaluator, "_invoke_case", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ReliabilityEvaluator().evaluate(_subset("provider-retry")))


def test_cli_reliability_json_and_original_baseline(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Evaluation must not load the user's .env")

    monkeypatch.setattr("dotenv.load_dotenv", forbidden)
    monkeypatch.setattr("sys.argv", ["evaluate.py", "--suite", "reliability", "--lang", "zh"])
    asyncio.run(cli.main())
    result = ReliabilityReport.model_validate_json(capsys.readouterr().out)
    assert result.language == "zh" and result.reliability_aggregate["verdict_counts"] == {"pass": 9, "fail": 2, "unsupported": 1}
    baseline = asyncio.run(Evaluator().evaluate(load_dataset()))
    assert len(baseline.runs) == 12 and all(run.task_completed and run.grounded for run in baseline.runs)
    assert baseline.aggregate["total_tool_calls"] == 25 and baseline.aggregate["total_errors"] == 3
