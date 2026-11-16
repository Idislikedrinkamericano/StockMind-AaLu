"""Offline checks for dependency imports, the async example, and Web mode copy."""

import ast
import asyncio
from pathlib import Path

import pytest

from app.agents.react_agent import StockMindAgent
from app.graph.checkpoint import build_checkpointer
from app.models.demo import build_demo_model
from examples import async_research


ROOT = Path(__file__).resolve().parents[1]


def test_sqlite_dependencies_and_backend_work():
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def check():
        async with build_checkpointer("sqlite", ":memory:") as saver:
            assert isinstance(saver, AsyncSqliteSaver)
            assert isinstance(saver.conn, aiosqlite.Connection)
            await saver.setup()
            async with saver.conn.execute("SELECT 1") as cursor:
                assert await cursor.fetchone() == (1,)

    asyncio.run(check())


@pytest.mark.parametrize("ticker,language", [("AMD", "en"), ("NVDA", "zh")])
def test_async_example_completes_two_real_scripted_runs(monkeypatch, tmp_path, capsys, ticker, language):
    completed = []

    class RecordingAgent(StockMindAgent):
        async def ainvoke(self, *args, **kwargs):
            report = await super().ainvoke(*args, **kwargs)
            snapshot = await self.aget_state(kwargs["thread_id"])
            completed.append((self, report, snapshot.values, kwargs["thread_id"]))
            return report

    def forbid_external_provider(*args, **kwargs):
        pytest.fail("The scripted example must explicitly select mock tools and a scripted model")

    monkeypatch.setattr(async_research, "StockMindAgent", RecordingAgent)
    monkeypatch.setattr(async_research, "load_dotenv", lambda: None)
    monkeypatch.setattr(async_research, "CHECKPOINT_DB", tmp_path / "checkpoints" / "demo.sqlite")
    monkeypatch.setattr("sys.argv", ["async_research.py", "--ticker", ticker, "--lang", language])
    monkeypatch.setattr("app.agents.react_agent.get_data_provider", forbid_external_provider)
    monkeypatch.setattr("app.agents.react_agent.get_chat_model", forbid_external_provider)
    monkeypatch.setenv("STOCKMIND_DATA_PROVIDER", "akshare")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    monkeypatch.setenv("STOCKMIND_TRACE_DIR", "")
    monkeypatch.setenv("STOCKMIND_MAX_ITERATIONS", "8")
    monkeypatch.setenv("STOCKMIND_LLM_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("STOCKMIND_RUN_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("STOCKMIND_TOOL_TIMEOUT_SECONDS", "15")
    asyncio.run(async_research.main())

    assert len(completed) == 2
    first, second = completed
    assert first[0].llm is not second[0].llm
    assert first[0].checkpointer is second[0].checkpointer
    assert first[3] == second[3]
    assert len(second[2]["messages"]) > len(first[2]["messages"])
    for agent, report, state, _ in completed:
        assert state["status"] == "finished"
        assert state["errors"] == []
        assert len(state["observations"]) == 15
        assert len([event for event in agent.tracer.events if event.event_type == "tool_call"]) == 15
        assert report.ticker == ticker
        assert report.key_metrics["price"] is not None
        assert report.key_metrics["pe"] is not None
        assert "mock" in report.key_metrics["data_sources"]
    output = capsys.readouterr().out
    assert "Scripted Demo: fixed tool sequence; mock data; no LLM provider calls." in output
    assert "[second scripted run] status=finished" in output


def test_scripted_model_remains_one_shot():
    model = build_demo_model("AMD")
    for _ in range(5):
        model._generate([])
    with pytest.raises(RuntimeError, match="ran out of scripted messages"):
        model._generate([])


@pytest.mark.parametrize("language", ["ZH", "EN"])
def test_web_copy_discloses_scripted_mode_without_importing_gradio(language):
    # Inspect the actual UI text bindings without starting Gradio or a server.
    tree = ast.parse((ROOT / "web" / "app.py").read_text())
    names = {"DISCLAIMER_ZH", "DISCLAIMER_EN", "DEMO_DESCRIPTION"}
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    namespace = {}
    exec(compile(ast.Module(body=assignments, type_ignores=[]), "web-copy", "exec"), namespace)
    disclaimer = namespace[f"DISCLAIMER_{language}"]
    assert "Scripted Demo" in disclaimer and "Mock" in disclaimer
    if language == "ZH":
        assert "固定工具序列" in disclaimer
        assert "没有真实 LLM 模式入口" in disclaimer
        assert "设置 API Key 不会切换模式" in disclaimer
    else:
        assert "fixed tool sequence" in disclaimer
        assert "no real LLM mode" in disclaimer
        assert "adding an API key does not switch modes" in disclaimer
    description = namespace["DEMO_DESCRIPTION"]
    assert disclaimer in description
    assert "examples/research.py" in description and "STOCKMIND_LLM_PROVIDER" in description
    assert "自主调用 18" not in description
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and isinstance(node.func.value, ast.Name) and node.func.value.id == "gr"
               and node.func.attr == "Markdown" and node.args
               and isinstance(node.args[0], ast.Name) and node.args[0].id == "DEMO_DESCRIPTION"
               for node in ast.walk(tree))
