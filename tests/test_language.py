from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agents.prompts import get_system_prompt
from app.agents.react_agent import StockMindAgent
from app.graph.state import create_initial_state


def test_language_in_state():
    state = create_initial_state("查询", language="zh")
    assert state["language"] == "zh"


def test_chinese_system_prompt():
    prompt = get_system_prompt("zh")
    assert "简体中文" in prompt


def test_chinese_finalize_without_evidence_returns_localized_no_data_report():
    agent = StockMindAgent(
        llm=GenericFakeChatModel(messages=iter([AIMessage(content="价格 999999，建议买入")])),
        tools=[],
        language="zh",
    )
    report = agent.invoke("查询 NVDA", ticker="NVDA")
    assert "未收集到任何可验证的数据" in report.summary
    assert report.ticker == "NVDA"
    assert report.key_metrics == {}
    assert "999999" not in report.model_dump_json()


def test_unknown_language_falls_back_to_english():
    assert get_system_prompt("xx") == get_system_prompt("en")

