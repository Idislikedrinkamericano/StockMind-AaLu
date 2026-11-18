"""Run the offline Scripted/Fixture baseline; never auto-enable a provider.

    python examples/evaluate.py --output /tmp/stockmind-evaluation.json

Without --output, stdout contains only JSON. Does not load .env.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation import Evaluator, load_dataset
from app.evaluation.dataset import DATASET_PATH
from app.evaluation.reliability import ReliabilityEvaluator
from app.evaluation.reliability_dataset import RELIABILITY_PATH, load_reliability_dataset


async def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Scripted/Fixture evaluation; not real-LLM performance")
    parser.add_argument("--lang", choices=["en", "zh"], default="en")
    parser.add_argument("--suite", choices=["baseline", "reliability"], default="baseline")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path, help="Write JSON here; otherwise print JSON to stdout")
    args = parser.parse_args()
    if args.suite == "reliability":
        result = await ReliabilityEvaluator(language=args.lang).evaluate(load_reliability_dataset(args.dataset or RELIABILITY_PATH))
    else:
        result = await Evaluator(language=args.lang).evaluate(load_dataset(args.dataset or DATASET_PATH))
    output = result.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
        print(result.summary(), file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    asyncio.run(main())

