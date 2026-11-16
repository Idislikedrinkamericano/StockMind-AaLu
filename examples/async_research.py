"""Async invocation and SQLite checkpoint demo.

Run:

    python examples/async_research.py --ticker NVDA --lang en

Demonstrates two scripted research runs with a shared SQLite ``thread_id``.
Each run gets a fresh one-shot model and uses explicit mock tools, regardless
of API keys or provider settings. Checkpoint messages persist between runs;
this does not demonstrate autonomous follow-up reasoning or interrupted-run resume.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from app.agents.react_agent import StockMindAgent
from app.graph.checkpoint import build_checkpointer
from app.models.demo import build_demo_model
from app.tools import build_tools
from app.tools.providers import MockMarketDataProvider
from app.utils import format_local_time

CHECKPOINT_DB = Path(__file__).resolve().parents[1] / "checkpoints" / "checkpoints.sqlite"


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Async + checkpoint demo")
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--lang", choices=["en", "zh"], default=os.getenv("STOCKMIND_LANGUAGE", "en"))
    args = parser.parse_args()
    ticker = args.ticker.upper()
    demo_tools = build_tools(MockMarketDataProvider())
    print("Scripted Demo: fixed tool sequence; mock data; no LLM provider calls.")

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with build_checkpointer("sqlite", str(CHECKPOINT_DB)) as saver:
        agent = StockMindAgent(
            llm=build_demo_model(ticker, args.lang),
            tools=demo_tools,
            language=args.lang,
            checkpointer=saver,
        )

        thread_id = f"{ticker.lower()}-run"
        report = await agent.ainvoke(
            f"Give me a research snapshot of {ticker}.", ticker=ticker, thread_id=thread_id
        )
        print(f"[first run] {report.ticker} generated_at={format_local_time(report.generated_at)}")
        print(f"[first run] summary: {report.summary[:120]}...")

        snapshot = await agent.aget_state(thread_id)
        print(f"[checkpoint] status={snapshot.values['status']}  messages={len(snapshot.values['messages'])}")

        # A new model owns a fresh iterator; the saver/thread retain checkpoint history.
        agent = StockMindAgent(
            llm=build_demo_model(ticker, args.lang),
            tools=demo_tools,
            language=args.lang,
            checkpointer=saver,
        )
        await agent.ainvoke(
            f"Anything else worth flagging for {ticker}?", ticker=ticker, thread_id=thread_id
        )
        snapshot2 = await agent.aget_state(thread_id)
        print(f"[second scripted run] status={snapshot2.values['status']}  messages={len(snapshot2.values['messages'])}")


if __name__ == "__main__":
    asyncio.run(main())
