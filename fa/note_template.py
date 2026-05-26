"""个股投资逻辑笔记模板 — 12 维度通用版.

设计原则：
  1. 通用：12 维度跨行业都能填（制造/消费/医药/金融/TMT）
  2. 不强制：任何维度都允许留空；行业不适用的直接跳过
  3. 量化字段结构化：forecast / valuation / catalysts 都用 JSON 块，便于 review 机器解析
  4. note 和 deep 共用此模板

行业特化（后续可扩展）：
  - 制造业（参考豪迈）：business_breakdown 强调收入拆分 + 下游景气
  - 创新药：business_breakdown 改为 pipeline（在研管线）
  - 银行：financial_quality 强调 NIM / 不良率 / 拨备
  - 大模型公司：financial_forecast 强调 ARR / token 单价 / 算力成本
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass
from typing import Optional


# ── 12 维度定义 ──
# is_json=True 的字段在 frontmatter 里是 yaml block，body 里渲染为 ```json fence
DIMENSIONS: list[dict] = [
    {"id": "core_thesis",           "name": "核心论点",          "is_json": False, "weight": 10,
     "hint": "一两句话总结：为什么看好/看坏，最核心的判断"},
    {"id": "business_breakdown",    "name": "业务结构",          "is_json": False, "weight": 8,
     "hint": "公司主业拆解 — 各业务线收入占比、增速、下游、客户、产品/服务定位"},
    {"id": "market_position",       "name": "行业地位与竞争格局", "is_json": False, "weight": 8,
     "hint": "市占率、龙一龙二份额、对手动态、行业 TAM/SAM、专家点评"},
    {"id": "moat",                  "name": "护城河 / 竞争优势",  "is_json": False, "weight": 9,
     "hint": "客户锁定 / 规模效应 / 技术 / 政府关系 / 内部管理 / 文化 — 5 项分析"},
    {"id": "management_governance", "name": "管理层与治理",      "is_json": False, "weight": 6,
     "hint": "创始人背景、关键决策、激励机制、股权结构、历史融资与分红"},
    {"id": "financial_quality",     "name": "财务质地",          "is_json": False, "weight": 7,
     "hint": "毛利率/净利率/ROE/ROIC 趋势、现金流质量、应收应付/库存、上下游话语权"},
    {"id": "financial_forecast",    "name": "盈利预测",          "is_json": True,  "weight": 9,
     "hint": "分年度（含分业务线）的收入/净利润/净利率预测"},
    {"id": "long_term_space",       "name": "远期空间",          "is_json": True,  "weight": 8,
     "hint": "3-5 年远期：TAM × 份额 × 净利率 = 远期收入/利润；分业务线拆"},
    {"id": "valuation_target",     "name": "估值与目标价",        "is_json": True,  "weight": 9,
     "hint": "base/bull/bear 三种情境：PE × 利润 = 市值 → 上涨/下跌空间"},
    {"id": "catalysts",             "name": "催化剂 / 关键时点",  "is_json": True,  "weight": 7,
     "hint": "未来 12 个月待发生的关键事件 + 预期窗口 + 监控数据源"},
    {"id": "falsification",         "name": "反证 / 复盘信号",    "is_json": False, "weight": 8,
     "hint": "出现什么具体可观察的指标 → 证伪当前论点"},
    {"id": "risks",                 "name": "风险清单",          "is_json": False, "weight": 7,
     "hint": "每个风险的触发条件 + 量化的下跌空间"},
]

DIM_IDS = [d["id"] for d in DIMENSIONS]
JSON_DIM_IDS = [d["id"] for d in DIMENSIONS if d["is_json"]]


# ── JSON Schema 示例（喂给 LLM 当 few-shot） ──
JSON_SCHEMA_EXAMPLES = {
    "financial_forecast": [
        {"year": 2026, "revenue_yi": 127, "net_margin": 0.22, "net_profit_yi": 28, "growth_yoy": 0.20,
         "by_segment": [
            {"name": "轮胎模具", "revenue_yi": 65},
            {"name": "燃机铸件", "revenue_yi": 13, "growth_yoy": 0.30},
         ]}
    ],
    "long_term_space": {
        "horizon_year": 2030,
        "by_segment": [
            {"name": "燃机铸件", "tam_yi": 50, "share_pct": 0.30, "rev_potential_yi": 20},
            {"name": "电硫化机", "tam_yi": 30, "share_pct": 0.50, "rev_potential_yi": 15},
        ],
        "total_rev_potential_yi": 200,
        "implied_profit_yi": 46,
    },
    "valuation_target": {
        "base":  {"pe": 20, "profit_yi": 28, "mcap_yi": 560, "upside_pct": -0.13},
        "bull":  {"composition": "20x×26e + 30x×8e", "mcap_yi": 760, "upside_pct": 0.18},
        "bear":  {"trigger": "Q4 业绩 miss", "mcap_drop_pct": -0.05},
    },
    "catalysts": [
        {"event": "燃机厂商扩产计划上修", "window": "2026Q3", "monitor": "GEV/西门子季报"},
        {"event": "热端零件订单首次落地", "window": "2026H2", "monitor": "公司公告"},
    ],
}


def empty_payload() -> dict:
    """生成一个空模板 payload，所有字段都是空字符串/空列表。"""
    return {d["id"]: ([] if d["is_json"] else "") for d in DIMENSIONS}


def is_filled(value) -> bool:
    """判断一个字段是不是真的有内容（不是空字符串/空数组）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def filled_dims(payload: dict) -> list[str]:
    """返回 payload 里有内容的维度 id 列表。"""
    return [d["id"] for d in DIMENSIONS if is_filled(payload.get(d["id"]))]


# ── markdown 渲染 ──

def _render_json_block(value) -> str:
    """渲染 JSON 字段：fenced code block。"""
    if not is_filled(value):
        return "_(未填写)_"
    try:
        return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"
    except Exception:
        return f"```\n{value}\n```"


def render_markdown(
    ticker: str,
    payload: dict,
    *,
    sector: Optional[str] = None,
    tags: Optional[list] = None,
    created_at: str = "",
    user_comment: str = "",
    source_doc: str = "",
    source: str = "user",
    weight: float = 2.0,
    confidence: str = "high",
) -> str:
    """把 payload 渲染成一份完整 markdown 文档（含 frontmatter）。

    JSON 字段同时写到 frontmatter（机器可读）和 body（人可读）。
    """
    tags_list = [t.strip() for t in (tags or []) if t and t.strip()]
    fm_lines = [
        "---",
        f"ticker: {ticker}",
        f"sector: {sector or ''}",
    ]
    if tags_list:
        fm_lines.append(f"tags: [{', '.join(tags_list)}]")
    fm_lines.extend([
        f"source: {source}",
    ])
    if source_doc:
        fm_lines.append(f"source_doc: {source_doc}")
    if user_comment:
        fm_lines.append(f"user_comment: {user_comment.strip().replace(chr(10), ' ')}")
    fm_lines.extend([
        f"created_at: {created_at}",
        f"weight: {weight}",
        f"confidence: {confidence}",
        f"template_version: 12d_v1",
    ])

    # JSON 字段拍平进 frontmatter (yaml inline)，便于 review 机器解析
    for d in DIMENSIONS:
        if not d["is_json"]:
            continue
        v = payload.get(d["id"])
        if not is_filled(v):
            continue
        try:
            # JSON 也是合法 YAML，直接 inline 进 frontmatter
            fm_lines.append(f"{d['id']}: " + json.dumps(v, ensure_ascii=False))
        except Exception:
            pass

    fm_lines.append("---")

    body_lines = ["", f"# {ticker} — 投资逻辑笔记 ({created_at})", ""]

    if user_comment:
        body_lines.extend([
            "## 🗨 用户角度提示（主观锚点）",
            "",
            user_comment.strip(),
            "",
        ])

    filled = filled_dims(payload)
    body_lines.append(f"**已填维度**: {len(filled)} / 12")
    if filled:
        body_lines.append(f"  → {', '.join(filled)}")
    body_lines.append("")

    for i, d in enumerate(DIMENSIONS, 1):
        v = payload.get(d["id"])
        body_lines.append(f"## {i}. {d['name']}")
        body_lines.append("")
        if not is_filled(v):
            body_lines.append(f"_(未填写 — {d['hint']})_")
        elif d["is_json"]:
            body_lines.append(_render_json_block(v))
        else:
            body_lines.append(str(v).strip())
        body_lines.append("")

    return "\n".join(fm_lines + body_lines)


# ── 从 frontmatter 反向解析 ──

def parse_frontmatter(text: str) -> dict:
    """从一份 12 维度 note 文件读 frontmatter，返回 dict（JSON 字段已反序列化）。"""
    import yaml as _yaml
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = _yaml.safe_load(parts[1])
    except Exception:
        return {}
    return fm or {}
