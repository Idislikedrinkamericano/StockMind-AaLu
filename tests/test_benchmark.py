"""Offline Benchmark/report regressions; original evaluators remain the scoring oracle."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.evaluation.benchmark import BenchmarkResult, run_benchmark, write_benchmark
from app.evaluation.evaluator import Evaluator
from app.evaluation.reliability import ReliabilityEvaluator
from app.evaluation.report_generator import main as report_main, render_report
from examples import evaluate as cli


def _offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Benchmark/report must not load .env, select a real provider/model, or connect to a network")

    for target in ("socket.socket.connect", "dotenv.load_dotenv", "app.agents.react_agent.get_chat_model",
                   "app.agents.react_agent.get_data_provider"):
        monkeypatch.setattr(target, forbidden)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    _offline(monkeypatch)


@pytest.fixture(scope="module")
def result():
    with pytest.MonkeyPatch.context() as patch:
        _offline(patch)
        return asyncio.run(run_benchmark())


def test_combined_runner_keeps_original_suite_results_and_raw_evidence(result):
    assert result.benchmark_version == "stockmind-benchmark-v1"
    assert result.baseline.dataset_version == "stockmind-eval-v1"
    assert result.reliability.dataset_version == "stockmind-reliability-v1"
    assert len(result.baseline.runs) == len(result.reliability.runs) == 12
    assert result.scope["dataset_count"] == 2 and result.scope["case_count"] == 24
    assert result.status == "completed_with_unsupported"
    assert result.baseline_counts == {"task_completed": 12, "grounded": 12}
    assert result.baseline.aggregate["total_tool_calls"] == 25
    assert result.reliability.reliability_aggregate["verdict_counts"] == {"pass": 11, "fail": 0, "unsupported": 1}
    assert result.elapsed_seconds > 0 and result.generated_at >= result.started_at
    for suite in (result.baseline, result.reliability):
        assert suite.runtime_versions["python"]
        assert len(suite.dataset_sha256) == len(suite.fixture_sha256) == 64
        assert suite.runs[0].report is not None and suite.runs[0].observations
    assert result.reliability.runs[0].reliability.injection_events
    assert result.reliability.runs[0].reliability.observed_behavior["trace"]


def test_expected_errors_are_not_unexpected_failures(result):
    errors = result.baseline_error_accounting
    assert errors["observed_state_errors"] == errors["expected_state_errors"] == 3
    assert errors["recorded_tool_error_attempts"] == errors["matching_expected_tool_error_attempts"] == 2
    assert errors["unmatched_tool_error_attempts"] == 0 and errors["unexpected_failure_case_ids"] == []
    rejected = next(case for case in errors["cases"] if case["case_id"] == "wrong-tool-ticker")
    assert rejected["observed_state_errors"] == 1 and rejected["recorded_tool_error_attempts"] == 0
    assert rejected["error_expectations_met"]


def test_json_markdown_roundtrip_and_metric_sources(result, tmp_path):
    json_path, md_path = write_benchmark(result, tmp_path / "nested" / "output")
    saved = BenchmarkResult.model_validate_json(json_path.read_text())
    assert saved == result
    json.dumps(saved.model_dump(mode="json"), allow_nan=False)
    markdown = md_path.read_text()
    assert markdown == render_report(saved)
    assert "| Task Completion | 12/12 (100.00%) |" in markdown
    assert "| Grounding Consistency | 12/12 (100.00%) |" in markdown
    assert "| Tool call attempts | 25 | baseline.aggregate.total_tool_calls |" in markdown
    assert "| Recorded tool error attempts | 2 |" in markdown
    for metric in ("precision", "recall", "f1"):
        assert f"| Tool selection macro {metric} | 1.000000 | baseline.aggregate.macro_tool_{metric} |" in markdown
    assert f"{saved.baseline.aggregate['average_latency_ms']:.6f}" in markdown
    for suite in (saved.baseline, saved.reliability):
        for run in suite.runs:
            assert f"| {run.case_id} | {run.execution.final_status} |" in markdown
    assert saved.generated_at in markdown and saved.baseline.dataset_version in markdown


def test_report_uses_input_scores_instead_of_hardcoded_current_results(result):
    changed = result.model_copy(deep=True)
    changed.baseline.aggregate.update(macro_tool_precision=0.125, macro_tool_recall=0.25, macro_tool_f1=1 / 6,
                                      average_latency_ms=123.456789, total_tool_calls=31, task_completion_rate=0.5)
    changed.baseline_counts["task_completed"] = 6
    markdown = render_report(changed)
    assert "| Task Completion | 6/12 (50.00%) |" in markdown
    assert "| Tool selection macro precision | 0.125000 |" in markdown
    assert "| Tool selection macro recall | 0.250000 |" in markdown
    assert "| Tool selection macro f1 | 0.166667 |" in markdown
    assert "| Tool call attempts | 31 |" in markdown
    assert "| Mean execution latency (ms) | 123.456789 |" in markdown


def test_denominators_and_unsupported_are_preserved(result):
    rates = result.reliability.reliability_aggregate
    assert rates["safe_termination"]["numerator"] == rates["safe_termination"]["denominator"] == 10
    assert rates["recovery"] == {"numerator": 7, "denominator": 8, "rate": 0.875, "unsupported_in_denominator": 1}
    text = render_report(result)
    assert "| Safe Termination Rate | 10/10 = 100.00% |" in text
    assert "| Recovery Rate | 7/8 = 87.50% |" in text
    assert "| checkpoint-terminal-retry | timeout | unsupported |" in text
    changed = result.model_copy(deep=True)
    changed.reliability.reliability_aggregate["recovery"] = {
        "numerator": 0, "denominator": 0, "rate": None, "unsupported_in_denominator": 0}
    assert "| Recovery Rate | 0/0 = unavailable |" in render_report(changed)


def test_failed_cases_and_unsupported_remain_in_real_runner_report(monkeypatch, tmp_path):
    original = Evaluator._build_agent
    def baseline_failure(self, case):
        if case.case_id == "price-nvda":
            raise RuntimeError("offline baseline construction failure")
        return original(self, case)

    original_reliability = ReliabilityEvaluator._build_agent
    def reliability_failure(self, case):
        if case.case_id == "provider-retry":
            raise RuntimeError("offline reliability construction failure")
        return original_reliability(self, case)

    monkeypatch.setattr(Evaluator, "_build_agent", baseline_failure)
    monkeypatch.setattr(ReliabilityEvaluator, "_build_agent", reliability_failure)
    result = asyncio.run(run_benchmark())
    assert result.status == "completed_with_failures"
    assert len(result.baseline.runs) == len(result.reliability.runs) == 12
    assert result.baseline_error_accounting["unexpected_failure_case_ids"] == ["price-nvda"]
    assert result.reliability.reliability_aggregate["verdict_counts"] == {"pass": 10, "fail": 1, "unsupported": 1}
    paths = write_benchmark(result, tmp_path)
    text = paths[1].read_text()
    assert "| price-nvda | error | False | False |" in text
    assert "| provider-retry | error | fail |" in text
    assert "| checkpoint-terminal-retry | timeout | unsupported |" in text
    assert "construction failure" in text
    assert result.historical_comparison["after_fix"]["pass"] == 11  # Historical, not overwritten by current failure.


def test_historical_source_is_separate_from_current_execution(result):
    history = result.historical_comparison
    assert history["source_kind"] == "historical_development_record"
    assert not history["raw_results_tracked"] and history["source_path"] == "docs/codex-log.md"
    assert history["source_commit"].startswith("22a6d50")
    assert history["before"]["pass"] == 9 and history["before"]["safety_violations"] == 1
    assert history["after_fix"]["pass"] == 11 and history["after_fix"]["safety_violations"] == 0
    text = render_report(result)
    assert "Neither historical column is recomputed" in text
    assert "not this run's output" in text and history["source_commit"] in text


@pytest.mark.parametrize("custom_dir", [False, True])
def test_cli_output_directory_and_no_credentials(monkeypatch, tmp_path, capsys, custom_dir):
    monkeypatch.chdir(tmp_path)
    # Keys and live provider settings must not influence model/data selection or leak into artifacts.
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-real-key")
    monkeypatch.setenv("STOCKMIND_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("STOCKMIND_DATA_PROVIDER", "akshare")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "offline-telemetry-placeholder")
    monkeypatch.setenv("STOCKMIND_TRACE_DIR", str(tmp_path / "unexpected-traces"))
    directory = tmp_path / ("custom/nested" if custom_dir else "benchmark-results")
    args = ["evaluate.py", "--suite", "benchmark"]
    if custom_dir:
        args += ["--output-dir", str(directory), "--lang", "zh"]
    monkeypatch.setattr("sys.argv", args)
    asyncio.run(cli.main())
    output = capsys.readouterr()
    assert "stockmind-benchmark-v1: completed_with_unsupported" in output.err
    assert "evaluation-results.json" in output.out and "evaluation-report.md" in output.out
    payload = (directory / "evaluation-results.json").read_text()
    assert "offline-test-not-a-real-key" not in payload and "offline-telemetry-placeholder" not in payload
    assert not (tmp_path / "unexpected-traces").exists()
    result = BenchmarkResult.model_validate_json(payload)
    assert result.baseline.language == ("zh" if custom_dir else "en")
    assert (directory / "evaluation-report.md").read_text() == render_report(result)


def test_report_regeneration_does_not_execute_agents(result, tmp_path, monkeypatch, capsys):
    paths = write_benchmark(result, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Markdown regeneration must only read saved JSON")

    monkeypatch.setattr(Evaluator, "evaluate", forbidden)
    monkeypatch.setattr(ReliabilityEvaluator, "evaluate", forbidden)
    target = tmp_path / "regenerated.md"
    monkeypatch.setattr("sys.argv", ["report_generator", str(paths[0]), "--output", str(target)])
    report_main()
    assert target.read_bytes() == paths[1].read_bytes() and str(target) in capsys.readouterr().out


def test_repeatability_uses_independent_runs_and_stable_fields(result):
    repeated = asyncio.run(run_benchmark())
    assert repeated.baseline_counts == result.baseline_counts
    assert repeated.baseline_error_accounting == result.baseline_error_accounting
    assert repeated.reliability.reliability_aggregate == result.reliability.reliability_aggregate
    for left, right in ((result.baseline, repeated.baseline), (result.reliability, repeated.reliability)):
        assert left.dataset_sha256 == right.dataset_sha256 and left.fixture_sha256 == right.fixture_sha256
        assert [run.case_id for run in left.runs] == [run.case_id for run in right.runs]
        for a, b in zip(left.runs, right.runs):
            assert a.actual_calls == b.actual_calls
            assert a.report.model_dump(exclude={"generated_at"}) == b.report.model_dump(exclude={"generated_at"})
    assert repeated.generated_at != result.generated_at  # Byte identity of timings is not promised.


@pytest.mark.parametrize("suite,version", [("baseline", "stockmind-eval-v1"), ("reliability", "stockmind-reliability-v1")])
def test_existing_cli_json_output_compatible(monkeypatch, capsys, suite, version):
    monkeypatch.setattr("sys.argv", ["evaluate.py", "--suite", suite])
    asyncio.run(cli.main())
    data = json.loads(capsys.readouterr().out)
    assert data["dataset_version"] == version and len(data["runs"]) == 12
    assert "benchmark_version" not in data


@pytest.mark.parametrize("args", [
    ["--suite", "benchmark", "--dataset", "anything.json"],
    ["--suite", "benchmark", "--output", "anything.json"],
    ["--output-dir", "anywhere"],
])
def test_cli_rejects_ambiguous_outputs_before_execution(args, monkeypatch):
    monkeypatch.setattr("sys.argv", ["evaluate.py", *args])
    with pytest.raises(SystemExit) as raised:
        asyncio.run(cli.main())
    assert raised.value.code == 2


def test_default_generated_directory_is_ignored_by_git():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "check-ignore", "benchmark-results/evaluation-results.json",
                             "benchmark-results/evaluation-report.md"], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0 and len(result.stdout.splitlines()) == 2
