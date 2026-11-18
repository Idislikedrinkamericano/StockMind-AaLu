"""Rule-based scoring of actual Agent state and existing trace events.

Default execution is offline/scripted. These scores are contract checks, not
autonomous LLM performance, independent financial truth, or investment quality.
"""
from __future__ import annotations

import hashlib
import math
import platform
import time
from importlib.metadata import version
from typing import Any, Callable

from langsmith import tracing_context
from pydantic import BaseModel, Field

from app.evaluation.dataset import EvaluationCase, EvaluationDataset
from app.evaluation.fixtures import FIXTURE_PATH, build_offline_agent
from app.graph.workflow import _grounded_or_no_data_report
from app.schemas.report import StockResearchReport


class ToolSelection(BaseModel):
    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int


def tool_selection(expected: list[str], actual: list[str]) -> ToolSelection:
    """Set matching. Both empty => 1/1/1; only one empty => 0/0/0."""
    expected_set, actual_set = set(expected), set(actual)
    tp = len(expected_set & actual_set)
    precision = tp / len(actual_set) if actual_set else float(not expected_set)
    recall = tp / len(expected_set) if expected_set else float(not actual_set)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ToolSelection(precision=precision, recall=recall, f1=f1, true_positives=tp,
                         false_positives=len(actual_set - expected_set),
                         false_negatives=len(expected_set - actual_set))


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(item) for item in value)
    return True


def evidence_checks(state: dict[str, Any], ticker: str) -> dict[str, bool]:
    """Reuse the workflow's assembler/no-data policy, excluding generated_at.

    This checks output consistency, not provider truth or assembler correctness.
    Exact content comparison detects added claims without parsing prose numbers.
    """
    report = state.get("final_output")
    checks = dict.fromkeys(("report_schema", "ticker_matches_request", "finite_numbers",
                            "metrics_match_observations", "content_matches_assembler"), False)
    if not isinstance(report, StockResearchReport):
        return checks
    checks["report_schema"] = True
    checks["ticker_matches_request"] = report.ticker == ticker and state.get("ticker") == ticker
    checks["finite_numbers"] = _finite(report.model_dump())
    expected = _grounded_or_no_data_report({**state, "ticker": ticker}, ticker)
    checks["metrics_match_observations"] = report.key_metrics == expected.key_metrics
    excluded = {"generated_at", "key_metrics", "ticker"}
    checks["content_matches_assembler"] = report.model_dump(exclude=excluded) == expected.model_dump(exclude=excluded)
    return checks


def completion_checks(case: EvaluationCase, state: dict[str, Any], actual_tools: list[str],
                      evidence: dict[str, bool]) -> dict[str, bool]:
    report = state.get("final_output")
    errors = state.get("errors", [])
    rule = case.constraints
    checks = {
        "finished": state.get("status") == "finished",
        "valid_report": isinstance(report, StockResearchReport),
        "evidence": all(evidence.values()),
        "expected_tools_executed": set(case.expected_tools) <= set(actual_tools),
        "error_count": len(errors) == rule.expected_error_count,
        "expected_errors": all(any(text in error for error in errors) for text in rule.error_contains),
    }
    if not isinstance(report, StockResearchReport):
        return checks
    metrics = report.key_metrics
    checks["report_mode"] = bool(metrics) if rule.report_mode == "grounded" else metrics == {}
    for name in rule.required_metrics:
        value = metrics.get(name)
        checks[f"required_metric:{name}"] = isinstance(value, (float, int)) and not isinstance(value, bool) and _finite(value)
    for name in rule.absent_metrics:
        checks[f"absent_metric:{name}"] = metrics.get(name) is None
    for name, expected in rule.expected_values.items():
        value = metrics.get(name)
        checks[f"expected_value:{name}"] = (isinstance(value, (float, int)) and not isinstance(value, bool)
                                            and math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12))
    for name in rule.available_sections:
        value = getattr(report, name)
        checks[f"available_section:{name}"] = bool(value) and value != "Not covered in this phase." and not value.startswith("Unavailable")
    for name in rule.unavailable_sections:
        checks[f"unavailable_section:{name}"] = getattr(report, name).startswith("Unavailable")
    text = report.model_dump_json()
    for forbidden in rule.forbidden_text:
        checks[f"forbidden_text:{forbidden}"] = forbidden not in text
    return checks


class ExecutionMetrics(BaseModel):
    tool_call_count: int  # recorded tool_call attempts, including retry/error events
    requested_tool_count: int
    observation_count: int
    error_count: int  # state errors, not duplicated trace errors
    latency_ms: float
    latency_source: str
    final_status: str


class RunMetrics(BaseModel):
    case_id: str
    question: str
    ticker: str
    scenario: str
    expected_tools: list[str]
    actual_tools: list[str]
    requested_tools: list[str]
    actual_calls: list[dict[str, Any]]
    tool_selection: ToolSelection
    task_completed: bool
    completion_checks: dict[str, bool]
    grounded: bool
    evidence_checks: dict[str, bool]
    execution: ExecutionMetrics
    errors: list[str]
    failure_reasons: list[str]
    report: StockResearchReport | None
    observations: list[dict[str, Any]]


class EvaluationReport(BaseModel):
    dataset_version: str
    dataset_sha256: str
    fixture_version: str
    fixture_sha256: str
    runner_mode: str
    language: str
    runtime_versions: dict[str, str]
    runs: list[RunMetrics] = Field(default_factory=list)
    aggregate: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        return (f"{self.runner_mode} — {self.dataset_version}\n"
                f"Cases: {len(self.runs)}; completed: {sum(run.task_completed for run in self.runs)}\n"
                f"Macro tool F1: {self.aggregate['macro_tool_f1']:.3f}; "
                f"grounding consistency: {self.aggregate['grounding_rate']:.3f}\n"
                "Scripted/fixture results are not real-LLM performance or financial accuracy.")


class Evaluator:
    """Sequential evaluator; injected ticker -> agent factories must return fresh agents.

    The CLI never injects a factory. Library callers control the data/model
    access of custom factories, whose results are labelled separately.
    """

    def __init__(self, agent_factory: Callable[[str], Any] | None = None, *, language: str = "en"):
        if language not in {"en", "zh"}:
            raise ValueError("Evaluation language must be en or zh")
        self.agent_factory = agent_factory
        self.language = language

    async def evaluate(self, dataset: EvaluationDataset) -> EvaluationReport:
        dataset = type(dataset).model_validate(dataset.model_dump())
        runs = []
        for case in dataset.cases:
            agent = None
            start = time.perf_counter()
            try:
                # Explicitly disable network telemetry from ambient environment settings.
                with tracing_context(enabled=False):
                    agent = self._build_agent(case)
                    state = await self._invoke_case(agent, case)
            except Exception as exc:
                state = {"ticker": case.ticker, "language": self.language, "status": "error",
                         "errors": [f"{type(exc).__name__}: {exc}"]}
            elapsed = (time.perf_counter() - start) * 1000.0
            runs.append(self._metrics(agent, state, case, elapsed))
        count = len(runs)
        mean = lambda values: sum(values) / count
        aggregate = {
            "case_count": count,
            "denominator": "all cases, including runner failures and safe-failure scenarios",
            "tool_aggregation": "unweighted macro average of per-case set scores",
            "macro_tool_precision": mean([run.tool_selection.precision for run in runs]),
            "macro_tool_recall": mean([run.tool_selection.recall for run in runs]),
            "macro_tool_f1": mean([run.tool_selection.f1 for run in runs]),
            "task_completion_rate": mean([run.task_completed for run in runs]),
            "grounding_rate": mean([run.grounded for run in runs]),
            "average_latency_ms": mean([run.execution.latency_ms for run in runs]),
            "total_tool_calls": sum(run.execution.tool_call_count for run in runs),
            "total_errors": sum(run.execution.error_count for run in runs),
        }
        return EvaluationReport(
            dataset_version=dataset.version,
            dataset_sha256=hashlib.sha256(dataset.model_dump_json().encode()).hexdigest(),
            fixture_version=dataset.fixture_version,
            fixture_sha256=hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
            runner_mode="offline-scripted" if self.agent_factory is None else "custom-factory-unverified",
            language=self.language,
            runtime_versions={"python": platform.python_version(), **{
                name: version(name) for name in ("langchain-core", "langgraph", "pydantic", "numpy")}},
            runs=runs, aggregate=aggregate,
        )

    def _build_agent(self, case):
        return self.agent_factory(case.ticker) if self.agent_factory else build_offline_agent(case, self.language)

    async def _invoke_case(self, agent, case):
        return await agent.arun(case.question, ticker=case.ticker, thread_id=f"eval-{case.case_id}")

    @staticmethod
    def _metrics(agent: Any, state: dict[str, Any], case: EvaluationCase, elapsed: float) -> RunMetrics:
        events = agent.tracer.events if agent is not None else []
        attempts = [event for event in events if event.event_type == "tool_call"]
        actual = sorted({event.tool_name for event in attempts if event.tool_name})
        requested = [call.get("name", "") for call in state.get("tool_calls", [])]
        errors = state.get("errors", [])
        end = next((event for event in reversed(events) if event.event_type == "run_end"), None)
        evidence = evidence_checks(state, case.ticker)
        completion = completion_checks(case, state, actual, evidence)
        reasons = [f"evidence:{name}" for name, passed in evidence.items() if not passed]
        reasons += [f"completion:{name}" for name, passed in completion.items() if not passed]
        selection = tool_selection(case.expected_tools, actual)
        if selection.f1 < 1:
            reasons.append("tool_selection:set_mismatch")
        report = state.get("final_output")
        return RunMetrics(
            case_id=case.case_id, question=case.question, ticker=case.ticker, scenario=case.scenario,
            expected_tools=case.expected_tools, actual_tools=actual, requested_tools=requested,
            actual_calls=[{"name": event.tool_name, "args": event.tool_input, "error": event.error} for event in attempts],
            tool_selection=selection, task_completed=all(completion.values()), completion_checks=completion,
            grounded=all(evidence.values()), evidence_checks=evidence,
            execution=ExecutionMetrics(tool_call_count=len(attempts), requested_tool_count=len(requested),
                                       observation_count=len(state.get("observations", [])), error_count=len(errors),
                                       latency_ms=end.latency_ms if end and end.latency_ms is not None else elapsed,
                                       latency_source="run_end_trace" if end and end.latency_ms is not None else "runner_elapsed",
                                       final_status=state.get("status", "error")),
            errors=errors, failure_reasons=reasons,
            report=report if isinstance(report, StockResearchReport) else None,
            observations=state.get("observations", []),
        )
