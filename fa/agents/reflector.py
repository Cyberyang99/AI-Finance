"""Reflector Agent — 完整执行轨迹的根因诊断 + 生成候选情境笔记.

PDF 2 §2.2.1 设计 (Predictor → Critic → Reflector → Evolver):
  - Critic 给"对错"和评分
  - Reflector 在 Critic 之上做一层"为什么"分析：失败的根本原因是什么？
    我们的判断框架在这里漏掉了什么？这能转化成一条可复用的情境笔记吗？
  - 输出包括：
    1. 诊断报告（root_cause / pattern_type）
    2. 候选情境笔记列表（每条含 situation/retrieval_text/body/sector_scope）
  - 候选笔记不直接写盘，交给 ConflictResolver 决策 add/skip/replace/branch

设计原则：
  - 不要每次回顾都生成笔记。只在以下情况触发：
    1. 重大失败（excess_return < -10% 或 critic_score < 0.4）
    2. 重大成功（excess_return > +20% 或 critic_score > 0.85）—— 沉淀成功模式
    3. 预测高度偏差（实际 vs 预期偏离严重）
  - 中等表现（0.4-0.7）不强求笔记，避免噪声笔记泛滥
"""

import json
import re
from typing import Optional

from ..config import ANTHROPIC_KEY, ANTHROPIC_AUTH_TOKEN, load_config, make_anthropic_client


REFLECTOR_SYSTEM_PROMPT = """你是投资研究的反思员（Reflector）。你不评分，只做根因分析和经验沉淀。

## 你的任务

阅读一个论点的完整执行轨迹（论点 + 预测 + 实际结果 + Critic 评审），完成两件事：

### 1. 诊断报告 (diagnosis)
- **root_cause**: 失败/成功的根本原因。要找到「认知盲区」级别的因素，而不是表面现象。
- **pattern_type**: 这个案例反映的模式类型：`认知错误 / 框架缺陷 / 时点错误 / 运气 / 成功模式`
- **applies_beyond_this_ticker**: 这个教训能否泛化到其他个股/板块？(true/false + 理由)

### 2. 候选情境笔记 (candidate_notes)
若 applies_beyond_this_ticker=true，**最多生成 2 条**情境笔记。否则返回空列表。
**笔记必须可被未来分析复用**，不是事后归因复读。

每条笔记包含：
- `situation`: 30-80 字一句话情境描述（什么场景下应该警惕/应用）
- `retrieval_text`: 80-200 字检索文本，含触发条件 + 适用范围 + 关键观察点
- `body`: Markdown 正文，必须含三段：
  ```
  ## 经验总结
  （从这个案例提炼出的核心规律）

  ## 建议调整
  - 在哪个具体框架/检查项里加入什么
  - 在 deep 模式时应该额外做什么检查

  ## 例外分支
  （什么情况下这条规律不适用）
  ```
- `sector_scope`: 适用行业列表，可填 `['all']` 或具体 GICS 类别（如 `['Information Technology', 'Communication Services']`）
- `sector_excluded`: 不适用的行业列表
- `confidence`: 0.0-1.0，初始置信度（首次提炼建议 0.5-0.7）

## 输出格式

严格 JSON（无 markdown 包裹、无额外文本）：

```
{
  "diagnosis": {
    "root_cause": "...",
    "pattern_type": "...",
    "applies_beyond_this_ticker": true,
    "generalization_reason": "..."
  },
  "candidate_notes": [
    {
      "situation": "...",
      "retrieval_text": "...",
      "body": "## 经验总结\\n...\\n\\n## 建议调整\\n- ...\\n\\n## 例外分支\\n...",
      "sector_scope": ["all"],
      "sector_excluded": [],
      "confidence": 0.6
    }
  ]
}
```

## 重要原则

- **诚实**：不要为了产出笔记而硬凑。空 candidate_notes 是可以接受的。
- **具体**：笔记内容必须具体到可以被后续 Predictor 在 prompt 里用上。
- **可证伪**：经验里要明确"什么观察可以证伪它"，避免变成永不错的废话。
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


def _build_user_message(thesis: dict, performance: dict, prediction_results: list,
                       critic_output: dict, current_fundamentals: Optional[dict]) -> str:
    base_date = thesis.get("baseline_date") or "?"
    parts = [
        "## 论点轨迹",
        f"标的: {thesis.get('ticker')}",
        f"建立日: {base_date}",
        f"评估日: {performance.get('checkpoint_date', '?')}",
        f"持仓天数: {performance.get('days_held', '?')}",
        "",
        "## 核心论点（建立时）",
        thesis.get("thesis", "(无)")[:1500],
        "",
    ]

    # 预测注册表
    preds_raw = thesis.get("predictions", "[]")
    try:
        preds = json.loads(preds_raw) if isinstance(preds_raw, str) else preds_raw
    except Exception:
        preds = []
    if preds:
        parts.append("## 预测注册表（建立时）")
        for i, p in enumerate(preds, 1):
            parts.append(
                f"  {i}. {p.get('prediction', '')} | "
                f"预期 {p.get('expected', '')} | 截止 {p.get('deadline', '')}"
            )
        parts.append("")

    # 实际结果
    parts.extend([
        "## 实际市场表现",
        f"股价收益: {performance.get('stock_return', 0):+.2f}%",
        f"基准收益: {performance.get('index_return', 0):+.2f}%",
        f"**超额收益: {performance.get('excess_return', 0):+.2f}%**",
        f"客观得分: {performance.get('objective_score', '?')}",
        "",
    ])

    # 预测验证
    if prediction_results:
        parts.append("## 预测验证结果")
        for r in prediction_results:
            emoji = {"正确": "✓", "部分正确": "△", "错误": "✗", "无法验证": "?"}.get(r["result"], "?")
            parts.append(
                f"  {emoji} {r.get('prediction', '')[:80]} "
                f"(预期 {r.get('expected', '?')} → 实际 {r.get('actual', '?')})"
            )
        parts.append("")

    # Critic 输出
    if critic_output:
        parts.extend([
            "## Critic 评审结论",
            f"对了: {critic_output.get('what_worked', '(无)')}",
            f"错了: {critic_output.get('what_failed', '(无)')}",
            f"完整批评: {critic_output.get('critique', '')[:600]}",
            "",
        ])
        hints = critic_output.get("improvement_hints", [])
        if hints:
            parts.append("Critic 改进建议:")
            for h in hints:
                parts.append(f"  - {h}")
            parts.append("")

    # 当前基本面
    if current_fundamentals:
        parts.append("## 评估时基本面")
        for k in ["gross_margin", "net_margin", "roe", "revenue_cagr_3y", "debt_ratio", "pe", "sector"]:
            v = current_fundamentals.get(k)
            if v is not None:
                parts.append(f"  {k}: {v}")
        parts.append("")

    parts.append("请按 JSON 格式输出 diagnosis + candidate_notes（最多 2 条）。")
    return "\n".join(parts)


class ReflectorAgent:
    """完整执行轨迹反思员。"""

    # 触发反思的阈值（中等表现不强求笔记）
    SIGNIFICANT_FAIL_EXCESS = -10.0
    SIGNIFICANT_WIN_EXCESS = 20.0
    SIGNIFICANT_SCORE_LO = 0.4
    SIGNIFICANT_SCORE_HI = 0.85

    def __init__(self, model: str = None, max_tokens: int = 3000):
        cfg = load_config().get("agent", {})
        self.model = model or cfg.get("model", "deepseek-v4-pro")
        self.max_tokens = max_tokens
        self.client = make_anthropic_client() if (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN) else None

    def should_reflect(self, performance: dict, critic_output: dict) -> tuple[bool, str]:
        """判断是否值得触发反思（避免对中等表现强行造笔记）。

        返回 (should, reason)。
        """
        excess = performance.get("excess_return")
        score = critic_output.get("final_score") if critic_output else None
        if excess is None and score is None:
            return False, "无表现数据"

        if excess is not None:
            if excess <= self.SIGNIFICANT_FAIL_EXCESS:
                return True, f"重大失败 (超额 {excess:+.1f}%)"
            if excess >= self.SIGNIFICANT_WIN_EXCESS:
                return True, f"重大成功 (超额 {excess:+.1f}%)"
        if score is not None:
            if score <= self.SIGNIFICANT_SCORE_LO:
                return True, f"评分偏低 ({score})"
            if score >= self.SIGNIFICANT_SCORE_HI:
                return True, f"评分突出 ({score})"
        return False, "中等表现，不触发反思"

    def reflect(self, thesis: dict, performance: dict, prediction_results: list,
                critic_output: dict, current_fundamentals: Optional[dict] = None) -> dict:
        """执行反思，返回 {diagnosis, candidate_notes}。

        失败 graceful：返回空 candidate_notes + 错误说明。
        """
        if not self.client:
            return self._empty("Anthropic 客户端未初始化")

        user_msg = _build_user_message(thesis, performance, prediction_results,
                                       critic_output, current_fundamentals)

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=REFLECTOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            return self._empty(f"Reflector LLM 调用失败: {e}")

        parsed = _parse_json_loose(text)
        if not parsed:
            return self._empty(f"Reflector 输出非 JSON: {text[:120]}")

        diagnosis = parsed.get("diagnosis", {}) or {}
        candidates = parsed.get("candidate_notes", []) or []
        if not isinstance(candidates, list):
            candidates = []

        # 校验候选笔记必填字段
        valid = []
        for c in candidates[:2]:  # hard cap = 2
            if not isinstance(c, dict):
                continue
            if not (c.get("situation") and c.get("body")):
                continue
            valid.append({
                "situation": str(c.get("situation", "")).strip()[:200],
                "retrieval_text": str(c.get("retrieval_text", c.get("situation", ""))).strip()[:500],
                "body": str(c.get("body", "")).strip(),
                "sector_scope": c.get("sector_scope") or ["all"],
                "sector_excluded": c.get("sector_excluded") or [],
                "confidence": float(c.get("confidence", 0.6)),
            })

        return {
            "diagnosis": diagnosis,
            "candidate_notes": valid,
            "error": None,
        }

    def _empty(self, reason: str) -> dict:
        return {"diagnosis": {}, "candidate_notes": [], "error": reason}
