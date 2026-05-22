"""Agent 核心 v2 — 三层框架 + 预测追踪 + 渐进式加载.

模式:
  scan:  板块横向对比 → 输出对比矩阵 + 重点标的 + 更新板块知识
  deep:  个股深度分析 → 五维分析 + 预测注册表 + 写入论点
  review: 拉历史预测 → 比对最新数据 → 判定 → 沉淀模式
  evolve: 分析偏差模式 → 建议框架更新 → 等你确认
"""

import json
from pathlib import Path
from typing import Optional

import anthropic

from .config import ANTHROPIC_KEY, load_config
from .memory import MemoryStore, PredictionRegistry, EvolutionEngine
from .memory.store import PROJECT_DIR
from .tools.data import fetch_fundamentals, fetch_batch
from .tools.sector import find_sector_peers, list_sectors
from .framework import get_framework_prompt

# ── 全局实例 ──
store = MemoryStore()
predictions = PredictionRegistry(store)
evolution = EvolutionEngine(store)

FRAMEWORK_DIR = PROJECT_DIR / "memory" / "framework"

# ── 渐进式框架加载 ──
def _load_framework_summary() -> str:
    """加载框架摘要（~500 tokens），每次对话都注入。"""
    parts = []
    for name in ["checklist", "red-flags", "valuation"]:
        path = FRAMEWORK_DIR / f"{name}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            # 只取标题和关键条目，不加载全文
            lines = content.split("\n")
            summary_lines = []
            for line in lines:
                if line.startswith("##") or line.startswith("- [ ]"):
                    summary_lines.append(line)
            parts.append(f"### {name}\n" + "\n".join(summary_lines[:20]))
    return "\n\n".join(parts) if parts else "（框架文件未初始化，请运行 fa init）"


def _load_sector_context(sector: str) -> str:
    """按需加载板块知识。"""
    kb = store.get_sector_knowledge(sector)
    if not kb:
        return ""
    parts = [f"## {sector} 板块已有知识\n"]
    if kb.get("characteristics"):
        parts.append(f"**行业特征:** {kb['characteristics']}")
    if kb.get("key_drivers"):
        drivers = json.loads(kb["key_drivers"]) if isinstance(kb["key_drivers"], str) else kb["key_drivers"]
        parts.append(f"**关键驱动:** {', '.join(drivers)}")
    if kb.get("common_risks"):
        risks = json.loads(kb["common_risks"]) if isinstance(kb["common_risks"], str) else kb["common_risks"]
        parts.append(f"**常见风险:** {', '.join(risks)}")
    return "\n".join(parts)


def _load_ticker_context(ticker: str) -> str:
    """加载个股历史论点和回顾记录。"""
    thesis = store.get_thesis(ticker)
    if not thesis:
        return "（首次分析，无历史记录）"

    parts = [f"## {ticker} 历史分析\n"]
    parts.append(f"上次分析: {thesis['updated_at']}")
    parts.append(f"核心论点: {thesis['thesis'][:500]}")

    # 历史预测
    preds = json.loads(thesis["predictions"]) if isinstance(thesis["predictions"], str) and thesis["predictions"] else []
    if preds:
        parts.append("\n### 上次预测（需验证）\n")
        for i, p in enumerate(preds):
            parts.append(f"  {i+1}. {p.get('prediction', '')} (截止: {p.get('deadline', '?')})")

    # 回顾记录
    reviews = store.get_reviews(ticker)
    if reviews:
        parts.append(f"\n### 已进行 {len(reviews)} 次回顾")
        latest = reviews[0]
        parts.append(f"最近回顾: {latest['reviewed_at']}")

    return "\n".join(parts)


def _load_global_context() -> str:
    """加载全局状态：仪表盘 + 模式摘要。"""
    dash = store.dashboard()
    parts = ["## 当前状态\n"]
    parts.append(f"活跃论点: {dash['active_theses']} | 待回顾: {dash['reviews_due']}")
    parts.append(f"预测准确率: {dash['prediction_accuracy']}")
    parts.append(f"板块知识: {dash['sectors_known']} 个 | 模式: {dash['patterns_found']} 个")

    patterns = store.get_patterns()
    if patterns:
        parts.append("\n### 已发现的模式\n")
        for p in patterns[:5]:
            parts.append(f"  - [{p['category']}] {p['name']}: {p['description'][:100]}")

    return "\n".join(parts)


# ── System Prompt ──
SYSTEM_PROMPT_V2 = """你是基本面研究分析师 Agent。你的判断框架由三层知识支撑，每层有不同权重。

## 核心原则

1. 先理解商业，再看数字。毛利率下降3pp — 竞争？成本？产品组合变化？
2. 找2-3个关键矛盾点，比列出20个指标有用。
3. 诚实标注"不知道"。数据不足以支撑判断时，明确说。
4. **每个结论必须有可验证的假设。** 无法验证的判断不是判断。

## 输出格式（必须严格遵守）

### deep 模式输出结构:

```
# {ticker} — 深度投资分析

## 一、商业模式一句话总结

## 二、五维分析
（业务质量 / 财务健康 / 增长动力 / 管理层信号 / 估值安全边际）
每个维度结束时写一句明确判断。

## 三、投资论点

核心论点（2-3句）
三大支柱（每支柱附证据）

## 四、预测注册（必填，这是整个分析最重要的部分）

| # | 预测 | 验证指标 | 截止日期 | 置信度 |
|---|------|----------|----------|--------|
| 1 | 具体、可验证的预测 | 精确的量化指标 | 明确的时间 | 高/中/低 |

要求：至少3条预测，至少2条可量化，最多12个月时限。

## 五、反证条件
什么情况下核心论点被证伪？列出具体触发条件。

## 六、风险信号检查
逐一检查框架中的红旗信号，注明是否存在。
```

### scan 模式输出结构:
1. 板块概览（共同特征、核心矛盾）
2. 对比矩阵（表格）
3. 重点标的（3-5只，每只1-2句）
4. 板块知识更新（此板块的关键特征，供后续分析复用）

---

{framework_summary}

---

{global_context}

---

{sector_context}
"""


def _build_system_prompt(mode: str, sector: str = None, ticker: str = None) -> str:
    """渐进式构建系统提示词 — 按模式加载相关内容。"""
    framework_summary = _load_framework_summary()
    global_context = _load_global_context()
    sector_context = ""

    if sector and mode == "scan":
        sector_context = _load_sector_context(sector)

    # deep 模式需要完整框架而非摘要
    if mode == "deep":
        full_framework = get_framework_prompt()
        framework_summary = full_framework if full_framework else framework_summary
        if ticker:
            ticker_history = _load_ticker_context(ticker)
            global_context += "\n\n" + ticker_history

    # 用 replace 而非 format，避免框架内容中的花括号冲突
    prompt = SYSTEM_PROMPT_V2
    prompt = prompt.replace("{framework_summary}", framework_summary)
    prompt = prompt.replace("{global_context}", global_context)
    prompt = prompt.replace("{sector_context}", sector_context)
    return prompt


# ── Anthropic 客户端 ──
def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── 工具定义 ──
TOOLS_V2 = [
    {
        "name": "fetch_data",
        "description": "拉取单只或多只股票的基本面数据（含行业基准上下文）",
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
            },
            "required": ["tickers"],
        },
    },
    {
        "name": "save_thesis",
        "description": "保存个股投资论点。predictions 必须是 JSON 列表，格式: [{'prediction':'...','metric':'...','deadline':'YYYY-QX'}]",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "thesis": {"type": "string", "description": "核心论点（纯文本）"},
                "assumptions": {"type": "string", "description": "JSON: [{'assumption':'...','validation':'...','deadline':'...'}]"},
                "predictions": {"type": "string", "description": "JSON: [{'prediction':'...','metric':'毛利率','expected':'> 56','deadline':'2026-Q3','confidence':'高'}]"},
                "risk_flags": {"type": "string", "description": "JSON: ['发现的风险信号1','风险信号2']"},
                "key_metrics": {"type": "string", "description": "JSON: {'pe':15.2,'roe':21,...} 分析时的关键指标快照"},
            },
            "required": ["ticker", "thesis", "predictions"],
        },
    },
    {
        "name": "save_sector_knowledge",
        "description": "保存板块分析后的行业知识（scan 模式用）",
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string"},
                "characteristics": {"type": "string", "description": "行业特征描述"},
                "key_drivers": {"type": "string", "description": "JSON: ['驱动1','驱动2']"},
                "common_risks": {"type": "string", "description": "JSON: ['风险1','风险2']"},
                "valuation_notes": {"type": "string", "description": "估值注意事项"},
            },
            "required": ["sector", "characteristics"],
        },
    },
    {
        "name": "get_history",
        "description": "读取某只股票的历史论点、预测和回顾记录",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "save_pattern",
        "description": "沉淀一个发现的模式（mistake/insight/success）",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string", "enum": ["mistake", "insight", "success"]},
                "examples": {"type": "string", "description": "JSON: ['ticker1','ticker2']"},
            },
            "required": ["name", "description", "category"],
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
            results = fetch_batch(tickers[:20])
            brief = []
            for r in results:
                brief.append({
                    "ticker": r["ticker"], "name": r["name"],
                    "sector": r["sector"],
                    "market_cap_yi": round(r["market_cap"] / 1e8, 2),
                    "revenue_cagr_3y": r["revenue_cagr_3y"],
                    "gross_margin": r["gross_margin"],
                    "net_margin": r["net_margin"],
                    "roe": r["roe"], "debt_ratio": r["debt_ratio"],
                    "pe": r["pe"], "div_yield": r["div_yield"],
                    "gross_margin_p50": r.get("gross_margin_p50"),
                    "roe_p50": r.get("roe_p50"),
                    "revenue_cagr_p75": r.get("revenue_cagr_p75"),
                    "ocf_neg_3yr": r["ocf_neg_3yr"],
                    "ni_neg_2yr": r["ni_neg_2yr"],
                    "gm_trend": r["gm_trend"],
                })
            return json.dumps(brief, ensure_ascii=False, default=str)

    elif name == "save_thesis":
        try:
            predictions_json = args.get("predictions", "[]")
            assumptions_json = args.get("assumptions", "[]")
            risk_flags_json = args.get("risk_flags", "[]")
            key_metrics_json = args.get("key_metrics", "{}")

            store.save_thesis(
                ticker=args["ticker"],
                thesis=args["thesis"],
                predictions=json.loads(predictions_json) if isinstance(predictions_json, str) else predictions_json,
                assumptions=json.loads(assumptions_json) if isinstance(assumptions_json, str) else assumptions_json,
                risk_flags=json.loads(risk_flags_json) if isinstance(risk_flags_json, str) else risk_flags_json,
                key_metrics=json.loads(key_metrics_json) if isinstance(key_metrics_json, str) else key_metrics_json,
            )
            return "论点已保存到 SQLite + Markdown"
        except Exception as e:
            return f"保存失败: {e}"

    elif name == "save_sector_knowledge":
        store.save_sector_knowledge(
            sector=args["sector"],
            characteristics=args["characteristics"],
            key_drivers=json.loads(args["key_drivers"]) if isinstance(args.get("key_drivers"), str) and args.get("key_drivers") else args.get("key_drivers"),
            common_risks=json.loads(args["common_risks"]) if isinstance(args.get("common_risks"), str) and args.get("common_risks") else args.get("common_risks"),
            valuation_notes=args.get("valuation_notes"),
        )
        return f"板块知识已保存: {args['sector']}"

    elif name == "get_history":
        thesis = store.get_thesis(args["ticker"])
        reviews = store.get_reviews(args["ticker"])
        result = {"thesis": thesis, "reviews": reviews}
        return json.dumps(result, ensure_ascii=False, default=str)

    elif name == "save_pattern":
        store.save_pattern(
            name=args["name"],
            description=args["description"],
            category=args["category"],
            examples=json.loads(args["examples"]) if isinstance(args.get("examples"), str) and args.get("examples") else args.get("examples"),
        )
        return f"模式已沉淀: {args['name']}"

    return f"未知工具: {name}"


# ── Agent 主循环 ──
def _run_agent(system: str, user_prompt: str, mode_desc: str) -> str:
    cfg = load_config()
    agent_cfg = cfg.get("agent", {})
    model = agent_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = agent_cfg.get("max_tokens", 16384)

    client = _make_client()
    messages = [{"role": "user", "content": user_prompt}]

    print(f"[AGENT] {mode_desc} | {model}")

    while True:
        response = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages, tools=TOOLS_V2,
        )

        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
                print(block.text, end="", flush=True)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            return "\n".join(text_parts)

        # 处理工具调用
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for tb in tool_calls:
            print(f"\n  [TOOL] {tb.name}...")
            result = _execute_tool(tb.name, tb.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tb.id,
                "content": result[:8000],
            })

        messages.append({"role": "user", "content": tool_results})


# ── CLI 命令实现 ──
def do_scan(topic: str, tickers: list[str] = None, output: str = None):
    if tickers:
        ticker_list = "\n".join(f"  - {t}" for t in tickers)
        user_msg = f"""## scan 模式

板块: {topic}
股票: {len(tickers)} 只
{ticker_list}

请拉取数据，横向对比分析。完成后调用 save_sector_knowledge 更新板块知识。"""
    else:
        user_msg = f"""## scan 模式

板块: {topic}

请搜索该板块主要成分股，拉取数据做横向对比。完成后调用 save_sector_knowledge 保存行业知识。"""

    system = _build_system_prompt("scan", sector=topic)
    result = _run_agent(system, user_msg, f"scan: {topic}")

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(result, encoding="utf-8")
        print(f"\n[OUTPUT] {output}")


def do_deep(ticker: str):
    print(f"[DATA] {ticker}...")
    data = fetch_fundamentals(ticker)
    if not data:
        print(f"[ERROR] 无法获取 {ticker} 数据")
        return

    sector = data.get("sector", "")

    user_msg = f"""## deep 模式

Ticker: {ticker}

### 基本面数据
```json
{json.dumps(data, ensure_ascii=False, default=str, indent=2)}
```

请按五维框架做深度分析。**必须在第四节输出预测注册表。** 完成后调用 save_thesis 保存。"""

    system = _build_system_prompt("deep", sector=sector, ticker=ticker)
    _run_agent(system, user_msg, f"deep: {ticker}")


def do_review(days: int = 90):
    due = store.list_due_reviews(days)
    if not due:
        print("[REVIEW] 无需回顾")
        print(store.dashboard())
        return

    print(f"[REVIEW] {len(due)} 只待回顾:")

    for item in due:
        ticker = item["ticker"]
        print(f"\n{'='*60}")
        print(f"  {ticker} (上次: {item['updated_at']})")

        thesis = store.get_thesis(ticker)
        if not thesis:
            continue

        # 拉最新数据
        data = fetch_fundamentals(ticker, with_benchmarks=False)
        if not data:
            print(f"  [SKIP] 无数据")
            continue

        # 验证预测
        results = predictions.verify(ticker, thesis["id"], data)
        print(f"  预测验证: {len(results)} 条")
        for r in results:
            status_emoji = {"正确": "✓", "部分正确": "△", "错误": "✗", "无法验证": "?"}.get(r["result"], "?")
            print(f"    {status_emoji} {r['prediction'][:60]}")
            print(f"      预期: {r['expected']} → 实际: {r['actual']} ({r['result']})")

        # 保存回顾
        learnings_str = f"预测准确率: {sum(1 for r in results if r['result']=='正确')}/{len(results)}"
        store.save_review(
            ticker=ticker,
            thesis_id=thesis["id"],
            prediction_results=results,
            learnings=learnings_str,
        )

    # 分析偏差
    biases = evolution.analyze_biases()
    acc = store.get_prediction_accuracy()
    print(f"\n{'='*60}")
    print(f"[REVIEW] 整体准确率: {acc['accuracy']}% ({acc['correct']}/{acc['total']})")

    if biases["weakest"]:
        print("最弱维度:")
        for m, a in biases["weakest"]:
            print(f"  {m}: {a}%")


def do_evolve():
    """进化引擎: 分析偏差 → 提取模式 → 建议框架更新。"""
    print("[EVOLVE] 分析预测偏差...")
    biases = evolution.analyze_biases()

    if not biases["dimensions"]:
        print("  尚无足够数据进行偏差分析。至少需要3次回顾记录。")
        print(f"  当前状态: {store.dashboard()}")
        return

    print(f"\n  预测总样本: {sum(s['total'] for s in biases['dimensions'].values())}")
    print("  各维度准确率:")
    for m, s in sorted(biases["dimensions"].items(), key=lambda x: x[1]["accuracy"]):
        if s["total"] >= 2:
            print(f"    {m:20s}: {s['accuracy']:5.1f}% ({s['correct']}/{s['total']})")

    print("\n[EVOLVE] 提取模式...")
    patterns = evolution.extract_patterns()

    if not patterns:
        print("  未发现显著模式。")
    else:
        for p in patterns:
            print(f"\n  [{p['category']}] {p['name']}")
            print(f"  {p['description']}")
            if p.get("suggested_fix"):
                print(f"  建议: {p['suggested_fix']}")

    print("\n[EVOLVE] 框架更新建议...")
    suggestions = evolution.suggest_framework_updates()

    if not suggestions:
        print("  当前框架无需调整。")

    for i, s in enumerate(suggestions):
        print(f"\n  --- 建议 {i+1} [{s['confidence']}置信度] ---")
        print(f"  目标: {s['target']}")
        print(f"  类型: {s['type']}")
        print(f"  原因: {s['reason']}")
        print(f"  内容: {s['suggested_content'][:200]}...")

    if suggestions:
        print(f"\n  共 {len(suggestions)} 条建议。审核后运行 fa evolve --apply 执行。")
