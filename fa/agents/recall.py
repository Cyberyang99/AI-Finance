"""Recall Agent — 情境记忆召回.

设计 (PDF 1 §2.2.3):
  - 起步阶段笔记 < 100 条，直接 LLM 全量判断比 Embedding 准
  - 输入: 当前股票/任务上下文 + MEMORY.md 索引
  - 输出: Top-K 相关笔记 id list
  - 召回后由 do_deep 加载完整 body 并拼到 Predictor prompt

为什么不用 Embedding 起步:
  PDF 实证: "由于技术分析下笔记数量有限，直接 LLM 自行全量判断的效果最好"
  → 我们起步同理。等笔记 > 100 条再加 embedding 加速。
"""

import json
import re
from typing import Optional

from ..config import ANTHROPIC_KEY, ANTHROPIC_AUTH_TOKEN, load_config, make_anthropic_client


RECALL_SYSTEM_PROMPT = """你是情境记忆召回员。你的任务是从笔记索引中选出与当前任务最相关的 Top-K 条笔记。

## 召回准则
1. **行业匹配优先**: 笔记 sector_scope 包含当前股票行业，或为 'all'，才考虑召回
2. **情境相似度**: 笔记的 retrieval_text 描述的触发条件是否可能在当前任务中出现
3. **优先高置信度**: 置信度 ≥ 0.6 的笔记优先
4. **冷启动友好**: 实在不相关，宁可少召回（返回空列表），不要硬凑数

## 输出格式
严格 JSON（不要 markdown 代码块）：
{
  "selected": [
    {"id": "<note_id>", "reason": "为什么相关（一句话）"},
    ...
  ]
}

最多返回 K 条。如果没有相关的就返回 {"selected": []}。
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


class RecallAgent:
    """情境记忆召回。"""

    def __init__(self, model: str = None, max_tokens: int = 1024):
        cfg = load_config().get("agent", {})
        self.model = model or cfg.get("model", "deepseek-v4-pro")
        self.max_tokens = max_tokens
        self.client = make_anthropic_client() if (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN) else None

    def recall(self, query_context: dict, memory_index: str, top_k: int = 5) -> list[dict]:
        """召回最相关的 Top-K 笔记。

        query_context: {
            "ticker": "600519.SHG",
            "name": "贵州茅台",
            "sector": "Consumer Staples",
            "task": "deep|scan",
            "highlights": "可选: 关键特征摘要（如基本面快照、风险点）",
        }
        memory_index: SituationStore.read_index() 的内容

        返回 list[{"id": str, "reason": str}]. 召回失败返回空列表（不阻塞）。
        """
        if not self.client:
            return []
        if not memory_index or "暂无笔记" in memory_index:
            return []

        user_msg = self._build_user_message(query_context, memory_index, top_k)

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=RECALL_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            print(f"  [RECALL] 召回失败 (graceful): {e}")
            return []

        parsed = _parse_json_loose(text)
        if not parsed or "selected" not in parsed:
            return []

        selected = parsed["selected"][:top_k]
        # 过滤掉非 dict 或缺 id 的项
        return [s for s in selected if isinstance(s, dict) and s.get("id")]

    def _build_user_message(self, ctx: dict, index: str, top_k: int) -> str:
        parts = [
            "## 当前任务上下文",
            f"标的: {ctx.get('ticker', '?')} ({ctx.get('name', '')})",
            f"行业: {ctx.get('sector', '未知')}",
            f"任务: {ctx.get('task', 'deep')}",
        ]
        if ctx.get("highlights"):
            parts.extend([
                "",
                "### 关键特征",
                ctx["highlights"],
            ])

        parts.extend([
            "",
            "## 笔记索引 (MEMORY.md)",
            index,
            "",
            f"请从上面索引中挑出 Top-{top_k} 条与当前任务最相关的笔记，"
            f"输出 JSON。无相关则返回 {{\"selected\": []}}。",
        ])
        return "\n".join(parts)
