"""Offline orchestration and audit metadata; scoring stays in the existing runners."""

from __future__ import annotations

import copy
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.evaluation.dataset import EvaluationDataset, load_dataset
from app.evaluation.evaluator import EvaluationReport, Evaluator
from app.evaluation.reliability import ReliabilityEvaluator, ReliabilityReport
from app.evaluation.reliability_dataset import load_reliability_dataset

DEFAULT_OUTPUT_DIR = Path("benchmark-results")
ROOT = Path(__file__).resolve().parents[2]

# Explicit historical transcription, NOT a result produced by the current runner.
HISTORY = {
    "source_kind": "historical_development_record",
    "source_commit": "22a6d5039547a1d102f9e29ebd48c4f4c96b2292",
    "source_path": "docs/codex-log.md",
    "source_section": "2026-11-17 — P2-B Fix：Agent Reliability Hardening",
    "raw_results_tracked": False,
    "note": "Transcribed from committed development records. Historical raw JSON was written to /tmp, not tracked in Git. Neither historical column is recomputed by this benchmark.",
    "before": {"pass": 9, "fail": 2, "unsupported": 1, "safe_termination": "8/10 = 80%",
               "recovery": "7/8 = 87.5%", "safety_violations": 1},
    "after_fix": {"pass": 11, "fail": 0, "unsupported": 1, "safe_termination": "10/10 = 100%",
                  "recovery": "7/8 = 87.5%", "safety_violations": 0},
    "defects": ["Identical tool requests in the same batch executed twice.",
                "An unrecovered LLM exception was overwritten with terminal finished during Finalize."],
}

METRIC_DEFINITIONS = {
    "task_completion": {"path": "baseline.aggregate.task_completion_rate",
                        "definition": "All existing completion checks must pass, including finished status, required evidence/tools and declared error expectations. Numerator: baseline_counts.task_completed; denominator: baseline.aggregate.case_count (all cases)."},
    "tool_selection": {"path": "baseline.aggregate.macro_tool_{precision,recall,f1}",
                       "definition": "Unweighted mean over all cases of executed-tool-name set precision, recall and F1; duplicates do not inflate the set. Both sets empty => 1/1/1; only one empty => 0/0/0. Not autonomous LLM selection performance."},
    "grounding": {"path": "baseline.aggregate.grounding_rate",
                  "definition": "Passing report schema/ticker/finite-value/assembler-consistency checks divided by all baseline cases. Numerator: baseline_counts.grounded. The production assembler is the shared oracle, not independent financial truth."},
    "tool_calls": {"path": "baseline.aggregate.total_tool_calls",
                   "definition": "Sum of recorded tool_call attempts, including errors/retries; blocked or duplicate-skipped requests are not executed attempts."},
    "state_errors": {"path": "baseline.aggregate.total_errors",
                     "definition": "Sum of State.errors lengths, not tool errors alone. Includes blocked requests; transient retry errors may appear only in traces."},
    "tool_errors": {"path": "baseline_error_accounting.recorded_tool_error_attempts",
                    "definition": "Actual attempts with a non-null error in baseline.runs[].actual_calls. Matching expected errors means matching declared dataset error text, not a new success score."},
    "unexpected_failures": {"path": "baseline_error_accounting.unexpected_failure_case_ids",
                            "definition": "Baseline cases with task_completed=false, grounded=false or existing failure_reasons. Kept separate from expected scenario errors; no new scoring rules."},
    "latency": {"path": "baseline.aggregate.average_latency_ms",
                "definition": "Unweighted mean of run.execution.latency_ms. Read latency_source per case: run_end_trace or runner_elapsed. Local timing, not a performance guarantee; checkpoint probe timing includes isolation work."},
    "safe_termination": {"path": "reliability.reliability_aggregate.safe_termination",
                         "definition": "Passing eligible cases / all cases with safe_termination_required=true. Existing checks include expected status, handling, evidence, attempts, recovery and no safety violations. Checkpoint recovery probes are not eligible."},
    "recovery": {"path": "reliability.reliability_aggregate.recovery",
                 "definition": "Passing recovered cases / all declared recovery cases. Unsupported stays in the denominator, never the numerator. Capturing an exception alone is not recovery. Zero denominator => null."},
    "safety_violations": {"path": "reliability.reliability_aggregate.safety_violation_count",
                          "definition": "Count of recorded findings, not cases; per-case category, field and expected/observed evidence remain in reliability.runs[].reliability.safety_violations."},
    "outcomes": {"path": "reliability.reliability_aggregate.outcome_counts",
                 "definition": "Counts of normal_completion, safe_degradation, controlled_failure, unexpected_failure and unsupported. Controlled failure can pass a reliability contract without completing the research task."},
    "benchmark_status": {"path": "status",
                         "definition": "completed_with_failures if baseline has existing failed checks or reliability has fail; otherwise completed_with_unsupported if any unsupported; otherwise completed. No weighted score."},
}


class BenchmarkResult(BaseModel):
    benchmark_version: Literal["stockmind-benchmark-v1"] = "stockmind-benchmark-v1"
    started_at: str
    generated_at: str
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    status: Literal["completed", "completed_with_failures", "completed_with_unsupported"]
    source_revision: dict[str, Any]
    scope: dict[str, Any]
    metric_definitions: dict[str, dict[str, str]]
    baseline: EvaluationReport
    reliability: ReliabilityReport
    baseline_counts: dict[str, int]
    baseline_error_accounting: dict[str, Any]
    historical_comparison: dict[str, Any]


def source_revision() -> dict[str, Any]:
    """Local Git metadata only; never read remotes, credentials or environment files."""
    try:
        def git(*args):
            return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        return {"commit": git("rev-parse", "HEAD"), "worktree_dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unavailable", "worktree_dirty": None}


def assemble_benchmark(baseline: EvaluationReport, reliability: ReliabilityReport,
                       dataset: EvaluationDataset, *, started_at: str,
                       elapsed_seconds: float, revision: dict[str, Any]) -> BenchmarkResult:
    """Summarize existing result fields and label dataset expectations; do not rescore."""
    rules = {case.case_id: case.constraints for case in dataset.cases}
    cases = []
    for run in baseline.runs:
        rule = rules[run.case_id]
        errors = [call["error"] for call in run.actual_calls if call.get("error") is not None]
        matching = sum(any(pattern in error for pattern in rule.error_contains) for error in errors)
        cases.append({"case_id": run.case_id, "expected_state_errors": rule.expected_error_count,
                      "expected_error_text": rule.error_contains, "observed_state_errors": run.execution.error_count,
                      "error_expectations_met": all(run.completion_checks.get(key, False) for key in ("error_count", "expected_errors")),
                      "recorded_tool_error_attempts": len(errors), "matching_expected_tool_error_attempts": matching,
                      "unmatched_tool_error_attempts": len(errors) - matching})
    unexpected = [run.case_id for run in baseline.runs if not run.task_completed or not run.grounded or run.failure_reasons]
    counts = reliability.reliability_aggregate["verdict_counts"]
    status = ("completed_with_failures" if unexpected or counts["fail"] else
              "completed_with_unsupported" if counts["unsupported"] else "completed")
    accounting = {key: sum(case[key] for case in cases) for key in (
        "expected_state_errors", "observed_state_errors", "recorded_tool_error_attempts",
        "matching_expected_tool_error_attempts", "unmatched_tool_error_attempts")}
    accounting.update(cases=cases, unexpected_failure_case_ids=unexpected)
    return BenchmarkResult(
        started_at=started_at, generated_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=elapsed_seconds,
        status=status, source_revision=revision,
        scope={"dataset_count": 2, "case_count": len(baseline.runs) + len(reliability.runs),
               "execution": "offline, sequential: baseline then reliability", "data_kind": "synthetic-fixture",
               "models": ["ScriptedReActDemoModel", "FaultModel"], "provider": "evaluation-only FixtureProvider and local fault wrappers",
               "checkpoint": "fresh in-process InMemorySaver per case; shared only by checkpoint probes within a case",
               "coverage": ["Agent Workflow contracts", "scripted tool execution", "grounding consistency", "failure injection and recovery"],
               "real_world_llm_performance_measured": False},
        metric_definitions=copy.deepcopy(METRIC_DEFINITIONS), baseline=baseline, reliability=reliability,
        baseline_counts={"task_completed": sum(run.task_completed for run in baseline.runs),
                         "grounded": sum(run.grounded for run in baseline.runs)},
        baseline_error_accounting=accounting, historical_comparison=copy.deepcopy(HISTORY),
    )


async def run_benchmark(*, language: str = "en") -> BenchmarkResult:
    """Fixed offline entry point; existing runners create fresh per-case agents/state."""
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    revision = source_revision()
    dataset = load_dataset()
    baseline = await Evaluator(language=language).evaluate(dataset)
    reliability = await ReliabilityEvaluator(language=language).evaluate(load_reliability_dataset())
    return assemble_benchmark(baseline, reliability, dataset, started_at=started_at,
                              elapsed_seconds=time.perf_counter() - start, revision=revision)


def write_benchmark(result: BenchmarkResult, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    from app.evaluation.report_generator import render_report

    # Render from the serialized result, so the Markdown and JSON share one input.
    payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    markdown = render_report(BenchmarkResult.model_validate_json(payload))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output_dir / "evaluation-results.json", output_dir / "evaluation-report.md"
    json_path.write_text(payload, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return json_path, markdown_path
