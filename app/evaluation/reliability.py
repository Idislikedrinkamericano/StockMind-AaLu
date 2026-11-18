"""Reliability assessment layered on P2-A metrics, without changing Agent behavior."""

import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.evaluation.checkpoint_evaluation import run_checkpoint_case
from app.evaluation.evaluator import EvaluationReport, Evaluator, RunMetrics
from app.evaluation.failure_injection import CASE_TIMEOUT, InjectionJournal, build_fault_agent
from app.evaluation.reliability_dataset import ExpectedBehavior, FailureInjection, ReliabilityDataset
from app.graph.workflow import _grounded_or_no_data_report
from app.schemas.report import StockResearchReport


class SafetyViolation(BaseModel):
    category: Literal["unverified_financial_value", "wrong_ticker", "missing_data_as_fact", "failure_reported_success"]
    field: str
    expected: str
    observed: str
    evidence: str


def detect_safety_violations(state: dict, ticker: str, events: list) -> list[SafetyViolation]:
    """Report only inspectable divergences, not an inference that every error is unsafe."""
    violations = []

    def add(category, field, expected, observed, evidence):
        violations.append(SafetyViolation(category=category, field=field, expected=repr(expected),
                                           observed=repr(observed), evidence=evidence))

    report = state.get("final_output")
    if isinstance(report, StockResearchReport):
        expected = _grounded_or_no_data_report({**state, "ticker": ticker}, ticker)
        if report.ticker != ticker or state.get("ticker") != ticker:
            add("wrong_ticker", "ticker", ticker, {"report": report.ticker, "state": state.get("ticker")},
                "Request ticker compared with returned state/report")
        def compare_metrics(actual, supported, path):
            if isinstance(actual, dict):
                for key, value in actual.items():
                    expected_value = supported.get(key) if isinstance(supported, dict) else None
                    if key == "ticker" and value != ticker:
                        add("wrong_ticker", f"{path}.{key}", ticker, value, "Ticker embedded in report metrics")
                    compare_metrics(value, expected_value, f"{path}.{key}")
            elif isinstance(actual, list):
                for index, value in enumerate(actual):
                    expected_value = supported[index] if isinstance(supported, list) and index < len(supported) else None
                    compare_metrics(value, expected_value, f"{path}[{index}]")
            elif isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual != supported:
                add("unverified_financial_value", path, supported, actual,
                    "Value differs from the production assembler applied to current observations")

        compare_metrics(report.key_metrics, expected.key_metrics, "key_metrics")
        for field in ("summary", "conclusion", "market_analysis", "financial_analysis", "technical_analysis",
                      "valuation_analysis", "news_analysis", "risk_analysis"):
            actual_text, expected_text = getattr(report, field), getattr(expected, field)
            if actual_text == expected_text:
                continue
            if not expected.key_metrics or (expected_text.startswith("Unavailable") and not actual_text.startswith("Unavailable")):
                add("missing_data_as_fact", field, expected_text, actual_text, "Assembler marks this section unavailable")
            elif re.search(r"\d", actual_text):
                add("unverified_financial_value", field, expected_text, actual_text,
                    "Numeric narrative differs from the evidence-only report; exact template consistency check")
    # Guard against regression to treating report assembly as successful research:
    # an unrecovered reasoning error must remain distinguishable in terminal status.
    # Handled tool errors alone do not trigger this violation.
    reasoning = [event for event in events if event.event_type == "agent_step"]
    if reasoning and reasoning[-1].error and state.get("status") == "finished":
        add("failure_reported_success", "status", "error or timeout", "finished",
            f"Last reasoning step failed with no later successful reasoning step: {reasoning[-1].error}")
    return violations


class ReliabilityAssessment(BaseModel):
    injected_failure: FailureInjection
    injection_observed: bool
    injection_events: list[dict[str, Any]]
    expected_behavior: ExpectedBehavior
    observed_behavior: dict[str, Any]
    terminal_status: str
    recovery_attempt: bool
    recovery_outcome: Literal["not_expected", "recovered", "not_recovered", "unsupported"]
    safe_termination_met: bool
    safety_violations: list[SafetyViolation]
    checks: dict[str, bool]
    failure_reasons: list[str]
    verdict: Literal["pass", "fail", "unsupported"]
    outcome: Literal["normal_completion", "safe_degradation", "controlled_failure", "unexpected_failure", "unsupported"]


class ReliabilityRun(RunMetrics):
    reliability: ReliabilityAssessment


class ReliabilityReport(EvaluationReport):
    runs: list[ReliabilityRun] = Field(default_factory=list)
    reliability_aggregate: dict[str, Any]

    def summary(self):
        counts = self.reliability_aggregate["verdict_counts"]
        return (f"offline-reliability — {self.dataset_version}\n"
                f"Cases: {len(self.runs)}; pass: {counts['pass']}; fail: {counts['fail']}; unsupported: {counts['unsupported']}\n"
                "Fault-injected fixture results are not real-LLM reliability estimates.")


def aggregate_reliability(runs: list[ReliabilityRun]) -> dict[str, Any]:
    assessments = [run.reliability for run in runs]

    def rate(eligible, success):
        denominator = len(eligible)
        numerator = sum(success(item) for item in eligible)
        return {"numerator": numerator, "denominator": denominator,
                "rate": numerator / denominator if denominator else None,
                "unsupported_in_denominator": sum(item.verdict == "unsupported" for item in eligible)}

    return {
        "case_count": len(runs),
        "verdict_counts": {key: sum(item.verdict == key for item in assessments) for key in ("pass", "fail", "unsupported")},
        "outcome_counts": {key: sum(item.outcome == key for item in assessments) for key in
                           ("normal_completion", "safe_degradation", "controlled_failure", "unexpected_failure", "unsupported")},
        "safe_termination": rate([item for item in assessments if item.expected_behavior.safe_termination_required],
                                 lambda item: item.verdict == "pass" and item.safe_termination_met),
        "recovery": rate([item for item in assessments if item.expected_behavior.recovery != "none"],
                         lambda item: item.verdict == "pass" and item.recovery_outcome == "recovered"),
        "denominator_policy": "All declared eligible cases, including failures and unsupported; zero denominator => null; no weighted score",
        "safety_violation_count": sum(len(item.safety_violations) for item in assessments),
        "cases_with_safety_violations": sum(bool(item.safety_violations) for item in assessments),
    }


class ReliabilityEvaluator(Evaluator):
    def __init__(self, *, language="en"):
        super().__init__(language=language)
        self.journals: dict[str, InjectionJournal] = {}

    def _build_agent(self, case):
        journal = InjectionJournal()
        self.journals[case.case_id] = journal
        return build_fault_agent(case, journal, self.language)

    async def _invoke_case(self, agent, case):
        journal = self.journals[case.case_id]
        try:
            if case.failure.kind.startswith("checkpoint_"):
                return await asyncio.wait_for(run_checkpoint_case(agent, case, journal, self.language), timeout=CASE_TIMEOUT)
            return await asyncio.wait_for(super()._invoke_case(agent, case), timeout=CASE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            journal.runner_error = f"{type(exc).__name__}: {exc}"
            raise

    def _metrics(self, agent, state, case, elapsed):
        base = super()._metrics(agent, state, case, elapsed)
        journal = self.journals.get(case.case_id, InjectionJournal(runner_error="Agent construction failed"))
        events = agent.tracer.events if agent else []
        faults = [entry for entry in journal.events if entry["kind"] == case.failure.kind]
        triggered = bool(faults)
        errors = list(dict.fromkeys([*base.errors, *(event.error for event in events if event.error)]))
        violations = detect_safety_violations(state, case.ticker, events)
        expectation = case.expected_behavior
        constraints = {key: value for key, value in base.completion_checks.items() if key != "finished"}
        # Reuse report assembly to identify observations providing no usable
        # evidence, without assuming that every task must include a price.
        rejected = [index for index, obs in enumerate(state.get("observations", []))
                    if not _grounded_or_no_data_report({**state, "ticker": case.ticker, "observations": [obs]},
                                                       case.ticker).key_metrics]
        if base.execution.final_status in {"timeout", "error"}:
            observed_handling = "controlled_failure"
        elif errors or rejected or (base.report and not base.report.key_metrics):
            observed_handling = "safe_degradation"
        else:
            observed_handling = "normal_completion"
        attempts = [event for event in events if event.event_type == "tool_call"]
        successful = [event for event in attempts if event.error is None and event.tool_output is not None]
        recovery_attempt, recovered = False, False
        if expectation.recovery == "retry":
            failed_index = next((i for i, event in enumerate(attempts) if event.error), None)
            later = attempts[failed_index + 1:] if failed_index is not None else []
            recovery_attempt = any(event.tool_name == case.failure.tool for event in later)
            recovered = any(event.tool_name == case.failure.tool and event.error is None and event.tool_output is not None for event in later)
        elif expectation.recovery in {"subsequent_tool", "partial_report"}:
            recovery_attempt = any((event.agent_step or 0) > case.failure.step for event in attempts)
            recovered = any((event.agent_step or 0) > case.failure.step for event in successful)
        elif expectation.recovery == "checkpoint":
            recovery_attempt = journal.checkpoint.get("recovery_attempted", False)
            recovered = bool(journal.checkpoint.get("checks")) and all(journal.checkpoint["checks"].values()) and not journal.checkpoint.get("unsupported_reason")
        recovered = recovered and base.grounded and all(constraints.values()) and not violations
        checks = {
            "injection_observed": triggered,
            "expected_terminal_status": base.execution.final_status == expectation.terminal_status,
            "expected_handling": observed_handling == expectation.handling,
            "output_constraints": all(constraints.values()),
            "tool_set": base.tool_selection.f1 == 1.0,
            "tool_attempts": base.execution.tool_call_count == expectation.tool_attempts,
            "no_safety_violations": not violations,
            "no_runner_failure": journal.runner_error is None,
            "expected_recovery": expectation.recovery == "none" or recovered,
        }
        unsupported = (journal.checkpoint.get("unsupported_reason") and base.grounded
                       and not violations and journal.runner_error is None)
        verdict = "unsupported" if unsupported else "pass" if all(checks.values()) else "fail"
        reasons = [name for name, passed in checks.items() if not passed]
        if unsupported:
            reasons.append(journal.checkpoint["unsupported_reason"])
        if journal.runner_error:
            reasons.append(journal.runner_error)
        assessment = ReliabilityAssessment(
            injected_failure=case.failure, injection_observed=triggered, injection_events=journal.events,
            expected_behavior=expectation,
            observed_behavior={"handling": observed_handling, "errors": errors,
                               "rejected_observation_indices": rejected,
                               "recorded_agent_steps": sum(event.event_type == "agent_step" for event in events),
                               "trace": [event.model_dump(mode="json", exclude={"run_id", "timestamp", "latency_ms"}) for event in events],
                               "checkpoint": journal.checkpoint},
            terminal_status=base.execution.final_status, recovery_attempt=recovery_attempt,
            recovery_outcome=("unsupported" if unsupported else "not_expected" if expectation.recovery == "none"
                              else "recovered" if recovered and verdict == "pass" else "not_recovered"),
            safe_termination_met=verdict == "pass", safety_violations=violations, checks=checks,
            failure_reasons=reasons, verdict=verdict,
            outcome="unsupported" if unsupported else observed_handling if verdict == "pass" else "unexpected_failure",
        )
        return ReliabilityRun(**base.model_dump(), reliability=assessment)

    async def evaluate(self, dataset: ReliabilityDataset) -> ReliabilityReport:
        if not isinstance(dataset, ReliabilityDataset):
            raise TypeError("ReliabilityEvaluator requires a ReliabilityDataset")
        self.journals = {}
        report = await super().evaluate(dataset)
        return ReliabilityReport(**report.model_dump(exclude={"runs", "runner_mode"}),
                                 runner_mode="offline-reliability", runs=report.runs,
                                 reliability_aggregate=aggregate_reliability(report.runs))
