"""Run offline evaluation or the combined Benchmark; never auto-enable a provider.

    python examples/evaluate.py --output /tmp/stockmind-evaluation.json
    python examples/evaluate.py --suite benchmark --output-dir benchmark-results

Single suites without --output print JSON. Benchmark writes JSON + Markdown. No .env loading.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation import Evaluator, load_dataset
from app.evaluation.benchmark import DEFAULT_OUTPUT_DIR, run_benchmark, write_benchmark
from app.evaluation.dataset import DATASET_PATH
from app.evaluation.reliability import ReliabilityEvaluator
from app.evaluation.reliability_dataset import RELIABILITY_PATH, load_reliability_dataset


async def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Scripted/Fixture evaluation; not real-LLM performance")
    parser.add_argument("--lang", choices=["en", "zh"], default="en")
    parser.add_argument("--suite", choices=["baseline", "reliability", "benchmark"], default="baseline")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path, help="Write JSON here; otherwise print JSON to stdout")
    parser.add_argument("--output-dir", type=Path, help="Benchmark artifacts directory (default: benchmark-results)")
    args = parser.parse_args()
    if args.suite == "benchmark":
        if args.dataset or args.output:
            parser.error("Benchmark uses both versioned datasets and --output-dir, not --dataset/--output")
        result = await run_benchmark(language=args.lang)
        paths = write_benchmark(result, args.output_dir or DEFAULT_OUTPUT_DIR)
        print(f"{result.benchmark_version}: {result.status}", file=sys.stderr)
        for path in paths:
            print(path)
        return
    if args.output_dir:
        parser.error("--output-dir is only supported with --suite benchmark")
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

