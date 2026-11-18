"""Reliability labels are separate from validated, executable baseline scripts."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from app.evaluation.dataset import DatasetModel, EvaluationCase, EvaluationDataset
from app.tools import build_tools
from app.tools.providers import MockMarketDataProvider

RELIABILITY_PATH = Path(__file__).parent / "datasets" / "reliability-v1.json"
FailureKind = Literal["provider_exception", "tool_timeout", "invalid_arguments", "wrong_ticker",
                      "duplicate_batch", "unknown_tool", "llm_exception", "llm_timeout",
                      "wrong_observation", "missing_data", "checkpoint_pause", "checkpoint_terminal_replay"]


class FailureInjection(DatasetModel):
    kind: FailureKind
    step: int = Field(default=1, ge=1, le=7)
    tool: str = "get_stock_price"
    replacement_args: dict[str, Any] = Field(default_factory=dict)


class ExpectedBehavior(DatasetModel):
    terminal_status: Literal["finished", "timeout", "error"]
    handling: Literal["normal_completion", "safe_degradation", "controlled_failure"]
    description: str = Field(min_length=1)
    safe_termination_required: bool = True
    recovery: Literal["none", "retry", "subsequent_tool", "partial_report", "checkpoint"] = "none"
    tool_attempts: int = Field(ge=0)


class ReliabilityCase(EvaluationCase):
    # Scripts remain valid under P2-A validation. Invalid requests are introduced
    # only by a declared injection after dataset validation, at the model boundary.
    scenario: Literal["reliability"] = "reliability"
    failure: FailureInjection
    expected_behavior: ExpectedBehavior

    @model_validator(mode="after")
    def valid_injection(self):
        registry = {tool.name: tool for tool in build_tools(MockMarketDataProvider())}
        fault = self.failure
        if fault.tool not in registry:
            raise ValueError("Injection must target an existing tool")
        if fault.kind.startswith("llm_") or fault.kind.startswith("checkpoint_"):
            if fault.step > len(self.script) + 1:
                raise ValueError("LLM injection step is beyond the script")
        else:
            if fault.step > len(self.script) or self.script[fault.step - 1].name != fault.tool:
                raise ValueError("Injection step/tool must match the validated script")
        if fault.kind == "invalid_arguments":
            call = self.script[fault.step - 1]
            try:
                registry[fault.tool].args_schema.model_validate({**call.args, **fault.replacement_args})
            except ValidationError:
                pass
            else:
                raise ValueError("Invalid-arguments injection must actually fail the tool schema")
        elif fault.kind == "wrong_ticker":
            if not fault.replacement_args.get("ticker") or fault.replacement_args["ticker"] == self.ticker:
                raise ValueError("Wrong-ticker injection needs a different ticker")
        elif fault.replacement_args:
            raise ValueError("replacement_args is only used by argument/ticker injections")
        if fault.kind.startswith("checkpoint_") and len(self.script) != 1:
            raise ValueError("Checkpoint cases use one initial tool request")
        if fault.kind == "checkpoint_terminal_replay" and fault.step != 2:
            raise ValueError("Terminal checkpoint probe times out after the first tool")
        if fault.kind == "provider_exception" and fault.tool != "get_stock_price":
            raise ValueError("Provider exception injection targets the fixture price method")
        if fault.kind in {"provider_exception", "tool_timeout", "wrong_observation", "missing_data"}:
            if any(call.name == fault.tool for call in self.script[:fault.step - 1]):
                raise ValueError("Tool-boundary injection targets the first invocation of that tool")
        return self


class ReliabilityDataset(EvaluationDataset):
    version: Literal["stockmind-reliability-v1"]
    cases: list[ReliabilityCase] = Field(min_length=1)


def load_reliability_dataset(path: str | Path = RELIABILITY_PATH) -> ReliabilityDataset:
    return ReliabilityDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))
