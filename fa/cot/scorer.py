"""CoT Scorer — 单条思维链对单只股票的符合度判定 (LLM).

PDF1 §2.2 设计:
- 输出三档: 不符合 / 较符合 / 完全符合
- 配合置信度 0-100
- 平均: 完全符合 4.13%, 较符合 51.12%, 不符合 44.75%
- 持仓策略: 完全符合全部纳入；较符合按置信度排序；不符合剔除
"""

import json
import re
from typing import Optional

from ..config import ANTHROPIC_KEY, ANTHROPIC_AUTH_TOKEN, load_config, make_anthropic_client

MATCH_LEVELS = ("不符合", "较符合", "完全符合")
MATCH_VALUE = {"不符合": 0, "较符合": 0.5, "完全符合": 1.0}


SCORER_SYSTEM_PROMPT = """你是基于投资思维链做个股符合度判断的分析师。

## 输入
- 1 条投资思维链（trigger + 推理链 + 信号强度）
- 1 只股票的基本面数据快照（含行业、增速、利润率、ROE、负债等）
- 可选的最新业务进展信息

## 任务
判断这只股票当下是否符合这条思维链描述的情境。

## 输出三档判定
- **完全符合**: 思维链描述的核心驱动因素在该股票上已有强证据，传导路径已开始兑现
- **较符合**: 部分驱动因素具备，但还有关键环节未验证或证据不足
- **不符合**: 思维链不适用，或核心驱动因素明显缺失

## 输出格式
严格 JSON（无 markdown 包裹）:
```
{
  "match": "完全符合" | "较符合" | "不符合",
  "confidence": 0-100,
  "evidence": "1-3 个关键证据点的具体描述",
  "missing": "什么关键证据缺失（如有）"
}
```

## 原则
- 不要为了产出"完全符合"硬凑，宁可"较符合"也要诚实
- evidence 必须引用具体数字或事实，不要泛泛而谈
- 如果信息明显不足以判断，confidence 给低值（< 40）并说明 missing
"""


def _parse_json_loose(text: str) -> Optional[dict]:
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


def _build_user_message(cot: dict, stock_data: dict, news: Optional[str]) -> str:
    parts = [
        "## 投资思维链",
        f"驱动因素: {cot.get('trigger', '')}",
        f"信号强度: {cot.get('signal', '?')}/10",
        f"推理链: {cot.get('COT', '')}",
        "",
        "## 股票基本面快照",
        f"代码: {stock_data.get('ticker', '?')}",
        f"名称: {stock_data.get('name', '?')}",
        f"行业: {stock_data.get('sector', '?')}",
    ]

    metrics = [
        ("市值 (亿)", "market_cap_yi" if stock_data.get("market_cap_yi") else "market_cap"),
        ("营收增速 3y CAGR (%)", "revenue_cagr_3y"),
        ("毛利率 (%)", "gross_margin"),
        ("净利率 (%)", "net_margin"),
        ("ROE (%)", "roe"),
        ("负债率 (%)", "debt_ratio"),
        ("PE", "pe"),
        ("股息率 (%)", "div_yield"),
    ]
    for label, key in metrics:
        v = stock_data.get(key)
        if v is not None:
            parts.append(f"  {label}: {v}")

    # 行业基准（如果有）
    if stock_data.get("gross_margin_p50") is not None:
        parts.append(f"  毛利率行业中位数: {stock_data['gross_margin_p50']}%")
    if stock_data.get("roe_p50") is not None:
        parts.append(f"  ROE 行业中位数: {stock_data['roe_p50']}%")

    if news:
        parts.extend(["", "## 最新业务进展 / 新闻", news[:2000]])

    parts.extend(["", "请按 JSON 格式输出 match / confidence / evidence / missing。"])
    return "\n".join(parts)


class CotScorer:
    """单 CoT 对单股的 LLM 判定器。"""

    def __init__(self, model: str = None, max_tokens: int = 800):
        cfg = load_config().get("agent", {})
        self.model = model or cfg.get("model", "deepseek-v4-pro")
        self.max_tokens = max_tokens
        self.client = make_anthropic_client() if (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN) else None

    def score(self, cot: dict, stock_data: dict, news: Optional[str] = None) -> dict:
        """返回 {match, confidence, evidence, missing, _cot_id, _trigger, _signal}.

        失败 graceful：match=不符合, confidence=0, evidence/missing 写明 LLM 失败原因。
        """
        if not self.client:
            return self._fallback(cot, "Anthropic 客户端未初始化")

        user_msg = _build_user_message(cot, stock_data, news)

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=SCORER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            return self._fallback(cot, f"LLM 失败: {e}")

        parsed = _parse_json_loose(text)
        if not parsed:
            return self._fallback(cot, f"输出非 JSON: {text[:100]}")

        match = str(parsed.get("match", "不符合")).strip()
        if match not in MATCH_LEVELS:
            match = "不符合"

        try:
            conf = int(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        conf = max(0, min(100, conf))

        return {
            "match": match,
            "match_value": MATCH_VALUE[match],
            "confidence": conf,
            "evidence": str(parsed.get("evidence", ""))[:600],
            "missing": str(parsed.get("missing", ""))[:300],
            "_cot_id": cot.get("_cot_id", ""),
            "_trigger": cot.get("trigger", ""),
            "_signal": cot.get("signal", "?"),
        }

    def _fallback(self, cot: dict, reason: str) -> dict:
        return {
            "match": "不符合",
            "match_value": 0,
            "confidence": 0,
            "evidence": "",
            "missing": reason,
            "_cot_id": cot.get("_cot_id", ""),
            "_trigger": cot.get("trigger", ""),
            "_signal": cot.get("signal", "?"),
        }


# 模块级便利函数
_default_scorer = None


def score_cot_against_stock(cot: dict, stock_data: dict, news: Optional[str] = None) -> dict:
    global _default_scorer
    if _default_scorer is None:
        _default_scorer = CotScorer()
    return _default_scorer.score(cot, stock_data, news)
