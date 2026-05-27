"""review_v2 — 基于 12d note frontmatter 的结构化复盘。

入口: fa review2 <ticker>

四个对照维度（按 note 的 frontmatter JSON 字段）:
  1. financial_forecast   → EODHD 年报 (totalRevenue / netIncome / 派生 net_margin)
  2. valuation_target     → fetch_price_at × sharesOutstanding 当前市值 vs base/bull/bear
  3. catalysts            → window 字符串解析，标 已到期 / 未到期 / 无窗口
  4. long_term_space      → 远期空间，只列出待跟踪（不强行评分）

输出: memory/reviews/<ticker>_<YYYY-MM-DD>_v2.md
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import ANTHROPIC_KEY, ANTHROPIC_AUTH_TOKEN, load_config, make_anthropic_client
from .tools.data import fetch_fundamentals, fetch_price_at


# ──────────────────────────────────────────────────────────────────────
# 1. 读取 12d note
# ──────────────────────────────────────────────────────────────────────

NOTES_DIR = Path("memory/theses/user")
REVIEWS_DIR = Path("memory/reviews")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """切 YAML frontmatter + body。无 frontmatter 时返回 ({}, text)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, parts[2]


def load_12d_note(ticker: str, notes_dir: Path = NOTES_DIR) -> Optional[dict]:
    """找该 ticker 最新的 12d_v1 note，返回 frontmatter dict + 路径。

    返回结构:
      {"path": Path, "fm": {...}, "body": str}
    无匹配返回 None。
    """
    t = ticker.upper().strip()
    candidates: list[tuple[str, Path]] = []
    if not notes_dir.exists():
        return None
    for fp in notes_dir.glob(f"{t}_*.md"):
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = _split_frontmatter(text)
        if fm.get("template_version") != "12d_v1":
            continue
        if str(fm.get("ticker", "")).upper().strip() != t:
            continue
        created = str(fm.get("created_at", "2000-01-01"))
        candidates.append((created, fp))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    latest_path = candidates[0][1]
    text = latest_path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    return {"path": latest_path, "fm": fm, "body": body}


# ──────────────────────────────────────────────────────────────────────
# 2. 财务预测 vs 实际
# ──────────────────────────────────────────────────────────────────────

def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _yi(v: Optional[float]) -> Optional[float]:
    """元 → 亿，保留 2 位。"""
    if v is None:
        return None
    return round(v / 1e8, 2)


def _extract_actual_year(fundamentals: dict, year: int) -> dict:
    """从 EODHD fundamentals 抽指定年份的 revenue / netIncome / margin。

    EODHD 历史序列在 fundamentals["history"] 字典，键格式 "YYYY-MM-DD"。
    fetch_fundamentals 已经把 inc_y / cf_y 拍平到顶层字段（latest），但历史是 series。
    这里走 rev_hist / ni_hist 序列。
    """
    out = {"year": year, "revenue_yi": None, "net_profit_yi": None, "net_margin": None}

    # fetch_fundamentals 返回的字段名（见 _fetch_eodhd 拍平后）
    rev_hist = fundamentals.get("rev_hist") or []
    ni_hist = fundamentals.get("ni_hist") or []

    rev = next((v for d, v in rev_hist if d.startswith(str(year))), None)
    ni = next((v for d, v in ni_hist if d.startswith(str(year))), None)

    out["revenue_yi"] = _yi(_safe_float(rev))
    out["net_profit_yi"] = _yi(_safe_float(ni))
    if rev and ni:
        out["net_margin"] = round(_safe_float(ni) / _safe_float(rev), 4)
    return out


def _verdict(pred: Optional[float], actual: Optional[float], tol_pct: float = 0.15) -> str:
    """误差容忍度评分：±tol 内 ✓，±2×tol ~ ±tol △，超出 ✗。actual 缺失 ?。"""
    if actual is None:
        return "?"
    if pred is None:
        return "?"
    if pred == 0:
        return "?"
    err = abs(actual - pred) / abs(pred)
    if err <= tol_pct:
        return "✓"
    if err <= tol_pct * 2:
        return "△"
    return "✗"


def compare_financial(forecast: list, fundamentals: Optional[dict]) -> list[dict]:
    """逐年对照 forecast 与实际。

    forecast: list[dict]，每项形如 {year, revenue_yi, net_margin, net_profit_yi, ...}
    返回: list[dict]，每年一条，含 pred/actual/verdict/diff_pct。
    """
    if not forecast or not fundamentals:
        return []
    rows = []
    for f in forecast:
        year = int(f.get("year", 0))
        if not year:
            continue
        actual = _extract_actual_year(fundamentals, year)
        row = {
            "year": year,
            "pred_revenue_yi": _safe_float(f.get("revenue_yi")),
            "pred_net_margin": _safe_float(f.get("net_margin")),
            "pred_net_profit_yi": _safe_float(f.get("net_profit_yi")),
            "actual_revenue_yi": actual["revenue_yi"],
            "actual_net_margin": actual["net_margin"],
            "actual_net_profit_yi": actual["net_profit_yi"],
        }
        row["verdict_revenue"] = _verdict(row["pred_revenue_yi"], row["actual_revenue_yi"])
        row["verdict_margin"] = _verdict(row["pred_net_margin"], row["actual_net_margin"], tol_pct=0.20)
        row["verdict_profit"] = _verdict(row["pred_net_profit_yi"], row["actual_net_profit_yi"])
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────────────
# 3. 估值对照
# ──────────────────────────────────────────────────────────────────────

def compare_valuation(target: dict, ticker: str, fundamentals: Optional[dict]) -> dict:
    """当前市值 vs base/bull/bear。

    需要：当前股价 (fetch_price_at) × 总股本 (fundamentals.shares_outstanding)。
    EODHD SharesStats.SharesOutstanding 在 fetch_fundamentals 已被读到 shares_outstanding。
    """
    out = {
        "current_price": None,
        "shares_outstanding": None,
        "current_mcap_yi": None,
        "base": target.get("base", {}),
        "bull": target.get("bull", {}),
        "bear": target.get("bear", {}),
        "verdict": None,  # 当前在哪个区间
    }
    price_data = fetch_price_at(ticker)
    if not price_data:
        return out
    out["current_price"] = price_data.get("close")
    shares = (fundamentals or {}).get("shares_outstanding")
    out["shares_outstanding"] = shares
    if out["current_price"] and shares:
        out["current_mcap_yi"] = round(out["current_price"] * shares / 1e8, 2)

    cur = out["current_mcap_yi"]
    if cur is None:
        return out

    base_mcap = _safe_float(out["base"].get("mcap_yi"))
    bull_mcap = _safe_float(out["bull"].get("mcap_yi"))
    bear_drop_pct = _safe_float(out["bear"].get("mcap_drop_pct"))
    # bear 通常给的是跌幅 vs base
    bear_mcap = None
    if base_mcap and bear_drop_pct is not None:
        bear_mcap = round(base_mcap * (1 + bear_drop_pct), 2)

    if bear_mcap is not None and cur < bear_mcap:
        out["verdict"] = "跌破 bear"
    elif bull_mcap is not None and cur >= bull_mcap:
        out["verdict"] = "达到 bull"
    elif base_mcap is not None and cur >= base_mcap:
        out["verdict"] = "在 base ~ bull"
    elif base_mcap is not None:
        out["verdict"] = "bear ~ base"
    return out


# ──────────────────────────────────────────────────────────────────────
# 4. 催化剂窗口检查
# ──────────────────────────────────────────────────────────────────────

_QUARTER_END = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}
_HALF_END = {"H1": "06-30", "H2": "12-31"}


def _parse_window_end(window: str) -> Optional[str]:
    """把 "2026Q3" / "2026H1" / "2026-2027" / "2026Q3-2027Q1" 等解析为窗口截止日期。

    返回 "YYYY-MM-DD"；解析不出返回 None。
    """
    if not window:
        return None
    w = window.strip().upper().replace(" ", "")
    # 范围 "2026-2027" 取后段终点
    if "-" in w:
        parts = w.split("-")
        w = parts[-1]
    m = re.match(r"^(\d{4})Q([1-4])$", w)
    if m:
        return f"{m.group(1)}-{_QUARTER_END['Q' + m.group(2)]}"
    m = re.match(r"^(\d{4})H([12])$", w)
    if m:
        return f"{m.group(1)}-{_HALF_END['H' + m.group(2)]}"
    m = re.match(r"^(\d{4})$", w)
    if m:
        return f"{m.group(1)}-12-31"
    return None


def check_catalysts(catalysts: list, today: Optional[str] = None) -> list[dict]:
    """逐项检查 catalysts 的窗口期。"""
    today_s = today or datetime.now().strftime("%Y-%m-%d")
    out = []
    for c in catalysts or []:
        end = _parse_window_end(str(c.get("window", "")))
        if not end:
            status = "无窗口"
        elif end < today_s:
            status = "已到期"
        else:
            status = "未到期"
        out.append({
            "event": c.get("event", ""),
            "window": c.get("window", ""),
            "monitor": c.get("monitor", ""),
            "window_end": end,
            "status": status,
        })
    return out


# ──────────────────────────────────────────────────────────────────────
# 5a. 反证 / 风险条件检查 (LLM)
# ──────────────────────────────────────────────────────────────────────

CONDITIONS_SYSTEM = """你是基本面投资 review agent。用户写了一组编号条件 (反证条件或风险清单)，请逐条对照当前事实判断状态。

输出严格 JSON:
{
  "items": [
    {"idx": 1, "condition": "原文片段(≤40 字)", "verdict": "已触发|未触发|不可判断", "evidence": "判断依据(≤50 字)"},
    ...
  ]
}

判断准则：
- 已触发：当前财务数据或公开事实明确显示该条件成立
- 未触发：当前财务数据或公开事实明确显示该条件未成立
- 不可判断：缺少数据 / 需要私域信息 / 时间窗未到

不要编造数据。evidence 必须基于用户给你的 fundamentals 字段或常识，不要瞎猜公司动态。
"""


def _fmt_fundamentals_brief(f: Optional[dict]) -> str:
    """把 fundamentals 关键字段简表化，喂给 LLM。"""
    if not f:
        return "(无 fundamentals 数据)"
    parts = [
        f"净利率 {f.get('net_margin')}%",
        f"毛利率 {f.get('gross_margin')}% (趋势 {f.get('gm_trend')}, Δ {f.get('gm_change_pp')}pp)",
        f"ROE {f.get('roe')}%",
        f"资产负债率 {f.get('debt_ratio')}%",
        f"3 年营收 CAGR {f.get('revenue_cagr_3y')}",
        f"OCF {_yi(f.get('ocf'))} 亿, FCF {_yi(f.get('fcf'))} 亿, Capex {_yi(f.get('capex'))} 亿",
        f"OCF 连负 3 年: {f.get('ocf_neg_3yr')}, 净利连负 2 年: {f.get('ni_neg_2yr')}",
        f"PE {f.get('pe')}, 10 年均 PE {f.get('avg_pe_10y')}, 股息率 {f.get('div_yield')}%",
    ]
    return "; ".join(str(p) for p in parts)


def check_conditions_llm(text: str, ticker: str, fundamentals: Optional[dict],
                         label: str = "反证") -> list[dict]:
    """逐条解析条件并判断是否触发。LLM 不可用时返回空表。"""
    if not text or not text.strip():
        return []
    if not (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN):
        return []
    client = make_anthropic_client()
    if not client:
        return []

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "claude-sonnet-4-6")
    today = datetime.now().strftime("%Y-%m-%d")

    user_msg = (
        f"## 标的: {ticker}\n"
        f"## 评估日: {today}\n"
        f"## 类别: {label}\n\n"
        f"## 当前 fundamentals (EODHD)\n{_fmt_fundamentals_brief(fundamentals)}\n\n"
        f"## 用户写的{label}条件\n{text.strip()}\n"
    )

    try:
        resp = client.messages.create(
            model=model, max_tokens=1500,
            system=CONDITIONS_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return [{"idx": 0, "condition": f"LLM 调用失败: {e}", "verdict": "不可判断", "evidence": ""}]

    parsed = _parse_json_loose(out)
    if not parsed:
        return [{"idx": 0, "condition": f"LLM 输出非 JSON: {out[:80]}", "verdict": "不可判断", "evidence": ""}]
    items = parsed.get("items") or []
    return [
        {
            "idx": it.get("idx", i + 1),
            "condition": str(it.get("condition", "")),
            "verdict": str(it.get("verdict", "不可判断")),
            "evidence": str(it.get("evidence", "")),
        }
        for i, it in enumerate(items)
    ]


# ──────────────────────────────────────────────────────────────────────
# 5b. 财务质地对照 (LLM)
# ──────────────────────────────────────────────────────────────────────

FQ_SYSTEM = """你是基本面投资 review agent。用户写了一段定性的"财务质地"描述，里面可能提到净利率、毛利率、ROE、现金流、应收应付、负债率等指标。请对照当前 fundamentals 实际数据，逐项判断"用户的描述是否仍然成立"。

输出严格 JSON:
{
  "items": [
    {"aspect": "净利率", "user_claim": "用户原话片段", "actual": "实际数值", "verdict": "吻合|偏离|未提及|不可判断", "note": "≤30 字"},
    ...
  ],
  "overall": "财务质地整体仍稳定 / 有显著恶化迹象 / 数据不足 (≤40 字)"
}

判断准则：
- 吻合：实际数值在用户描述的方向 / 数量级
- 偏离：实际数值明确偏离用户描述
- 未提及：用户没提到这项，但 fundamentals 有
- 不可判断：fundamentals 缺失

只评用户文本里实际提到的或 fundamentals 里有数据的方面。不要凭空生成。
"""


def check_financial_quality(text: str, fundamentals: Optional[dict]) -> dict:
    """对照用户写的财务质地 vs EODHD 实际指标。"""
    if not text or not text.strip() or text.strip().startswith("_(未填写"):
        return {"items": [], "overall": "用户未填写财务质地"}
    if not (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN):
        return {"items": [], "overall": "未配置 LLM"}
    client = make_anthropic_client()
    if not client:
        return {"items": [], "overall": "LLM 客户端初始化失败"}

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "claude-sonnet-4-6")

    user_msg = (
        f"## 用户写的财务质地\n{text.strip()}\n\n"
        f"## 当前 fundamentals (EODHD)\n{_fmt_fundamentals_brief(fundamentals)}\n"
    )
    try:
        resp = client.messages.create(
            model=model, max_tokens=1200,
            system=FQ_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return {"items": [], "overall": f"LLM 调用失败: {e}"}

    parsed = _parse_json_loose(out)
    if not parsed:
        return {"items": [], "overall": f"LLM 输出非 JSON: {out[:80]}"}
    return {
        "items": parsed.get("items") or [],
        "overall": str(parsed.get("overall", "")),
    }


# ──────────────────────────────────────────────────────────────────────
# 5c. note body 段落抽取 (用于读 falsification / risks / financial_quality)
# ──────────────────────────────────────────────────────────────────────

_SECTION_HEADERS = {
    "financial_quality": "## 6. 财务质地",
    "falsification": "## 11. 反证 / 复盘信号",
    "risks": "## 12. 风险清单",
}


def extract_section(body: str, key: str) -> str:
    """从 12d note body 抽出指定 section 的纯文本（不含标题、不含后续 section）。"""
    header = _SECTION_HEADERS.get(key)
    if not header:
        return ""
    idx = body.find(header)
    if idx < 0:
        return ""
    # 从标题之后开始
    start = body.find("\n", idx)
    if start < 0:
        return ""
    # 找下一个 "## " 标题
    end = body.find("\n## ", start + 1)
    section = body[start + 1:end] if end >= 0 else body[start + 1:]
    section = section.strip()
    # 跳过 "_(未填写 ...)_" 占位
    if section.startswith("_(未填写"):
        return ""
    return section


# ──────────────────────────────────────────────────────────────────────
# 5d. LLM 归因（数值对照表）
# ──────────────────────────────────────────────────────────────────────

ATTRIBUTION_SYSTEM = """你是基本面投资 review agent。用户给你一份预测 vs 实际的对照表，请逐项给出简短归因。

输出严格 JSON:
{
  "overall_bias": "乐观|悲观|中性|数据不足",
  "items": [
    {"metric": "26 收入", "verdict": "✓|△|✗|?", "note": "一句话归因"},
    ...
  ],
  "next_steps": ["..."]
}

note 不超过 30 字，next_steps 列 1-3 条。
"""


def _attribute_errors(financial_rows: list, valuation: dict, catalysts: list) -> dict:
    """调 LLM 给出归因。失败时返回 fallback。"""
    if not (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN):
        return {"overall_bias": "数据不足", "items": [], "next_steps": ["未配置 LLM"]}

    client = make_anthropic_client()
    if not client:
        return {"overall_bias": "数据不足", "items": [], "next_steps": ["LLM 客户端初始化失败"]}

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "claude-sonnet-4-6")

    user_msg = "## 财务对照\n"
    for r in financial_rows:
        user_msg += (f"- {r['year']}: 收入预 {r['pred_revenue_yi']} / 实 {r['actual_revenue_yi']} ({r['verdict_revenue']}) | "
                     f"净利率预 {r['pred_net_margin']} / 实 {r['actual_net_margin']} ({r['verdict_margin']}) | "
                     f"净利润预 {r['pred_net_profit_yi']} / 实 {r['actual_net_profit_yi']} ({r['verdict_profit']})\n")
    user_msg += "\n## 估值\n"
    user_msg += f"当前市值 {valuation.get('current_mcap_yi')} 亿，base {valuation.get('base', {}).get('mcap_yi')} 亿，状态：{valuation.get('verdict')}\n"
    user_msg += "\n## 催化剂\n"
    for c in catalysts:
        user_msg += f"- [{c['status']}] {c['event']} (窗口 {c['window']})\n"

    try:
        resp = client.messages.create(
            model=model, max_tokens=1024,
            system=ATTRIBUTION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return {"overall_bias": "数据不足", "items": [], "next_steps": [f"LLM 调用失败: {e}"]}

    parsed = _parse_json_loose(text)
    if not parsed:
        return {"overall_bias": "数据不足", "items": [], "next_steps": [f"LLM 输出非 JSON: {text[:80]}"]}
    return parsed


def _parse_json_loose(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块或前后冗余文本）。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


# ──────────────────────────────────────────────────────────────────────
# 6. 报告输出
# ──────────────────────────────────────────────────────────────────────

def _fmt_v(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _build_report(ticker: str, note: dict, financial_rows: list, valuation: dict,
                  catalysts: list, long_term: dict, attribution: dict,
                  fq_check: dict, falsif_items: list, risks_items: list) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    fm = note["fm"]
    lines: list[str] = []
    lines.append("---")
    lines.append(f"ticker: {ticker}")
    lines.append(f"review_date: {today}")
    lines.append("template_version: review_v2")
    lines.append(f"source_note: {note['path'].name}")
    lines.append(f"overall_bias: {attribution.get('overall_bias', '?')}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {ticker} 复盘报告 v2 ({today})")
    lines.append("")
    lines.append(f"源 note: `{note['path'].name}` (创建于 {fm.get('created_at')})")
    lines.append("")

    # 1. 财务
    lines.append("## 1. 财务预测 vs 实际")
    lines.append("")
    if not financial_rows:
        lines.append("_无可对照的财务数据（实际年报未到 / 数据源缺失）_")
    else:
        lines.append("| 年份 | 指标 | 预测 | 实际 | 判定 |")
        lines.append("|---|---|---|---|---|")
        for r in financial_rows:
            lines.append(f"| {r['year']} | 收入(亿) | {_fmt_v(r['pred_revenue_yi'])} | {_fmt_v(r['actual_revenue_yi'])} | {r['verdict_revenue']} |")
            lines.append(f"| {r['year']} | 净利率   | {_fmt_v(r['pred_net_margin'])} | {_fmt_v(r['actual_net_margin'])} | {r['verdict_margin']} |")
            lines.append(f"| {r['year']} | 净利润(亿) | {_fmt_v(r['pred_net_profit_yi'])} | {_fmt_v(r['actual_net_profit_yi'])} | {r['verdict_profit']} |")
    lines.append("")

    # 2. 估值
    lines.append("## 2. 估值 vs 当前市值")
    lines.append("")
    lines.append(f"- 当前股价: {_fmt_v(valuation.get('current_price'))}")
    lines.append(f"- 总股本: {_fmt_v(valuation.get('shares_outstanding'))}")
    lines.append(f"- 当前市值: **{_fmt_v(valuation.get('current_mcap_yi'))} 亿**")
    if valuation.get("base"):
        b = valuation["base"]
        lines.append(f"- Base: {_fmt_v(b.get('mcap_yi'))} 亿 (PE {_fmt_v(b.get('pe'))} × 净利 {_fmt_v(b.get('profit_yi'))} 亿)")
    if valuation.get("bull"):
        lines.append(f"- Bull: {_fmt_v(valuation['bull'].get('mcap_yi'))} 亿 ({valuation['bull'].get('composition', '')})")
    if valuation.get("bear"):
        lines.append(f"- Bear: drop {_fmt_v(valuation['bear'].get('mcap_drop_pct'))} (触发: {valuation['bear'].get('trigger', '')})")
    lines.append(f"- **当前状态**: {valuation.get('verdict') or '—'}")
    lines.append("")

    # 3. 催化剂
    lines.append("## 3. 催化剂窗口检查")
    lines.append("")
    if not catalysts:
        lines.append("_无催化剂记录_")
    else:
        for c in catalysts:
            icon = {"已到期": "⏰", "未到期": "🕓", "无窗口": "·"}.get(c["status"], "?")
            lines.append(f"- {icon} **{c['status']}** | {c['event']}")
            lines.append(f"  - 窗口: {c['window']} (截止 {c['window_end'] or '?'})")
            if c.get("monitor"):
                lines.append(f"  - 监控点: {c['monitor']}")
    lines.append("")

    # 4. 远期空间
    lines.append("## 4. 远期空间（待跟踪）")
    lines.append("")
    if not long_term:
        lines.append("_无远期空间字段_")
    else:
        lines.append(f"- 时间锚: {long_term.get('horizon_year', '?')}")
        lines.append(f"- 总收入空间: {_fmt_v(long_term.get('total_rev_potential_yi'))} 亿")
        lines.append(f"- 隐含利润: {_fmt_v(long_term.get('implied_profit_yi'))} 亿")
        for seg in long_term.get("by_segment", []) or []:
            lines.append(f"  - {seg.get('name', '?')}: TAM {_fmt_v(seg.get('tam_yi'))} 亿，市占 {_fmt_v(seg.get('share_pct'))}，收入潜力 {_fmt_v(seg.get('rev_potential_yi'))} 亿")
    lines.append("")

    # 5. 财务质地对照
    lines.append("## 5. 财务质地对照（LLM）")
    lines.append("")
    fq_items = fq_check.get("items") or []
    if not fq_items:
        lines.append(f"_{fq_check.get('overall') or '无数据'}_")
    else:
        lines.append("| 方面 | 用户描述 | 实际 | 判定 | 备注 |")
        lines.append("|---|---|---|---|---|")
        for it in fq_items:
            lines.append(
                f"| {it.get('aspect', '?')} | {it.get('user_claim', '')[:30]} | "
                f"{it.get('actual', '')} | {it.get('verdict', '?')} | {it.get('note', '')} |"
            )
        if fq_check.get("overall"):
            lines.append("")
            lines.append(f"**整体**: {fq_check['overall']}")
    lines.append("")

    # 6. 反证检查
    lines.append("## 6. 反证条件检查（LLM）")
    lines.append("")
    if not falsif_items:
        lines.append("_用户未填写反证条件，或 LLM 不可用_")
    else:
        for it in falsif_items:
            icon = {"已触发": "🚨", "未触发": "✓", "不可判断": "?"}.get(it["verdict"], "?")
            lines.append(f"- {icon} **#{it['idx']} [{it['verdict']}]** {it['condition']}")
            if it.get("evidence"):
                lines.append(f"  - 依据: {it['evidence']}")
    lines.append("")

    # 7. 风险检查
    lines.append("## 7. 风险清单检查（LLM）")
    lines.append("")
    if not risks_items:
        lines.append("_用户未填写风险清单，或 LLM 不可用_")
    else:
        for it in risks_items:
            icon = {"已触发": "🚨", "未触发": "✓", "不可判断": "?"}.get(it["verdict"], "?")
            lines.append(f"- {icon} **#{it['idx']} [{it['verdict']}]** {it['condition']}")
            if it.get("evidence"):
                lines.append(f"  - 依据: {it['evidence']}")
    lines.append("")

    # 8. LLM 归因（数值层）
    lines.append("## 8. 数值归因（LLM）")
    lines.append("")
    lines.append(f"**总体倾向**: {attribution.get('overall_bias', '?')}")
    lines.append("")
    items = attribution.get("items") or []
    if items:
        for it in items:
            lines.append(f"- [{it.get('verdict', '?')}] {it.get('metric', '?')} — {it.get('note', '')}")
    next_steps = attribution.get("next_steps") or []
    if next_steps:
        lines.append("")
        lines.append("**下一步**:")
        for s in next_steps:
            lines.append(f"- {s}")
    lines.append("")

    return "\n".join(lines)


def write_report(ticker: str, note: dict, financial_rows: list, valuation: dict,
                 catalysts: list, long_term: dict, attribution: dict,
                 fq_check: dict, falsif_items: list, risks_items: list,
                 reviews_dir: Path = REVIEWS_DIR) -> Path:
    reviews_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = reviews_dir / f"{ticker}_{today}_v2.md"
    content = _build_report(ticker, note, financial_rows, valuation, catalysts, long_term,
                            attribution, fq_check, falsif_items, risks_items)
    out_path.write_text(content, encoding="utf-8")
    return out_path


# ──────────────────────────────────────────────────────────────────────
# 7. 主入口
# ──────────────────────────────────────────────────────────────────────

def do_review_v2(ticker: str, *, skip_llm: bool = False) -> Optional[Path]:
    """执行一次 v2 复盘。返回输出文件路径，失败返回 None。"""
    ticker = ticker.upper().strip()
    note = load_12d_note(ticker)
    if not note:
        print(f"[REVIEW2] {ticker} 无 12d_v1 模板的 note。请先 fa note -f 升级。")
        return None
    fm = note["fm"]
    print(f"[REVIEW2] 源 note: {note['path'].name} (创建于 {fm.get('created_at')})")

    forecast = fm.get("financial_forecast") or []
    target = fm.get("valuation_target") or {}
    catalysts_raw = fm.get("catalysts") or []
    long_term = fm.get("long_term_space") or {}

    print(f"[REVIEW2] 拉取 fundamentals ...")
    fund = fetch_fundamentals(ticker, with_benchmarks=False)

    print(f"[REVIEW2] 对照财务预测 ({len(forecast)} 年)")
    financial_rows = compare_financial(forecast, fund)

    print(f"[REVIEW2] 对照估值目标")
    valuation = compare_valuation(target, ticker, fund)

    print(f"[REVIEW2] 检查催化剂 ({len(catalysts_raw)} 条)")
    catalysts = check_catalysts(catalysts_raw)

    body = note["body"]
    fq_text = extract_section(body, "financial_quality")
    falsif_text = extract_section(body, "falsification")
    risks_text = extract_section(body, "risks")

    if skip_llm:
        attribution = {"overall_bias": "未调用 LLM", "items": [], "next_steps": []}
        fq_check = {"items": [], "overall": "未调用 LLM"}
        falsif_items = []
        risks_items = []
    else:
        print(f"[REVIEW2] LLM 财务质地对照 ({len(fq_text)} 字)")
        fq_check = check_financial_quality(fq_text, fund)
        print(f"[REVIEW2] LLM 反证条件检查 ({len(falsif_text)} 字)")
        falsif_items = check_conditions_llm(falsif_text, ticker, fund, label="反证")
        print(f"[REVIEW2] LLM 风险清单检查 ({len(risks_text)} 字)")
        risks_items = check_conditions_llm(risks_text, ticker, fund, label="风险")
        print(f"[REVIEW2] LLM 数值归因 ...")
        attribution = _attribute_errors(financial_rows, valuation, catalysts)

    out_path = write_report(ticker, note, financial_rows, valuation, catalysts,
                            long_term, attribution, fq_check, falsif_items, risks_items)
    print(f"[REVIEW2] ✓ 输出: {out_path}")
    return out_path
