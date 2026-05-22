"""Agent 核心 — 调用 Claude API，注入投资框架，执行扫描/深度分析/回顾.

模式:
  scan:  批量拉取成分股数据 → 横向对比 → 挑出值得深挖的
  deep:  单只个股深度分析 → 按框架逐项检查 → 形成投资论点
  review: 翻历史论点 → 对比新数据 → 标记偏差 → 建议框架调整
"""

import json
from typing import Optional

import anthropic

from .config import ANTHROPIC_KEY, load_config
from .framework import get_framework_prompt
from .memory import (
    save_thesis, get_thesis, save_scan, save_review,
    list_pending_reviews, save_learning,
)
from .tools.data import fetch_fundamentals, fetch_batch
from .tools.sector import find_sector_peers

SYSTEM_PROMPT = """你是基本面研究分析师 Agent。你的任务是基于财务数据和投资框架，做出独立、结构化的判断。

## 核心原则

- 你分析的是商业实体，不是股票代码。先理解商业模式，再看财务数据
- 数字背后是商业逻辑。毛利率下降3pp——是竞争加剧？成本结构恶化？产品组合变化？
- 不追求面面俱到。找到2-3个关键矛盾点，比列出20个指标更有价值
- 诚实标注"不知道"。数据不足以支撑判断时，明确说出来
- 结论必须有可验证的假设。每个论点附上"什么情况下这个判断是错的"

## 工作模式

### scan 模式 — 板块横向对比
输入一个板块/主题的成分股列表和数据，你需要：
1. 快速识别板块的共同特征和差异点
2. 用框架检查清单逐只过一遍（不需要逐只写长文，关键指标+一句话判断）
3. 输出对比矩阵
4. 挑出 3-5 只值得深度研究的，给出理由

### deep 模式 — 个股深度分析
输入单只股票的完整数据，你需要：
1. 阅读该股票的历史论点（如有）
2. 按框架做完整的五维分析
3. 形成明确的投资论点
4. 标注关键假设和验证指标
5. 给出"什么情况下需要修正判断"的具体条件

### review 模式 — 定期回顾
输入历史论点和最新数据，你需要：
1. 逐一比对当初的假设是否成立
2. 判断结论是否正确，为什么对/错
3. 提取可推广的经验教训
4. 如果框架漏掉了重要因素，建议修改框架

## 输出格式

scan 模式输出 Markdown 文档，包含：
- 板块概览（共同特征、核心矛盾）
- 对比矩阵表格（代码/名称/市值/核心指标/一句话判断/是否深挖）
- 重点标的（3-5只，每只有1-2句为什么值得深挖）

deep 模式输出 Markdown 文档，包含：
- 商业模式一句话总结
- 五维分析（业务质量 / 财务健康 / 增长动力 / 管理层信号 / 估值安全边际）
- 投资论点（核心论点 + 关键假设 + 验证指标）
- 风险与反证（什么情况下这个判断是错的）

---

{framework}

---

## 工具使用

你可以使用以下工具：
- fetch_data: 拉取单只或批量股票基本面数据
- save_thesis: 保存个股投资论点
- get_thesis: 读取历史论点
- save_scan: 保存扫描结果
- list_pending_reviews: 查看需要回顾的股票

开始分析时，使用获取到的数据进行独立判断。不要复述数据，要用数据支撑你的判断。
"""


def _build_system_prompt() -> str:
    framework_text = get_framework_prompt()
    return SYSTEM_PROMPT.format(framework=framework_text or "（框架文件尚未创建，请基于通用价值投资原则进行分析）")


def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── Tool definitions ──

TOOLS = [
    {
        "name": "fetch_data",
        "description": "拉取单只或多只股票的基本面数据。tickers 可以是单个 ticker 字符串或列表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "oneOf": [
                        {"type": "string", "description": "单个 ticker，如 'BABA.US'"},
                        {"type": "array", "items": {"type": "string"}, "description": "多个 tickers"},
                    ]
                },
            },
            "required": ["tickers"],
        },
    },
    {
        "name": "save_thesis",
        "description": "保存个股投资论点到记忆。content 应为完整的 Markdown 分析文档。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "content": {"type": "string", "description": "完整的投资论点 Markdown"},
            },
            "required": ["ticker", "content"],
        },
    },
    {
        "name": "get_thesis",
        "description": "读取某只股票的历史投资论点（如果存在）。",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "save_scan",
        "description": "保存板块横向扫描结果。",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "板块/主题名称，如 '固态电池'"},
                "content": {"type": "string", "description": "扫描结果 Markdown"},
            },
            "required": ["topic", "content"],
        },
    },
]


def _execute_tool(name: str, args: dict) -> str:
    if name == "fetch_data":
        tickers = args["tickers"]
        if isinstance(tickers, str):
            tickers = [tickers]
        if len(tickers) == 1:
            result = fetch_fundamentals(tickers[0])
            return json.dumps(result, ensure_ascii=False, default=str) if result else "数据获取失败"
        else:
            results = fetch_batch(tickers)
            # 返回精简版，避免 token 爆炸
            brief = []
            for r in results:
                brief.append({
                    "ticker": r["ticker"], "name": r["name"],
                    "sector": r["sector"], "market_cap": round(r["market_cap"] / 1e8, 2),
                    "revenue_cagr_3y": r["revenue_cagr_3y"],
                    "gross_margin": r["gross_margin"],
                    "net_margin": r["net_margin"],
                    "roe": r["roe"], "debt_ratio": r["debt_ratio"],
                    "pe": r["pe"], "div_yield": r["div_yield"],
                    "ocf_neg_3yr": r["ocf_neg_3yr"],
                    "ni_neg_2yr": r["ni_neg_2yr"],
                    "gm_trend": r["gm_trend"],
                })
            return json.dumps(brief, ensure_ascii=False, default=str)
    elif name == "save_thesis":
        save_thesis(args["ticker"], args["content"])
        return "论点已保存"
    elif name == "get_thesis":
        result = get_thesis(args["ticker"])
        return result or "无历史记录"
    elif name == "save_scan":
        path = save_scan(args["topic"], args["content"])
        return f"扫描结果已保存: {path}"
    else:
        return f"未知工具: {name}"


def _run_agent(user_prompt: str, mode_desc: str) -> str:
    """Agent 主循环：发送 prompt + 框架 → Claude → 处理工具调用 → 循环直到完成."""
    cfg = load_config()
    agent_cfg = cfg.get("agent", {})
    model = agent_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = agent_cfg.get("max_tokens", 64000)

    client = _make_client()
    system = _build_system_prompt()
    messages = [{"role": "user", "content": user_prompt}]

    print(f"[AGENT] {mode_desc} — 模型: {model}")
    print(f"[AGENT] 框架已注入 ({'有框架' if get_framework_prompt() else '无框架，使用通用原则'})")

    while True:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=TOOLS,
        )

        # 收集文本 + 工具调用
        text_blocks = []
        tool_blocks = []

        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
            elif block.type == "tool_use":
                tool_blocks.append(block)

        # 打印流式文本
        for t in text_blocks:
            print(t, end="")

        if not tool_blocks:
            # Agent 完成，返回所有文本
            return "\n".join(text_blocks)

        # 处理工具调用
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        for tb in tool_blocks:
            print(f"\n  [TOOL] {tb.name}(", end="")
            arg_preview = ", ".join(f"{k}={str(v)[:50]}..." for k, v in tb.input.items())
            print(f"{arg_preview})")
            result = _execute_tool(tb.name, tb.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tb.id,
                "content": result[:8000],  # 截断过长结果
            })

        messages.append({"role": "user", "content": tool_results})


# ── 三大模式 ──

def do_scan(topic: str, tickers: list[str] = None, output: str = None):
    """板块横向扫描.

    topic: 板块名称，如 "固态电池"
    tickers: 可选，手动指定成分股列表；不指定则由 Agent 自行搜索
    output: 输出路径，默认写入 memory/scans/
    """
    if tickers:
        ticker_list = "\n".join(f"  - {t}" for t in tickers)
        user_msg = f"""## scan 模式: 横向对比

板块: {topic}
股票列表:
{ticker_list}

请拉取以上股票的数据，进行横向对比分析。"""
    else:
        user_msg = f"""## scan 模式: 横向对比

板块: {topic}

请先搜索该板块的主要成分股，然后拉取数据做横向对比。
如果数据不足以覆盖全部成分股，优先分析市值最大的前10只。"""

    result = _run_agent(user_msg, f"scan: {topic}")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(result, encoding="utf-8")
        print(f"\n[OUTPUT] {output}")


def do_deep(ticker: str):
    """个股深度分析."""
    # 先拉数据
    print(f"[DATA] 拉取 {ticker}...")
    data = fetch_fundamentals(ticker)
    if not data:
        print(f"[ERROR] 无法获取 {ticker} 数据")
        return

    history = get_thesis(ticker)
    history_text = f"\n\n## 历史论点\n\n{history}" if history else "（首次分析，无历史记录）"

    user_msg = f"""## deep 模式: 个股深度分析

Ticker: {ticker}

### 当前基本面数据

```json
{json.dumps(data, ensure_ascii=False, default=str, indent=2)}
```

{history_text}

请按框架做完整的五维分析，形成投资论点。
分析完成后，调用 save_thesis 保存。"""

    _run_agent(user_msg, f"deep: {ticker}")


def do_review(days: int = 90):
    """定期回顾."""
    pending = list_pending_reviews(days)
    if not pending:
        print("[REVIEW] 没有需要回顾的股票")
        return

    print(f"[REVIEW] {len(pending)} 只股票需要回顾: {', '.join(pending[:10])}{'...' if len(pending) > 10 else ''}")

    ticker_list = "\n".join(f"  - {t}" for t in pending[:10])
    user_msg = f"""## review 模式: 定期回顾

以下股票距上次分析已超过 {days} 天，需要回顾:\n{ticker_list}

请逐一调取最新数据和历史论点，做对比分析:
1. 当初的假设是否成立？
2. 判断是否正确？为什么？
3. 有什么经验教训值得记录？
4. 框架是否需要调整？

分析完每只股票后，调用 save_thesis 更新论点。"""

    _run_agent(user_msg, f"review: {len(pending)} stocks")
