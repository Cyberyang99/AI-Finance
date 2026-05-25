"""Critic Agent — 独立评审员，对 Predictor 的论点做诚实评分.

设计要点（PDF 1 §2.2.1 + §2.3.2）:
  1. **独立 LLM 调用** — 不复用 Predictor 的 prompt/上下文，防止自我循环
  2. **客观锚定** — LLM score 被程序 clamp 到 [objective-0.2, objective+0.2]，
     防止 LLM 美化（PDF: 0.7 × objective + 0.3 × LLM_score）
  3. **结构化输出** — JSON: {score, what_worked, what_failed, improvement_hints, critique}
  4. **失败 graceful** — API 出错时退化为纯客观评分，不阻塞 review 流程

LLM 不应被告知 clamp 机制，否则它会故意偏移。clamp 是程序后处理。
"""

import json
import re
from typing import Optional

import anthropic

from ..config import ANTHROPIC_KEY, ANTHROPIC_AUTH_TOKEN, load_config, make_anthropic_client


# ─────────────────────────────────────────────────────────────
# System Prompt — Critic 的"独立评审员"角色定位
# ─────────────────────────────────────────────────────────────

CRITIC_SYSTEM_PROMPT = """你是独立的投资论点评审员（Critic）。你不做新预测，只评审已有论点的对错。

## 你的输入
- 一个 Predictor 早先建立的投资论点（包含核心论点、预测注册、风险信号）
- 论点建立时的基本面快照
- 论点期间的实际市场表现（股价收益、基准收益、超额收益）
- 各条预测的验证结果（哪些对、哪些错、哪些无法验证）

## 你的任务
1. **诚实评分**：给这个论点 0.0-1.0 的得分。客观市场表现是主要参考。
2. **what_worked**：论点中哪些判断被市场/数据证明是对的。要具体。
3. **what_failed**：论点中哪些判断错了，错在哪里（认知错误 vs 时点错误 vs 运气）。
4. **improvement_hints**：可操作的改进建议（列表）。不要写"加强分析"这种废话，要写具体到框架/检查项级别的修改。
5. **critique**：完整批评（200-400字），整合上述要素。

## 评分参考
- 客观表现是主要锚：大幅跑赢→0.8+，跑赢→0.6-0.8，持平→0.4-0.6，跑输→0.2-0.4，大幅跑输→0.0-0.2
- 主观加成：如果论点提到的风险/反证条件确实兑现了（说明Predictor有预见性），可在客观分基础上加 0.1
- 主观扣分：如果预测大量"无法验证"或论点空泛，扣 0.1

## 输出格式
严格 JSON（不要任何额外文字、不要 markdown 代码块标记）：
{
  "score": 0.0-1.0,
  "what_worked": "1-3 句具体描述",
  "what_failed": "1-3 句具体描述",
  "improvement_hints": ["建议1", "建议2", "建议3"],
  "critique": "200-400字完整批评"
}
"""


def _build_user_message(thesis: dict, performance: dict, prediction_results: list,
                       current_fundamentals: Optional[dict] = None) -> str:
    """组装 Critic 的 User Message。"""
    base = thesis.get("baseline", {}) if "baseline" in thesis else {
        "date": thesis.get("baseline_date"),
        "price": thesis.get("baseline_price"),
        "index": thesis.get("baseline_index"),
        "index_name": thesis.get("baseline_index_name"),
    }

    # 解析 key_metrics（论点建立时的快照）
    km = thesis.get("key_metrics", {})
    if isinstance(km, str):
        try:
            km = json.loads(km)
        except Exception:
            km = {}

    parts = [
        f"## 论点信息",
        f"标的: {thesis.get('ticker')}",
        f"行业: {km.get('sector', '未知')}",
        f"建立日期: {base.get('date', '?')}",
        f"",
        f"## 核心论点",
        thesis.get("thesis", "(无)"),
        f"",
    ]

    # 预测注册表
    preds_raw = thesis.get("predictions", "[]")
    try:
        preds = json.loads(preds_raw) if isinstance(preds_raw, str) else preds_raw
    except Exception:
        preds = []
    if preds:
        parts.append("## 当时建立的预测注册表")
        for i, p in enumerate(preds, 1):
            parts.append(
                f"  {i}. {p.get('prediction', '')} | "
                f"指标={p.get('metric', '')} 预期={p.get('expected', '')} "
                f"截止={p.get('deadline', '')} 置信度={p.get('confidence', '')}"
            )
        parts.append("")

    # 风险信号
    rf_raw = thesis.get("risk_flags", "[]")
    try:
        rfs = json.loads(rf_raw) if isinstance(rf_raw, str) else rf_raw
    except Exception:
        rfs = []
    if rfs:
        parts.append(f"## 当时识别的风险信号")
        for f in rfs:
            parts.append(f"  - {f}")
        parts.append("")

    # 论点建立时的关键指标快照
    if km:
        parts.append(f"## 论点建立时基本面快照")
        for k, v in km.items():
            if k != "sector":
                parts.append(f"  {k}: {v}")
        parts.append("")

    # 客观市场表现
    parts.extend([
        f"## 实际市场表现",
        f"持仓天数: {performance.get('days_held', '?')} 天",
        f"基线日: {base.get('date', '?')} → 评估日: {performance.get('checkpoint_date', '?')}",
        f"股价: {base.get('price', '?')} → {performance.get('current_price', '?')} "
        f"({performance.get('stock_return', 0):+.2f}%)",
        f"{base.get('index_name', '基准')}: {base.get('index', '?')} → "
        f"{performance.get('current_index', '?')} ({performance.get('index_return', 0):+.2f}%)",
        f"**超额收益: {performance.get('excess_return', 0):+.2f}%** ({performance.get('verdict', '?')})",
        f"客观得分（程序计算）: {performance.get('objective_score', '?')}",
        f"",
    ])

    # 预测验证结果
    if prediction_results:
        parts.append(f"## 预测验证结果")
        for r in prediction_results:
            emoji = {"正确": "✓", "部分正确": "△", "错误": "✗", "无法验证": "?"}.get(r["result"], "?")
            parts.append(
                f"  {emoji} {r.get('prediction', '')[:80]}\n"
                f"     预期: {r.get('expected', '?')} → 实际: {r.get('actual', '?')} ({r['result']})"
            )
        parts.append("")

    # 当前基本面（如果有）
    if current_fundamentals:
        parts.append(f"## 评估时基本面")
        for k in ["gross_margin", "net_margin", "roe", "revenue_cagr_3y", "debt_ratio", "pe"]:
            v = current_fundamentals.get(k)
            if v is not None:
                parts.append(f"  {k}: {v}")
        parts.append("")

    parts.append("请按要求输出 JSON 评审结果。")
    return "\n".join(parts)


def _clamp_score(llm_score: Optional[float], objective_score: Optional[float],
                 max_deviation: float = 0.2) -> Optional[float]:
    """客观锚定：LLM 评分只能在客观分上下 ±0.2 浮动。

    防止 LLM 美化（如客观跑输给 0.8 高分）或过度悲观。
    """
    if llm_score is None:
        return None
    if objective_score is None:
        return max(0.0, min(1.0, llm_score))
    lo = max(0.0, objective_score - max_deviation)
    hi = min(1.0, objective_score + max_deviation)
    return round(max(lo, min(hi, llm_score)), 3)


def _parse_json_loose(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块包裹或前后多余文本）。"""
    if not text:
        return None
    # 优先匹配 markdown JSON 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 退而求其次：找第一个 { 和最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


class CriticAgent:
    """独立 Critic Agent — 评审 Predictor 论点。"""

    def __init__(self, model: str = None, max_tokens: int = 2048):
        cfg = load_config().get("agent", {})
        self.model = model or cfg.get("model", "claude-sonnet-4-6")
        self.max_tokens = max_tokens
        self.client = make_anthropic_client() if (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN) else None

    def critique(self, thesis: dict, performance: dict, prediction_results: list,
                 current_fundamentals: Optional[dict] = None) -> dict:
        """调用 Critic 评审一个论点。

        失败时退化为纯客观评分 + 通用 critique（保证 review 流程不中断）。

        返回 dict:
          {
            "critic_score": float,        # 锚定后的 LLM 评分
            "raw_llm_score": float,       # LLM 原始评分（未锚定）
            "final_score": float,         # 综合 0.7×obj + 0.3×critic_score
            "what_worked": str,
            "what_failed": str,
            "improvement_hints": list[str],
            "critique": str,
            "anchor_adjusted": bool,      # 是否被 clamp 锚定调整过
          }
        """
        objective_score = performance.get("objective_score")

        # 客户端没初始化（API key 缺失）→ 直接退化
        if not self.client:
            return self._fallback(objective_score, "ANTHROPIC_API_KEY 未设置")

        user_msg = _build_user_message(thesis, performance, prediction_results,
                                        current_fundamentals)

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=CRITIC_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            return self._fallback(objective_score, f"Critic LLM 调用失败: {e}")

        parsed = _parse_json_loose(text)
        if not parsed:
            return self._fallback(objective_score, f"Critic 输出非 JSON: {text[:120]}")

        raw_score = parsed.get("score")
        try:
            raw_score = float(raw_score) if raw_score is not None else None
        except Exception:
            raw_score = None

        clamped = _clamp_score(raw_score, objective_score)
        adjusted = (raw_score is not None and clamped is not None and
                    abs(raw_score - clamped) > 0.001)

        # 综合得分：0.7 × objective + 0.3 × clamped LLM
        final = None
        if objective_score is not None and clamped is not None:
            final = round(0.7 * objective_score + 0.3 * clamped, 3)
        elif objective_score is not None:
            final = objective_score

        hints = parsed.get("improvement_hints", [])
        if isinstance(hints, str):
            hints = [hints]

        return {
            "critic_score": clamped,
            "raw_llm_score": raw_score,
            "final_score": final,
            "what_worked": str(parsed.get("what_worked", "")),
            "what_failed": str(parsed.get("what_failed", "")),
            "improvement_hints": [str(h) for h in hints],
            "critique": str(parsed.get("critique", "")),
            "anchor_adjusted": adjusted,
        }

    def _fallback(self, objective_score: Optional[float], reason: str) -> dict:
        """Critic 失败时退化为纯客观评分。"""
        return {
            "critic_score": None,
            "raw_llm_score": None,
            "final_score": objective_score,
            "what_worked": "",
            "what_failed": "",
            "improvement_hints": [],
            "critique": f"[Critic 跳过] {reason}",
            "anchor_adjusted": False,
        }
