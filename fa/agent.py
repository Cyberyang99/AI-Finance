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

from .config import ANTHROPIC_KEY, ANTHROPIC_BASE_URL, load_config, make_anthropic_client
from .memory import MemoryStore, PredictionRegistry, EvolutionEngine, PerformanceTracker, SituationStore
from .memory.store import PROJECT_DIR
from .agents import CriticAgent, RecallAgent, ReflectorAgent, ConflictResolver
from .tools.data import fetch_fundamentals, fetch_batch
from .tools.sector import find_sector_peers, list_sectors
from .framework import get_framework_prompt

# ── 全局实例 ──
store = MemoryStore()
predictions = PredictionRegistry(store)
performance = PerformanceTracker(store)
evolution = EvolutionEngine(store)
critic = CriticAgent()
situations = SituationStore()
recall = RecallAgent()
reflector = ReflectorAgent()
conflict_resolver = ConflictResolver()

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

---

{situational_memory}
"""


def _build_system_prompt(mode: str, sector: str = None, ticker: str = None,
                          recalled_notes: list = None) -> str:
    """渐进式构建系统提示词 — 按模式加载相关内容。

    recalled_notes: 召回的情境笔记 list[dict] (含 body)，注入到 situational_memory 段。
    """
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

    # 情境记忆段
    situational_memory = _format_situational_memory(recalled_notes)

    # 用 replace 而非 format，避免框架内容中的花括号冲突
    prompt = SYSTEM_PROMPT_V2
    prompt = prompt.replace("{framework_summary}", framework_summary)
    prompt = prompt.replace("{global_context}", global_context)
    prompt = prompt.replace("{sector_context}", sector_context)
    prompt = prompt.replace("{situational_memory}", situational_memory)
    return prompt


def _format_situational_memory(notes: list = None) -> str:
    """把召回的情境笔记格式化成 system prompt 段落。

    用户论点 (id 以 user_ 开头) 单独高亮在最前面，权重信号给到模型。
    """
    if not notes:
        return "## 情境记忆\n（本次未召回任何历史经验笔记。如有类似案例，请优先参考自身经验。）"

    user_notes = [n for n in notes if str(n.get("id", "")).startswith("user_")]
    other_notes = [n for n in notes if not str(n.get("id", "")).startswith("user_")]

    parts = []
    if user_notes:
        parts.extend([
            "## 用户论点（最高优先级，必须重点对照）",
            f"> 以下 {len(user_notes)} 条是用户亲自录入的对该标的的判断和思考。",
            f"> 你的分析结论必须明确与用户论点进行比对：如果同意，说明在哪几点；",
            f"> 如果有分歧，必须解释具体在哪里、为什么。**绝不允许忽略用户论点**。",
            "",
        ])
        for i, n in enumerate(user_notes, 1):
            parts.extend([
                f"### 用户论点 {i}: {n.get('situation', '?')}",
                "",
                n.get("body", "").strip(),
                "",
                "---",
                "",
            ])

    if other_notes:
        parts.extend([
            "## 情境记忆 — 历史经验笔记",
            f"> 系统召回了 {len(other_notes)} 条与当前任务相关的历史经验笔记。",
            f"> 这些是过往论点的复盘沉淀，请结合当前情境判断是否适用。",
            "",
        ])
        for i, n in enumerate(other_notes, 1):
            parts.extend([
                f"### 笔记 {i}: {n.get('situation', '?')}",
                f"- 适用行业: {', '.join(n.get('sector_scope', ['all']))}",
                f"- 置信度: {n.get('confidence', 0.5)}",
                f"- 召回理由: {n.get('_recall_reason', '相关')}",
                "",
                n.get("body", "").strip(),
                "",
                "---",
                "",
            ])
    return "\n".join(parts)


# ── Anthropic 客户端（自动处理 DeepSeek 代理） ──
def _make_client() -> anthropic.Anthropic:
    return make_anthropic_client()


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
    """运行 agent 循环。

    返回值：累积所有轮次的 assistant text（不只是最后一轮）。
    deep 模式下用这个完整文本喂给 extract_12d 抽 canonical_15d_v1 deep note。
    """
    cfg = load_config()
    agent_cfg = cfg.get("agent", {})
    model = agent_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = agent_cfg.get("max_tokens", 16384)

    client = _make_client()
    messages = [{"role": "user", "content": user_prompt}]

    print(f"[AGENT] {mode_desc} | {model}")

    all_text_parts: list[str] = []

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
                all_text_parts.append(block.text)
                print(block.text, end="", flush=True)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            return "\n\n".join(all_text_parts)

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

    # 情境记忆召回
    recalled_notes = _recall_for_scan(topic, tickers)

    system = _build_system_prompt("scan", sector=topic, recalled_notes=recalled_notes)
    result = _run_agent(system, user_msg, f"scan: {topic}")

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(result, encoding="utf-8")
        print(f"\n[OUTPUT] {output}")


def _recall_for_scan(topic: str, tickers: list[str] = None) -> list:
    """为 scan 分析召回相关情境笔记。失败不阻塞。P1: 行业硬过滤。"""
    try:
        highlights = f"板块: {topic}, 成分股: {len(tickers or [])} 只"
        ctx = {
            "ticker": topic,
            "name": topic,
            "sector": topic,
            "task": "scan",
            "highlights": highlights,
        }
        # P1: 行业门限硬过滤
        index = situations.build_index_for_sector(topic)
        if "暂无笔记" in index:
            return []
        selected = recall.recall(ctx, index, top_k=3)
        if selected:
            note_ids = [s["id"] for s in selected]
            print(f"  [RECALL] 召回 {len(note_ids)} 条情境笔记 (板块 {topic}): {note_ids}")
            return situations.get_full_notes(note_ids)
    except Exception as e:
        print(f"  [RECALL] 跳过: {e}")
    return []


def do_deep(ticker: str):
    print(f"[DATA] {ticker}...")
    data = fetch_fundamentals(ticker)
    if not data:
        print(f"[ERROR] 无法获取 {ticker} 数据")
        return

    sector = data.get("sector", "")

    # 情境记忆召回
    recalled_notes, recalled_situ_ids = _recall_for_deep(ticker, data)

    user_msg = f"""## deep 模式

Ticker: {ticker}

### 基本面数据
```json
{json.dumps(data, ensure_ascii=False, default=str, indent=2)}
```

请按五维框架做深度分析。**必须在第四节输出预测注册表。** 完成后调用 save_thesis 保存。"""

    system = _build_system_prompt("deep", sector=sector, ticker=ticker,
                                  recalled_notes=recalled_notes)
    full_text = _run_agent(system, user_msg, f"deep: {ticker}")

    # 召回反馈闭环：记下这次预测召回了哪些情境笔记，供 fa evolve 算胜率 / 识别僵尸笔记
    if recalled_situ_ids:
        try:
            store.set_thesis_recall(ticker, recalled_situ_ids)
        except Exception as e:
            print(f"  [RECALL] 召回记录写入跳过: {e}")

    # ── 后置：把 agent 输出的完整分析过一遍 LLM，抽 15 维 deep note ──
    print(f"\n\n[DEEP→15d] 抽取 15 维 deep note...")
    try:
        from .note_extractor import extract_12d
        from .ingest.user_note import save_note_12d, inherit_sector_tags
        from .note_template import filled_dims, JSON_DIM_IDS, is_filled

        # 把基本面数据也拼进 raw_text 供 15d 抽取（agent 输出可能没覆盖财务细节）
        raw_text = (
            full_text
            + "\n\n## 附：基本面数据（结构化）\n```json\n"
            + json.dumps(data, ensure_ascii=False, default=str, indent=2)
            + "\n```"
        )
        payload = extract_12d(ticker, raw_text, user_comment="agent 自动 deep 分析")
        filled = filled_dims(payload)
        json_filled = [k for k in JSON_DIM_IDS if is_filled(payload.get(k))]
        print(f"  ✓ 填了 {len(filled)}/15 维度，量化字段 {json_filled}")

        # 继承 sector/tags (从 CoT 库)，没有就用 fetch 的 sector
        inherited_sector, inherited_tags = inherit_sector_tags(ticker)
        final_sector = inherited_sector or sector or "Other"
        final_tags = inherited_tags or []

        if filled:
            path = save_note_12d(
                ticker=ticker, payload=payload,
                sector=final_sector, tags=final_tags,
                source="llm_deep",
                user_comment="agent fa deep 跑出的自动分析（结构化 15 维版）",
                filename_suffix="deep",
            )
            print(f"  ✓ 已保存 deep note → {path.name}")
        else:
            print(f"  ⚠ 15 维全空，跳过保存")
    except Exception as e:
        print(f"  [DEEP→15d] 15 维抽取失败（不影响 agent 主流程）: {e}")


def _recall_for_deep(ticker: str, data: dict) -> list:
    """为 deep 分析召回相关情境笔记 + 用户论点。失败不阻塞。

    P0: 用户论点优先 — 直接取该 ticker 的所有 user note，append 在情境笔记前面。
    用户输入比 agent 自己沉淀的笔记权重高（PDF2 设计：和你思考逻辑对齐）。
    """
    out = []
    situ_note_ids = []  # 仅情境笔记的 id（用户论点/CoT 不在此列），供召回反馈闭环记账

    # 1. 用户论点优先（无 LLM 调用，按 ticker 直接拉）
    try:
        from .ingest.user_note import load_user_notes
        user_notes = load_user_notes(ticker)
        for un in user_notes[:3]:  # 最多 3 条最新的
            out.append({
                "id": f"user_{un['created_at']}",
                "situation": f"用户论点 ({un['created_at']})",
                "sector_scope": [data.get("sector", "all")],
                "confidence": 1.0,
                "_recall_reason": "用户亲自录入的论点，权重最高，必须重点对照",
                "body": un["content"],
            })
        if user_notes:
            print(f"  [RECALL] 用户论点 {len(user_notes)} 条 (注入前 {min(3,len(user_notes))} 条)")
    except Exception as e:
        print(f"  [RECALL] 用户论点跳过: {e}")

    # 2. LLM 召回情境笔记（先按行业硬过滤）
    try:
        sector = data.get("sector", "未知")
        highlights = (
            f"毛利率: {data.get('gross_margin')}%, ROE: {data.get('roe')}%, "
            f"营收增速: {data.get('revenue_cagr_3y')}%, PE: {data.get('pe')}"
        )
        ctx = {
            "ticker": ticker,
            "name": data.get("name", ""),
            "sector": sector,
            "task": "deep",
            "highlights": highlights,
        }
        # P1: 行业门限硬过滤 — 缩小候选池后再让 LLM 选 Top-K
        index = situations.build_index_for_sector(sector)
        if "暂无笔记" in index:
            print(f"  [RECALL] 行业 {sector} 无适用笔记")
        else:
            selected = recall.recall(ctx, index, top_k=5)
            if selected:
                note_ids = [s["id"] for s in selected]
                situ_note_ids = note_ids
                print(f"  [RECALL] 情境笔记召回 {len(note_ids)} 条 (行业过滤后): {note_ids}")
                full = situations.get_full_notes(note_ids)
                out.extend(full)
    except Exception as e:
        print(f"  [RECALL] 情境笔记跳过: {e}")

    # 3. P2: 注入相关 CoT 作为辅助逻辑参考（无 LLM 调用，纯磁盘读）
    try:
        from .cot.loader import load_cots
        sector = data.get("sector", "")
        # 按 ticker 自己的 sector 找 CoT，没有就全库高信号 fallback
        cots = load_cots(sector=sector, min_signal=8) if sector else []
        if not cots:
            cots = load_cots(min_signal=9)
        if cots:
            # 取信号最高的 5 条作为参考
            cots = sorted(cots, key=lambda c: -int(c.get("signal", "5")))[:5]
            body_lines = ["以下是从历史研报中提炼的高信号思维链，供你做基本面分析时参考："]
            for c in cots:
                body_lines.append(
                    f"- [{c['signal']}/10] **{c['trigger']}**: {c['COT']}"
                )
            out.append({
                "id": "cot_reference",
                "situation": "研报思维链参考（不必逐条对照，作为分析的逻辑库）",
                "sector_scope": [sector or "all"],
                "confidence": 0.7,
                "_recall_reason": "研报里高信号 (signal≥8) 的推理链摘要",
                "body": "\n".join(body_lines),
            })
            print(f"  [RECALL] 注入 {len(cots)} 条 CoT 作为分析参考")
    except Exception as e:
        print(f"  [RECALL] CoT 注入跳过: {e}")

    return out, situ_note_ids


def _apply_reflection(candidates: list[dict], ticker: str, excess: float = None) -> dict:
    """对 Reflector 产出的候选笔记，逐条用 ConflictResolver 决策并落盘。

    返回统计 {"add": N, "skip": N, "replace": N, "branch": N}.
    """
    stats = {"add": 0, "skip": 0, "replace": 0, "branch": 0}
    if not candidates:
        return stats

    existing = situations.list_notes()
    for cand in candidates:
        decision = conflict_resolver.resolve(cand, existing)
        op = decision["decision"]
        tid = decision.get("target_id")
        reason = decision.get("reason", "")

        print(f"  [Reflect] 候选笔记: {cand['situation'][:50]}")
        print(f"    决策: {op} | target={tid or '-'} | 理由: {reason[:120]}")

        if op == "skip":
            stats["skip"] += 1
            continue

        if op == "replace" and tid:
            # 旧笔记归档，新笔记用新 id
            situations.archive(tid)
            note = _make_note_from_candidate(cand, ticker, excess)
            situations.save(note)
            stats["replace"] += 1
            existing = situations.list_notes()  # 刷新池供后续候选用

        elif op == "branch" and tid:
            # 在旧笔记 body 末尾追加例外分支
            old = situations.load(tid)
            if old:
                body = old.get("body", "")
                branch_text = (
                    f"\n\n## 例外分支 (新增 {_today()})\n\n"
                    f"### 触发情境\n{cand['situation']}\n\n"
                    f"### 经验\n{cand['body']}\n"
                )
                old["body"] = body + branch_text
                old["refined_count"] = old.get("refined_count", 0) + 1
                # 同步扩展 sector_scope 如需
                situations.save(old)
                stats["branch"] += 1

        else:  # add (默认)
            note = _make_note_from_candidate(cand, ticker, excess)
            situations.save(note)
            stats["add"] += 1
            existing = situations.list_notes()

    return stats


def _today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def _make_note_from_candidate(cand: dict, source_ticker: str, excess_return: float = None) -> dict:
    """把 Reflector 输出的候选笔记转换成可保存的 note dict。"""
    return {
        "situation": cand.get("situation", ""),
        "retrieval_text": cand.get("retrieval_text", cand.get("situation", "")),
        "body": cand.get("body", ""),
        "sector_scope": cand.get("sector_scope") or ["all"],
        "sector_excluded": cand.get("sector_excluded") or [],
        "confidence": float(cand.get("confidence", 0.6)),
        "source_thesis": source_ticker,
        "source_excess_return": excess_return,
        "validated_on": [source_ticker],
        "refined_count": 0,
        "absorbed": False,
        "archived": False,
    }


def do_review(days: int = 90, with_critic: bool = True, with_reflector: bool = True):
    due = store.list_due_reviews(days)
    if not due:
        print("[REVIEW] 无需回顾")
        _print_dashboard()
        return

    print(f"[REVIEW] {len(due)} 只待回顾 (Critic: {'开' if with_critic else '关'})")

    for item in due:
        ticker = item["ticker"]
        print(f"\n{'='*60}")
        print(f"  {ticker} (上次: {item['updated_at']})")

        thesis = store.get_thesis(ticker)
        if not thesis:
            continue

        # 1. 拉最新数据
        data = fetch_fundamentals(ticker, with_benchmarks=False)
        if not data:
            print(f"  [SKIP] 无数据")
            continue

        # 2. 主观评分（预测验证）
        results = predictions.verify(ticker, thesis["id"], data)
        correct_n = sum(1 for r in results if r["result"] == "正确")
        partial_n = sum(1 for r in results if r["result"] == "部分正确")
        total_n = len(results)
        # 主观得分: 正确=1.0, 部分=0.5, 错误=0
        subjective = None
        if total_n > 0:
            subjective = round((correct_n + 0.5 * partial_n) / total_n, 3)

        print(f"  [主观] 预测验证: {correct_n}/{total_n} 正确 (得分 {subjective})")
        for r in results:
            emoji = {"正确": "✓", "部分正确": "△", "错误": "✗", "无法验证": "?"}.get(r["result"], "?")
            print(f"    {emoji} {r['prediction'][:60]}")
            print(f"      预期: {r['expected']} → 实际: {r['actual']} ({r['result']})")

        # 3. 客观评分（vs 大盘超额）
        perf = performance.evaluate(ticker, subjective_score=subjective)
        if perf and "error" not in perf:
            print(f"  [客观] {perf['verdict']} | 持仓 {perf['days_held']}天")
            print(f"    股票: {perf['stock_return']:+.2f}% | "
                  f"{perf['baseline']['index_name']}: {perf['index_return']:+.2f}% | "
                  f"超额: {perf['excess_return']:+.2f}%")
            print(f"    客观分: {perf['objective_score']} | "
                  f"主观分: {perf['subjective_score']} | "
                  f"综合: {perf['composite_score']} (0.7×客观 + 0.3×主观)")
        elif perf and "error" in perf:
            print(f"  [客观] 跳过: {perf['error']}")
            if perf.get("hint"):
                print(f"    提示: {perf['hint']}")
            perf = None
        else:
            print(f"  [客观] 跳过: 无法评估")
            perf = None

        # 4. Critic 评审（独立 LLM 调用，评分锚定在客观分上下 ±0.2）
        critic_out = None
        if with_critic and perf:
            print(f"  [Critic] 调用 LLM 评审...")
            critic_out = critic.critique(thesis, perf, results, current_fundamentals=data)
            performance.attach_critic(perf["performance_id"], critic_out)

            if critic_out["critic_score"] is not None:
                anchor_note = " (已锚定调整)" if critic_out["anchor_adjusted"] else ""
                print(f"  [Critic] LLM 评分: {critic_out['raw_llm_score']} → "
                      f"锚定后 {critic_out['critic_score']}{anchor_note}")
                print(f"  [Critic] 最终综合: {critic_out['final_score']} (0.7×客观 + 0.3×Critic)")
                if critic_out["what_worked"]:
                    print(f"  ✓ 对了: {critic_out['what_worked']}")
                if critic_out["what_failed"]:
                    print(f"  ✗ 错了: {critic_out['what_failed']}")
                if critic_out["improvement_hints"]:
                    print(f"  → 改进建议:")
                    for h in critic_out["improvement_hints"]:
                        print(f"     - {h}")
                if critic_out["critique"]:
                    print(f"  [评审]\n  {critic_out['critique']}")
            else:
                print(f"  [Critic] {critic_out['critique']}")

        # 4.5 Reflector — 重大失败/成功才触发，产出候选笔记 → ConflictResolver 落盘
        if with_reflector and perf and critic_out:
            should, why = reflector.should_reflect(perf, critic_out)
            if should:
                print(f"  [Reflector] 触发 ({why})...")
                reflection = reflector.reflect(thesis, perf, results, critic_out,
                                               current_fundamentals=data)
                diag = reflection.get("diagnosis", {})
                cands = reflection.get("candidate_notes", [])

                if reflection.get("error"):
                    print(f"  [Reflector] {reflection['error']}")
                else:
                    if diag.get("root_cause"):
                        print(f"  [诊断] root_cause: {diag['root_cause'][:150]}")
                        print(f"  [诊断] pattern_type: {diag.get('pattern_type', '?')}")
                    if cands:
                        print(f"  [Reflector] 产出 {len(cands)} 条候选笔记，进入冲突仲裁...")
                        stats = _apply_reflection(cands, ticker,
                                                  excess=perf.get("excess_return"))
                        print(f"  [Reflector] 笔记落盘: "
                              f"add={stats['add']} skip={stats['skip']} "
                              f"replace={stats['replace']} branch={stats['branch']}")
                    else:
                        print(f"  [Reflector] 无可泛化经验，未产出笔记")
            else:
                print(f"  [Reflector] 跳过 ({why})")

        # 5. 保存回顾记录
        score_parts = [f"主观 {subjective}"]
        if perf:
            score_parts.append(f"客观 {perf['objective_score']}")
        if critic_out and critic_out.get("final_score") is not None:
            score_parts.append(f"最终 {critic_out['final_score']}")
        learnings_str = " | ".join(score_parts)
        store.save_review(
            ticker=ticker,
            thesis_id=thesis["id"],
            prediction_results=results,
            learnings=learnings_str,
        )

    # 整体汇总
    print(f"\n{'='*60}")
    summary = performance.summary()
    if summary["total"] > 0:
        print(f"[组合表现] {summary['total']} 只 | 胜率 {summary['win_rate']}% | "
              f"平均超额 {summary['avg_excess']:+.2f}% | 平均客观分 {summary['avg_objective_score']}")
        print(f"  最佳: {summary['best']['ticker']} ({summary['best']['excess']:+.2f}%) | "
              f"最差: {summary['worst']['ticker']} ({summary['worst']['excess']:+.2f}%)")

    acc = store.get_prediction_accuracy()
    if acc["total"] > 0:
        print(f"[预测准确率] {acc['accuracy']}% ({acc['correct']}/{acc['total']})")

    biases = evolution.analyze_biases()
    if biases["weakest"]:
        print("[最弱维度]:")
        for m, a in biases["weakest"]:
            print(f"  {m}: {a}%")


def _print_dashboard():
    """简易仪表盘 (do_review 无任务时调用)。"""
    dash = store.dashboard()
    summary = performance.summary()
    print(f"\n  活跃论点: {dash['active_theses']} | 待回顾: {dash['reviews_due']}")
    if summary["total"] > 0:
        print(f"  组合胜率: {summary['win_rate']}% | 平均超额: {summary['avg_excess']:+.2f}%")


def do_evolve(apply_index: int = None):
    """进化引擎: 分析偏差 → 提取模式 → 建议框架更新。

    apply_index: 如果指定，直接执行该编号的建议（1-based）。
    """
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

    print("\n[EVOLVE] 情境笔记召回胜率（僵尸笔记识别）...")
    note_stats = store.note_recall_stats()
    reviewed = [n for n in note_stats if n["total"] > 0]
    if not reviewed:
        print("  暂无「召回笔记 × 已回顾论点」样本（需先 fa deep 召回笔记、再 fa review 验证预测）。")
    else:
        for n in reviewed[:8]:
            note = situations.load(n["note_id"])
            title = note.get("situation", "")[:40] if note else "（笔记已删除）"
            print(f"    {n['hit_rate']:5.1f}% | 召回 {n['recall_count']:>2} 次 / 已验证 {n['reviewed_theses']} "
                  f"| {n['note_id']}  {title}")
        zombies = [n for n in reviewed if n["recall_count"] >= 2 and (n["hit_rate"] or 0) < 50]
        if zombies:
            print(f"\n  ⚠ 疑似僵尸笔记 {len(zombies)} 条（召回≥2 次但胜率<50%），建议复核或 archive：")
            for n in zombies:
                print(f"    - {n['note_id']} (胜率 {n['hit_rate']}%, 召回 {n['recall_count']} 次)")

    print("\n[EVOLVE] 框架更新建议...")
    suggestions = evolution.suggest_framework_updates()

    if not suggestions:
        print("  当前框架无需调整。")
        return

    for i, s in enumerate(suggestions):
        print(f"\n  --- 建议 {i+1} [{s['confidence']}置信度] ---")
        print(f"  目标: {s['target']}")
        print(f"  类型: {s['type']}")
        print(f"  原因: {s['reason']}")
        print(f"  内容: {s['suggested_content'][:200]}...")

    # ── apply 模式 ──
    if apply_index is not None:
        if apply_index < 1 or apply_index > len(suggestions):
            print(f"\n[EVOLVE] 无效编号: {apply_index} (有效: 1-{len(suggestions)})")
            return
        s = suggestions[apply_index - 1]
        print(f"\n[EVOLVE] 执行建议 {apply_index}...")
        result = evolution.execute_update(s)

        # 如果目标是 framework 文件，写入实际文件
        target = s["target"]
        if target.startswith("framework/") and s["type"] in ("add", "modify"):
            fname = target.replace("framework/", "")
            fpath = FRAMEWORK_DIR / fname
            if s["type"] == "add":
                existing = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
                new_content = existing + "\n\n" + s["suggested_content"]
                fpath.write_text(new_content, encoding="utf-8")
                print(f"  已追加到 {fpath}")
            elif s["type"] == "modify":
                fpath.write_text(s["suggested_content"], encoding="utf-8")
                print(f"  已覆盖 {fpath}")

        print(f"  {result['status']}: {result['target']}")
        return

    if suggestions:
        print(f"\n  共 {len(suggestions)} 条建议。审核后运行 fa evolve --apply N 执行。")
