#!/usr/bin/env python3
"""StockMind Web — Gradio demo UI.

Runs the agent with the scripted demo model and the configured data provider
(mock by default). The model uses a fixed tool sequence, with no LLM API calls.
The repository-root app.py launches this module's Gradio app.
"""
import asyncio
import json
import os
import tempfile

import gradio as gr

from app.agents.react_agent import StockMindAgent
from app.models.demo import build_demo_model
from app.observability.tracing import TraceCollector
from app.tools.providers import PriceBar
from app.visualization.charts import render_price_chart

NOT_COVERED = "Not covered in this phase."

SECTION_TITLES_ZH = {
    "market_analysis": "市场概况",
    "financial_analysis": "财务分析",
    "technical_analysis": "技术分析",
    "news_analysis": "新闻动态",
    "valuation_analysis": "估值分析",
    "risk_analysis": "风险提示",
}
SECTION_TITLES_EN = {
    "market_analysis": "Market",
    "financial_analysis": "Financials",
    "technical_analysis": "Technical",
    "news_analysis": "News",
    "valuation_analysis": "Valuation",
    "risk_analysis": "Risk",
}

DISCLAIMER_ZH = (
    "> ⚠️ 当前为 **Scripted Demo**，使用固定工具序列，不根据问题自主选择工具。"
    "数据默认来自 **Mock 模拟数据**，并非实时行情；若管理员更改 Provider，以报告来源为准。"
    "本页面没有真实 LLM 模式入口，设置 API Key 不会切换模式。不构成投资建议。"
)
DISCLAIMER_EN = (
    "> ⚠️ This is a **Scripted Demo** with a fixed tool sequence, not autonomous tool selection. "
    "Data defaults to **Mock simulated data**, not live quotes; check report sources if the administrator changes the provider. "
    "This page has no real LLM mode; adding an API key does not switch modes. Not investment advice."
)

DEMO_DESCRIPTION = (
    "# 📈 StockMind — 股票研究流程演示 / Scripted Demo\n\n"
    "通过 LangGraph 执行固定工具序列，再根据工具观测生成报告。\n\n"
    "Runs a fixed tool sequence through LangGraph and assembles reports from tool observations.\n\n"
    + DISCLAIMER_ZH + "\n\n" + DISCLAIMER_EN + "\n\n"
    "真实 LLM Agent 请使用 `python examples/research.py`，配置所选 Provider 的 API Key、"
    "`STOCKMIND_LLM_PROVIDER` 和相应模型；可能产生 Provider 费用。数据仍默认 Mock。\n\n"
    "For the real LLM agent, use `python examples/research.py` with the selected provider's API key, "
    "`STOCKMIND_LLM_PROVIDER`, and a matching model. Provider charges may apply; data still defaults to Mock."
)


class StreamingTracer(TraceCollector):
    """TraceCollector subclass that forwards every event into an asyncio queue
    for live UI streaming. Zero changes to app/* required."""

    def __init__(self, queue: "asyncio.Queue", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queue = queue
        self.history_payload = None  # raw JSON of get_historical_prices output

    def record(self, *args, **kwargs):
        super().record(*args, **kwargs)
        try:
            etype = args[0] if args else kwargs.get("event_type", "?")
            if etype == "tool_call" and kwargs.get("tool_name") == "get_historical_prices":
                self.history_payload = kwargs.get("tool_output")
            self._queue.put_nowait((etype, kwargs))
        except Exception:
            pass


def describe_event(etype, kw, lang):
    """One-line human-readable summary of a trace event for the progress log."""
    zh = lang == "zh"
    name = kw.get("tool_name", "?")
    lat = kw.get("latency_ms")
    lat_s = f"（{lat / 1000:.1f}s）" if isinstance(lat, (int, float)) else ""
    err = kw.get("error")
    if etype == "run_start":
        return "🚀 开始运行" if zh else "🚀 Run started"
    if etype == "agent_step":
        n = kw.get("agent_step", "?")
        base = f"🧠 推理步骤 {n}" if zh else f"🧠 Reasoning step {n}"
        return base + (" ⚠️ 出错" if err else "")
    if etype == "tool_call":
        base = f"🔧 调用工具 `{name}`{lat_s}" if zh else f"🔧 Tool `{name}`{lat_s}"
        return base + (" ⚠️ 出错" if err else "")
    if etype == "finalize":
        return "📝 生成研报" if zh else "📝 Finalizing report"
    if etype == "finalize_fallback":
        return "📝 生成研报（确定性组装）" if zh else "📝 Finalizing report (deterministic assembly)"
    if etype == "run_end":
        status = kw.get("final_status", kw.get("status", ""))
        ok = "✅ 完成" if zh else "✅ Done"
        return f"{ok}（{status}）" if status else ok
    return None


def safe_str(v):
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def report_to_markdown(report, lang):
    zh = lang == "zh"
    titles = SECTION_TITLES_ZH if zh else SECTION_TITLES_EN
    L = []
    disc = DISCLAIMER_ZH if zh else DISCLAIMER_EN
    L.append(disc)
    L.append("")
    L.append(f"# {report.ticker} " + ("研究报告" if zh else "Research Report"))
    if getattr(report, "generated_at", None):
        L.append(("_生成时间_" if zh else "_Generated at_") + f"：{report.generated_at}")
    L.append("")
    if report.summary:
        L.append(("## 摘要" if zh else "## Summary") + "\n" + report.summary + "")
    for key, title in titles.items():
        text = getattr(report, key, "") or ""
        if text and text.strip() != NOT_COVERED:
            L.append(f"## {title}\n{text}")
    def bullets(title, items):
        items = [i for i in (items or []) if i]
        if items:
            L.append(f"## {title}\n" + "\n".join(f"- {i}" for i in items))
    if zh:
        bullets("看多理由", report.bull_case)
        bullets("看空理由", report.bear_case)
        bullets("不确定性", report.uncertainty)
        if report.conclusion:
            L.append("## 结论\n" + report.conclusion)
    else:
        bullets("Bull case", report.bull_case)
        bullets("Bear case", report.bear_case)
        bullets("Uncertainties", report.uncertainty)
        if report.conclusion:
            L.append("## Conclusion\n" + report.conclusion)
    return "\n\n".join(L)


def metrics_to_markdown(report, lang):
    zh = lang == "zh"
    km = getattr(report, "key_metrics", None) or {}
    if not km:
        return ""
    title = "## 关键指标" if zh else "## Key Metrics"
    rows = [title, "", "| 指标 Metric | 数值 Value |", "| --- | --- |"]
    for k, v in km.items():
        rows.append(f"| `{k}` | {safe_str(v)} |")
    return "\n".join(rows)


def build_chart(ticker, tracer, lang):
    """Render the price chart from the historical-bars tool observation."""
    payload = tracer.history_payload
    if not payload:
        return None
    try:
        data = json.loads(payload)
        bars = [PriceBar(**b) for b in data.get("bars", [])]
        note = data.get("note") or "simulated data"
    except Exception:
        return None
    if not bars:
        return None
    outdir = tempfile.mkdtemp(prefix="stockmind_charts_")
    path = os.path.join(outdir, f"{ticker}.png")
    try:
        return str(render_price_chart(bars, ticker, path, language=lang, watermark=note))
    except Exception:
        return None


async def run_research(ticker, lang, max_iter):
    """Async generator handler: streams progress, then yields final outputs."""
    zh = lang == "zh"
    ticker = (ticker or "").strip().upper()
    if not ticker:
        msg = "请输入 ticker，例如 NVDA" if zh else "Please enter a ticker, e.g. NVDA"
        yield msg, "", "", None
        return

    os.environ["STOCKMIND_MAX_ITERATIONS"] = str(int(max_iter))
    queue: asyncio.Queue = asyncio.Queue()
    tracer = StreamingTracer(queue)
    agent = StockMindAgent(
        llm=build_demo_model(ticker, lang),
        language=lang,
        tracer=tracer,
    )
    query = f"请对 {ticker} 做一份完整研究报告" if zh else f"Produce a full research report on {ticker}"
    log = [f"🚀 开始分析 {ticker}…" if zh else f"🚀 Analyzing {ticker}…"]
    task = asyncio.create_task(agent.ainvoke(query, ticker=ticker))
    try:
        while not task.done():
            try:
                etype, kw = await asyncio.wait_for(queue.get(), timeout=1.0)
                line = describe_event(etype, kw, lang)
                if line:
                    log.append(line)
                    log = log[-40:]
            except asyncio.TimeoutError:
                pass
            yield "\n".join(log), "", "", None
        report = await task
    except Exception as e:  # noqa: BLE001 — surface honest errors in UI
        log.append(f"❌ 运行出错：{e}" if zh else f"❌ Error: {e}")
        yield "\n".join(log), "", "", None
        return

    # Drain any remaining events for a complete log.
    while not queue.empty():
        try:
            etype, kw = queue.get_nowait()
            line = describe_event(etype, kw, lang)
            if line:
                log.append(line)
        except asyncio.QueueEmpty:
            break

    chart_path = build_chart(ticker, tracer, lang)
    report_md = report_to_markdown(report, lang)
    metrics_md = metrics_to_markdown(report, lang)
    done = "✅ 研报生成完毕" if zh else "✅ Report ready"
    log.append(done)
    yield "\n".join(log[-40:]), report_md, metrics_md, chart_path


with gr.Blocks(title="StockMind — Scripted Research Demo") as demo:
    gr.Markdown(DEMO_DESCRIPTION)
    with gr.Row():
        ticker_in = gr.Textbox(label="Ticker 股票代码", value="NVDA", max_lines=1)
        lang_in = gr.Radio(choices=[("中文", "zh"), ("English", "en")],
                           value="zh", label="报告语言 Language")
        iter_in = gr.Slider(4, 8, value=8, step=1, label="最大推理步数 Max iterations")
    run_btn = gr.Button("🚀 生成研报 Generate Report", variant="primary")
    progress_out = gr.Markdown(label="运行进度")
    with gr.Row():
        with gr.Column(scale=3):
            report_out = gr.Markdown(label="研究报告")
        with gr.Column(scale=2):
            metrics_out = gr.Markdown(label="关键指标")
    chart_out = gr.Image(label="价格走势 Price Chart")
    gr.Markdown("---\nBuilt with LangGraph · 工具：市场/财务/新闻/技术/估值共 18 个 · "
               "[GitHub](https://github.com/Idislikedrinkamericano/StockMind-AaLu)")

    run_btn.click(
        fn=run_research,
        inputs=[ticker_in, lang_in, iter_in],
        outputs=[progress_out, report_out, metrics_out, chart_out],
    )
    demo.queue(max_size=8)
