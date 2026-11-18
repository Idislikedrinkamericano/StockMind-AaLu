"""Real-world smoke entry. Default: offline preflight. --execute-real permits paid requests."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError
from app.evaluation.real_smoke import (
    SmokeCase, SmokeConfig, _redact, create_output_directory, default_cases, run_case, save_result,
)


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-real", action="store_true",
                        help="Explicitly authorize real LLM/data requests; provider charges may apply.")
    parser.add_argument("--case", choices=["all", *[c.case_id for c in default_cases()]], default="all")
    parser.add_argument("--ticker")
    parser.add_argument("--query")
    parser.add_argument("--provider", choices=["yfinance", "akshare"])
    parser.add_argument("--llm-provider", choices=["openai", "anthropic"])
    parser.add_argument("--model")
    parser.add_argument("--deadline", type=float, default=60)
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--output-dir", type=Path, default=Path("smoke-results"))
    args = parser.parse_args(argv)
    try:
        config = SmokeConfig.from_environment(**{
            k: v for k, v in vars(args).items() if k in SmokeConfig.model_fields and v is not None})
        cases = default_cases()
        custom = any((args.ticker, args.query, args.provider))
        if custom:
            if not all((args.ticker, args.query, args.provider)) or args.case != "all":
                parser.error("Custom runs require --ticker, --query and --provider, without --case.")
            cases = [SmokeCase(case_id="custom", ticker=args.ticker.strip().upper(), query=args.query,
                               financial_provider=args.provider,
                               required_tool_groups=[["get_stock_price"], ["get_historical_prices"]])]
        elif args.case != "all":
            cases = [case for case in cases if case.case_id == args.case]
    except ValidationError:
        # Pydantic input dumps may contain user text. Keep terminal errors minimal.
        parser.error("Invalid smoke configuration; check provider, ticker and positive bounded limits.")
    try:
        directory = create_output_directory(args.output_dir)
    except (OSError, ValueError):
        parser.error("Output directory unavailable or unsafe.")
    plan = {"mode": "real-requests-enabled" if args.execute_real else "offline-preflight",
            "cases": [case.model_dump() for case in cases], "limits": config.model_dump(),
            "llm_request_upper_bound": len(cases) * config.max_iterations,
            "sdk_retries": 0, "tool_retries": 0,
            "cost": "Unknown: endpoint tariffs, input context and tool fan-out are not known. "
                    "Output-token limits are per call, not a monetary cap."}
    print(json.dumps(_redact(plan), ensure_ascii=False, indent=2), flush=True)
    outcomes = []
    for case in cases:
        result = await run_case(case, config, execute_real=args.execute_real)
        path = save_result(result, directory)
        outcomes.append(result["outcome"])
        print(f'{case.case_id}: {result["outcome"]} -> {path}', flush=True)
    return 1 if "fail" in outcomes else 2 if "blocked" in outcomes else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
