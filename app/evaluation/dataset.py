"""Versioned offline cases; tool names and arguments use the production registry."""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools import build_tools
from app.tools.providers import MockMarketDataProvider

DATASET_PATH = Path(__file__).parent / "datasets" / "questions.json"
FIXTURE_PATH = DATASET_PATH.with_name("fixtures-v1.json")
Scenario = Literal["standard", "missing_financials", "news_failure", "price_unavailable",
                   "wrong_tool_ticker", "wrong_observation_ticker"]
Section = Literal["market_analysis", "financial_analysis", "technical_analysis",
                  "valuation_analysis", "news_analysis", "risk_analysis"]
Metric = Literal["price", "change", "revenue", "net_income", "ebitda", "eps",
                 "free_cash_flow", "total_debt", "cash", "pe", "ev_ebitda", "fcf_yield",
                 "rsi", "annualized_volatility", "max_drawdown", "dcf_intrinsic_value"]


class DatasetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OutputConstraints(DatasetModel):
    report_mode: Literal["grounded", "no_data"]
    required_metrics: list[Metric] = Field(default_factory=list)
    absent_metrics: list[Metric] = Field(default_factory=list)
    expected_values: dict[Metric, float] = Field(default_factory=dict)
    available_sections: list[Section] = Field(default_factory=list)
    unavailable_sections: list[Section] = Field(default_factory=list)
    expected_error_count: int = Field(default=0, ge=0, strict=True)
    error_contains: list[str] = Field(default_factory=list)
    forbidden_text: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_constraints(self):
        if set(self.required_metrics) & set(self.absent_metrics):
            raise ValueError("A metric cannot be both required and absent")
        if set(self.expected_values) & set(self.absent_metrics):
            raise ValueError("An absent metric cannot have an expected value")
        if set(self.available_sections) & set(self.unavailable_sections):
            raise ValueError("Conflicting section constraints")
        if self.report_mode == "no_data" and (self.required_metrics or self.expected_values or self.available_sections):
            raise ValueError("No-data cases cannot require supported metrics or sections")
        if self.report_mode == "grounded" and not (self.required_metrics or self.expected_values or self.available_sections):
            raise ValueError("Grounded cases need a verifiable output requirement")
        if self.error_contains and not self.expected_error_count:
            raise ValueError("Expected error text requires a nonzero error count")
        return self


class ScriptCall(DatasetModel):
    name: str
    args: dict[str, Any]


class EvaluationCase(DatasetModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    question: str = Field(min_length=1)
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]*$")
    scenario: Scenario
    expected_tools: list[str]
    constraints: OutputConstraints
    script: list[ScriptCall] = Field(max_length=6)
    final_text: str = "Evidence collection complete."

    @model_validator(mode="after")
    def valid_tools(self):
        if not self.question.strip():
            raise ValueError("The question must not be blank")
        registry = {tool.name: tool for tool in build_tools(MockMarketDataProvider())}
        if len(set(self.expected_tools)) != len(self.expected_tools):
            raise ValueError("Expected tools must be unique")
        if set(self.expected_tools) - registry.keys():
            raise ValueError("Unknown expected tool")
        for call in self.script:
            if call.name not in registry:
                raise ValueError(f"Unknown scripted tool: {call.name}")
            schema = registry[call.name].args_schema
            if set(call.args) - schema.model_fields.keys():
                raise ValueError(f"Unknown arguments for {call.name}")
            schema.model_validate(call.args)
            requested = call.args.get("ticker", self.ticker)
            if requested != self.ticker and self.scenario != "wrong_tool_ticker":
                raise ValueError("Cross-ticker scripts require an explicit adversarial scenario")
        return self


class EvaluationDataset(DatasetModel):
    version: Literal["stockmind-eval-v1"]
    data_kind: Literal["synthetic-fixture"]
    fixture_version: Literal["synthetic-v1"]
    description: str
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        if fixtures["version"] != self.fixture_version:
            raise ValueError("Fixture version mismatch")
        if any(case.ticker not in fixtures["symbols"] for case in self.cases):
            raise ValueError("Dataset ticker has no versioned fixture")
        return self


def load_dataset(path: str | Path = DATASET_PATH) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))
