import asyncio
import copy
import json
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.evaluation import EvaluationDataset, EvaluationReport, Evaluator, load_dataset
from app.evaluation.evaluator import completion_checks, evidence_checks, tool_selection
from app.evaluation.fixtures import FixtureProvider, build_offline_agent
from app.graph.state import create_initial_state
from app.graph.workflow import _grounded_or_no_data_report
from app.observability.cost import estimate_cost_usd, token_counts
from app.observability.tracing import TraceCollector
from examples import evaluate as cli


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Evaluation tests must not select external providers or access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.agents.react_agent.get_chat_model", forbidden)
    monkeypatch.setattr("app.agents.react_agent.get_data_provider", forbidden)


def _dataset_with(*cases):
    raw = load_dataset().model_dump()
    raw["cases"] = list(cases)
    return EvaluationDataset.model_validate(raw)


def test_cost_estimation():
    assert estimate_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert estimate_cost_usd("unknown-model", 1000, 1000) == 0.0


def test_token_counts():
    assert token_counts({"input_tokens": 10, "output_tokens": 5}) == (10, 5)
    assert token_counts({"prompt_tokens": 7, "completion_tokens": 3}) == (7, 3)
    assert token_counts(None) == (0, 0)


def test_evaluation_metrics():
    report = asyncio.run(Evaluator().evaluate(load_dataset()))
    assert len(report.runs) == 12
    assert all(run.task_completed and run.grounded and not run.failure_reasons for run in report.runs)
    assert report.aggregate["task_completion_rate"] == 1.0
    assert report.aggregate["macro_tool_precision"] == 1.0
    assert report.aggregate["macro_tool_recall"] == 1.0
    assert report.aggregate["macro_tool_f1"] == 1.0
    assert report.aggregate["total_tool_calls"] == 25
    assert report.aggregate["total_errors"] == 3  # explicit safe-failure cases
    assert all(run.execution.final_status == "finished" for run in report.runs)
    assert all(run.execution.latency_ms >= 0 and run.execution.latency_source == "run_end_trace" for run in report.runs)
    blocked = next(run for run in report.runs if run.case_id == "wrong-tool-ticker")
    assert blocked.requested_tools == ["get_stock_price"]
    assert blocked.actual_tools == [] and blocked.execution.tool_call_count == 0
    assert blocked.report.key_metrics == {}
    financial = next(run for run in report.runs if run.case_id == "financials-nvda")
    assert financial.report.key_metrics["price"] is None  # Earlier NVDA price state must not leak.
    assert {obs["tool"] for obs in financial.observations} == set(financial.expected_tools)
    valuation = next(run for run in report.runs if run.case_id == "valuation-tsm")
    provenance = valuation.report.key_metrics["valuation_metadata"]
    assert provenance["pe"]["input_provenance"]["eps"]["period"] == "TTM"
    market_cap = provenance["ev_ebitda"]["input_provenance"]["market_cap"]
    assert market_cap["period"] == "instant" and market_cap["period_end"] == "2025-01-31"
    assert market_cap["published_at"] == "unavailable"
    assert "not real-LLM performance" in report.summary()
    encoded = report.model_dump_json()
    assert EvaluationReport.model_validate_json(encoded) == report
    json.dumps(report.model_dump(mode="json"), allow_nan=False)


@pytest.mark.parametrize("invalid", ["version", "duplicate_id", "empty", "tool", "argument", "ticker",
                                    "conflicting", "empty_constraint", "extra", "nonfinite"])
def test_invalid_dataset_is_rejected(invalid):
    raw = load_dataset().model_dump()
    case = raw["cases"][0]
    if invalid == "version":
        raw["version"] = "unrecognized-v2"
    elif invalid == "duplicate_id":
        raw["cases"].append(copy.deepcopy(case))
    elif invalid == "empty":
        raw["cases"] = []
    elif invalid == "tool":
        case["expected_tools"] = ["invented_tool"]
    elif invalid == "argument":
        case["script"][0]["args"] = {"tickre": "NVDA"}
    elif invalid == "ticker":
        case["ticker"] = "UNKNOWN"
        case["script"][0]["args"]["ticker"] = "UNKNOWN"
    elif invalid == "conflicting":
        case["constraints"]["absent_metrics"] = ["price"]
    elif invalid == "empty_constraint":
        case["constraints"] = {"report_mode": "grounded"}
    elif invalid == "extra":
        case["ignored_typo"] = True
    else:
        case["constraints"]["expected_values"]["price"] = float("nan")
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate(raw)


@pytest.mark.parametrize("expected,actual,values", [
    (["a", "b"], ["a", "c", "d"], (1 / 3, 1 / 2, 2 / 5)),
    (["a", "b"], ["a"], (1.0, 0.5, 2 / 3)),
    (["a"], ["a", "a"], (1.0, 1.0, 1.0)),
    ([], [], (1.0, 1.0, 1.0)),
    ([], ["a"], (0.0, 0.0, 0.0)),
    (["a"], [], (0.0, 0.0, 0.0)),
])
def test_tool_precision_recall_f1(expected, actual, values):
    score = tool_selection(expected, actual)
    assert (score.precision, score.recall, score.f1) == pytest.approx(values)
    assert score.true_positives == len(set(expected) & set(actual))
    assert score.false_positives == len(set(actual) - set(expected))
    assert score.false_negatives == len(set(expected) - set(actual))


def test_runner_reproducible_and_ignores_provider_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    monkeypatch.setenv("STOCKMIND_DATA_PROVIDER", "akshare")
    monkeypatch.setenv("STOCKMIND_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("STOCKMIND_TRACE_DIR", str(tmp_path / "unexpected-traces"))
    monkeypatch.setenv("STOCKMIND_RUN_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("STOCKMIND_LANGUAGE", "zh")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "offline-test-placeholder")
    evaluator = Evaluator()
    first = asyncio.run(evaluator.evaluate(load_dataset()))
    second = asyncio.run(evaluator.evaluate(load_dataset()))
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.fixture_sha256 == second.fixture_sha256
    assert not (tmp_path / "unexpected-traces").exists()
    for a, b in zip(first.runs, second.runs):
        assert a.model_dump(exclude={"report", "execution", "observations"}) == b.model_dump(exclude={"report", "execution", "observations"})
        assert a.report.model_dump(exclude={"generated_at"}) == b.report.model_dump(exclude={"generated_at"})
        assert a.execution.model_dump(exclude={"latency_ms"}) == b.execution.model_dump(exclude={"latency_ms"})
        assert a.task_completed and b.task_completed


def test_expected_tools_do_not_drive_the_script():
    raw = load_dataset().cases[0].model_dump()
    raw["expected_tools"] = ["calculate_pe"]
    run = asyncio.run(Evaluator().evaluate(_dataset_with(raw))).runs[0]
    assert run.actual_tools == ["get_stock_price"]
    assert run.tool_selection.f1 == 0
    assert not run.task_completed and "completion:expected_tools_executed" in run.failure_reasons


def test_duplicate_requests_are_separate_from_executed_attempts():
    raw = load_dataset().cases[0].model_dump()
    raw["script"].append(copy.deepcopy(raw["script"][0]))
    run = asyncio.run(Evaluator().evaluate(_dataset_with(raw))).runs[0]
    assert run.execution.requested_tool_count == 2
    assert run.execution.tool_call_count == run.execution.observation_count == 1
    assert run.tool_selection.f1 == 1 and run.task_completed


def _price_state():
    state = create_initial_state("Price", ticker="NVDA")
    state["status"] = "finished"
    state["observations"] = [{"tool": "get_stock_price", "args": {"ticker": "NVDA"},
                              "result": FixtureProvider().get_stock_price("NVDA").model_dump_json()}]
    state["final_output"] = _grounded_or_no_data_report(state, "NVDA")
    return state


@pytest.mark.parametrize("tamper", ["price", "prose", "source", "ticker", "state_ticker", "missing", "wrong_observation", "nonfinite"])
def test_evidence_rejects_unsupported_output(tamper):
    state = _price_state()
    report = state["final_output"]
    if tamper == "price":
        report.key_metrics["price"] = 999999
    elif tamper == "prose":
        report.conclusion += " The verified valuation is 999999."
    elif tamper == "source":
        report.key_metrics["data_sources"] = ["invented-source"]
    elif tamper == "ticker":
        report.ticker = "AMD"
    elif tamper == "state_ticker":
        state["ticker"] = "AMD"
    elif tamper == "missing":
        state["observations"] = []
    elif tamper == "wrong_observation":
        payload = json.loads(state["observations"][0]["result"])
        payload["ticker"] = "AMD"
        state["observations"][0]["result"] = json.dumps(payload)
    else:
        report.key_metrics["price"] = float("inf")
    assert not all(evidence_checks(state, "NVDA").values())


def test_safe_no_data_is_grounded_but_not_a_completed_price_task():
    state = create_initial_state("Price", ticker="NVDA")
    state["status"] = "finished"
    state["final_output"] = _grounded_or_no_data_report(state, "NVDA")
    evidence = evidence_checks(state, "NVDA")
    assert all(evidence.values())
    checks = completion_checks(load_dataset().cases[0], state, ["get_stock_price"], evidence)
    assert checks["valid_report"] and not checks["required_metric:price"]
    assert not all(checks.values())


def test_runner_failure_is_included_in_denominator_and_does_not_stop_batch():
    cases = load_dataset().cases[:2]

    def factory(ticker):
        if ticker == "NVDA":
            raise RuntimeError("fixture factory failure")
        return build_offline_agent(cases[1])

    report = asyncio.run(Evaluator(factory).evaluate(_dataset_with(*(case.model_dump() for case in cases))))
    assert report.runner_mode == "custom-factory-unverified"
    assert report.aggregate["task_completion_rate"] == 0.5
    assert report.aggregate["case_count"] == 2
    assert report.runs[0].execution.final_status == "error"
    assert report.runs[0].execution.latency_source == "runner_elapsed"
    assert report.runs[0].report is None and report.runs[0].failure_reasons
    assert report.runs[1].task_completed


def test_runner_propagates_cancellation():
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    factory = lambda ticker: SimpleNamespace(tracer=TraceCollector(), arun=cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Evaluator(factory).evaluate(load_dataset()))


def test_cli_outputs_json_in_chinese_without_loading_dotenv(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("The evaluation CLI must not load .env")

    monkeypatch.setattr("dotenv.load_dotenv", forbidden)
    monkeypatch.setattr("sys.argv", ["evaluate.py", "--lang", "zh"])
    asyncio.run(cli.main())
    report = json.loads(capsys.readouterr().out)
    assert report["language"] == "zh" and report["dataset_version"] == "stockmind-eval-v1"
    assert all(run["task_completed"] for run in report["runs"])
    assert "观测" in report["runs"][0]["report"]["summary"]

