"""Render a concise audit report from saved Benchmark JSON, without running an Agent."""

import argparse
from pathlib import Path

from app.evaluation.benchmark import BenchmarkResult


def _cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("`", "'")


def _table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
                      *("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)])


def _rate(value):
    percent = "unavailable" if value["rate"] is None else f"{value['rate'] * 100:.2f}%"
    return f"{value['numerator']}/{value['denominator']} = {percent}"


def render_report(result: BenchmarkResult) -> str:
    base, rel = result.baseline, result.reliability
    aggregate, reliable = base.aggregate, rel.reliability_aggregate
    errors, history = result.baseline_error_accounting, result.historical_comparison
    count = aggregate["case_count"]
    lines = [
        "# StockMind Offline Evaluation Report", "",
        "> Scripted/fixture contract checks. Not a real-LLM benchmark or financial accuracy claim.", "",
        f"Generated at: {result.generated_at} (`generated_at`). Started at: {result.started_at} (`started_at`).",
        f"Execution status: **{result.status}** (`status`). Elapsed: {result.elapsed_seconds:.6f} s (`elapsed_seconds`).",
        f"Source revision: `{result.source_revision['commit']}`; dirty worktree: {result.source_revision['worktree_dirty']} (`source_revision`).", "",
        "## A. Benchmark Scope", "",
        f"Version: `{result.benchmark_version}`. Datasets: {result.scope['dataset_count']}; cases: {result.scope['case_count']} (`scope`).",
        f"Execution: {result.scope['execution']}. Models: {', '.join(result.scope['models'])}.",
        f"Mock financial provider: {result.scope['provider']}. Data: **{result.scope['data_kind']}**.",
        f"Checkpoint: {result.scope['checkpoint']}.", "",
        _table(["Suite", "Dataset version", "Cases", "Dataset / fixture SHA256"], [
            (name, suite.dataset_version, len(suite.runs), f"{suite.dataset_sha256} / {suite.fixture_sha256}")
            for name, suite in (("baseline", base), ("reliability", rel))]), "",
        "Suite metadata paths: `baseline` and `reliability`; counts are their `runs` lengths. Runtime package versions are retained in each suite's `runtime_versions`.",
        "Covers Agent Workflow contracts, scripted tool execution, grounding consistency and reliability faults. These are separate from real-world LLM planning, tool selection or financial correctness, which are not measured.", "",
        "## B. P2-A Agent Evaluation", "",
        _table(["Current measured metric", "Value", "JSON source"], [
            ("Task Completion", f"{result.baseline_counts['task_completed']}/{count} ({aggregate['task_completion_rate'] * 100:.2f}%)", "baseline_counts.task_completed; baseline.aggregate.task_completion_rate; baseline.aggregate.case_count"),
            ("Grounding Consistency", f"{result.baseline_counts['grounded']}/{count} ({aggregate['grounding_rate'] * 100:.2f}%)", "baseline_counts.grounded; baseline.aggregate.grounding_rate; baseline.aggregate.case_count"),
            *[(f"Tool selection macro {metric}", f"{aggregate['macro_tool_' + metric]:.6f}", f"baseline.aggregate.macro_tool_{metric}") for metric in ("precision", "recall", "f1")],
            ("Tool call attempts", aggregate["total_tool_calls"], "baseline.aggregate.total_tool_calls"),
            ("Recorded tool error attempts", errors["recorded_tool_error_attempts"], "baseline_error_accounting.recorded_tool_error_attempts"),
            ("Tool errors matching expected scenario text", errors["matching_expected_tool_error_attempts"], "baseline_error_accounting.matching_expected_tool_error_attempts"),
            ("Tool errors not matching expected text", errors["unmatched_tool_error_attempts"], "baseline_error_accounting.unmatched_tool_error_attempts"),
            ("Observed State errors / expected State errors", f"{aggregate['total_errors']} / {errors['expected_state_errors']}", "baseline.aggregate.total_errors; baseline_error_accounting.expected_state_errors"),
            ("Unexpected failure cases", len(errors["unexpected_failure_case_ids"]), "baseline_error_accounting.unexpected_failure_case_ids (length)"),
            ("Mean execution latency (ms)", f"{aggregate['average_latency_ms']:.6f}", "baseline.aggregate.average_latency_ms"),
        ]), "",
        "Expected errors are declared dataset scenarios, not unexpected failures. State errors also include rejected requests; they are not synonymous with executed tool errors. Per-case expected counts/text and observed counts are in `baseline_error_accounting.cases`.",
        "A scripted tool-selection F1 of 1.0 does not establish autonomous LLM tool choice. Grounding reuses the production assembler; it is not independent financial fact checking.", "",
        _table(["Case ID", "Final status", "Completed", "Grounded", "Tool attempts", "State errors", "Latency ms", "Failure reasons"], [
            (run.case_id, run.execution.final_status, run.task_completed, run.grounded, run.execution.tool_call_count,
             run.execution.error_count, f"{run.execution.latency_ms:.6f}",
             "; ".join(run.failure_reasons + (run.errors if run.failure_reasons else [])) or "none") for run in base.runs]),
        "", "Case sources: `baseline.runs[]`; latency provenance: `execution.latency_source`. Every case is included, including failures.", "",
        "## C. P2-B Reliability Evaluation", "",
        _table(["Current measured metric", "Value", "JSON source"], [
            *[(label, reliable["verdict_counts"][label], f"reliability.reliability_aggregate.verdict_counts.{label}") for label in ("pass", "fail", "unsupported")],
            ("Safe Termination Rate", _rate(reliable["safe_termination"]), "reliability.reliability_aggregate.safe_termination"),
            ("Recovery Rate", _rate(reliable["recovery"]), "reliability.reliability_aggregate.recovery"),
            ("Unsupported included in recovery denominator", reliable["recovery"]["unsupported_in_denominator"], "reliability.reliability_aggregate.recovery.unsupported_in_denominator"),
            ("Safety Violation Count", reliable["safety_violation_count"], "reliability.reliability_aggregate.safety_violation_count"),
            ("Tool attempts", rel.aggregate["total_tool_calls"], "reliability.aggregate.total_tool_calls"),
            ("State errors", rel.aggregate["total_errors"], "reliability.aggregate.total_errors"),
            ("Mean execution latency (ms)", f"{rel.aggregate['average_latency_ms']:.6f}", "reliability.aggregate.average_latency_ms"),
        ]), "",
        "Unsupported is never counted as success and remains in the declared recovery denominator. Safe termination excludes checkpoint recovery probes; zero eligible cases produce a null rate. A controlled failure can satisfy reliability while task_completed is false.", "",
        _table(["Outcome / failure category", "Count"], reliable["outcome_counts"].items()),
        "", "Source: `reliability.reliability_aggregate.outcome_counts`. No weighted total score.", "",
        _table(["Case ID", "Final status", "Verdict", "Observed category", "Recovery", "Failure / unsupported reason"], [
            (run.case_id, run.execution.final_status, run.reliability.verdict, run.reliability.outcome,
             run.reliability.recovery_outcome,
             "; ".join(run.reliability.failure_reasons + (run.errors if run.reliability.verdict == "fail" else [])) or "none") for run in rel.runs]), "",
        "Case sources: `reliability.runs[]`. Full injected faults, actual traces, observations, safety evidence and recovery checks are retained in JSON.",
        "Checkpoint coverage: native pause/resume with a new Agent sharing an in-process saver, different-thread/ticker isolation, and completed-timeout END replay. END replay has no pending node and cannot directly retry research. Durable/cross-process recovery is not measured here.", "",
        "## D. Historical Before / After Comparison", "",
        f"Source kind: **{history['source_kind']}**. Source: `{history['source_path']}`, section `{history['source_section']}`, commit `{history['source_commit']}`.",
        history["note"],
        f"Historical raw results tracked in Git: **{history['raw_results_tracked']}**. These historical values are not this run's output; current results are in section C.", "",
        _table(["Historical metric", "Before fix (recorded)", "After fix (recorded)"], [
            (key, history["before"][key], history["after_fix"][key]) for key in history["before"]]),
        "", "Sources: `historical_comparison.before` / `historical_comparison.after_fix`. They are fixed, explicitly sourced historical data, not recalculated current scores.", "",
        *[f"- {defect}" for defect in history["defects"]], "",
        "## E. Metric Definitions and Limitations", "",
        *[f"- **{name}** — {item['definition']} Source: `{item['path']}`." for name, item in result.metric_definitions.items()], "",
        "- Not a real-LLM benchmark, live-market accuracy validation or independent financial truth check.",
        "- Mock faults do not cover all real Provider failure modes or synchronous network-thread cancellation.",
        "- Completed timeout checkpoints at END cannot directly resume failed research.",
        "- These checks do not establish suitability for automated trading or investment decisions.",
        "- Input versions/hashes, case ordering and stable outcomes are reproducible in the recorded runtime; durations and timestamps vary by machine/load. Short timeout fixtures can fail on slow environments; those failures remain visible.",
        "- Dirty worktrees and unpinned dependency combinations limit exact source/environment reproduction; consult source_revision and runtime_versions.",
        "- Benchmark JSON/Markdown are local generated artifacts. A successful CLI write does not mean every case passed; inspect status and verdict_counts.", "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate Markdown from saved offline Benchmark JSON; no Agent calls")
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = BenchmarkResult.model_validate_json(args.results.read_text(encoding="utf-8"))
    output = args.output or args.results.with_name("evaluation-report.md")
    output.write_text(render_report(result), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
