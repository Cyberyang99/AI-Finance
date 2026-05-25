"""ConflictResolver — 候选新笔记 vs 现有笔记池的冲突判定.

PDF 2 §2.2.4 设计：
  - 新笔记入库前不直接写盘，先做冲突检测
  - 决策：add / skip / replace / branch
    - add: 新情境，无重叠 → 直接写入
    - skip: 被现有笔记覆盖，冗余 → 丢弃
    - replace: 同一情境但新笔记更完整 → 新笔记替换旧笔记（旧归档）
    - branch: 同一情境但条件分支不同 → 新笔记写入，标记例外分支

起步阶段笔记 < 100 条，直接 LLM 全量判断比 embedding 准（PDF 实测）。
后续笔记多了再加 embedding + Top-K 候选剪枝。
"""

import json
import re
from typing import Optional

from ..config import ANTHROPIC_KEY, ANTHROPIC_AUTH_TOKEN, load_config, make_anthropic_client


CONFLICT_SYSTEM_PROMPT = """你是情境笔记冲突仲裁员。你的任务是判断一条新候选笔记与现有笔记池的关系。

## 输入
- 1 条候选新笔记（situation + retrieval_text + body）
- N 条现有笔记的元信息（id + situation + retrieval_text + sector_scope）

## 决策选项
- **add**: 新情境，没有任何现有笔记覆盖这个角度 → 直接写入
- **skip**: 现有笔记已经完整覆盖了这个情境，新笔记无新信息 → 丢弃
- **replace**: 与某条现有笔记是同一情境，但新笔记更完整/更准确 → 替换该笔记
- **branch**: 与某条现有笔记是同一情境，但反映了不同的条件分支（例外情况） → 在该笔记下追加例外分支

## 判定要点
1. "同一情境" 不是 "同一行业"，而是 "同一类触发条件 + 类似的判断逻辑"
2. 一条新笔记如果只是对现有笔记的微小补充，倾向 branch 而非 replace
3. replace 要慎用：必须新笔记在表达完整度、举例、可证伪性上明显优于旧笔记

## 输出格式

严格 JSON（无 markdown 包裹）：

```
{
  "decision": "add" | "skip" | "replace" | "branch",
  "target_id": "...",      // replace/branch 时必填 = 对应的现有笔记 id；add/skip 留空
  "reason": "一句话理由"
}
```

## 注意

- 当现有笔记池为空时，**必须**返回 `decision: "add"`
- 不能输出除以上四个之外的 decision 值
- replace/branch 时 target_id 必须是输入中真实存在的 id
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


def _build_user_message(candidate: dict, existing_notes: list[dict]) -> str:
    parts = [
        "## 候选新笔记",
        f"- 情境: {candidate.get('situation', '')}",
        f"- 检索文本: {candidate.get('retrieval_text', '')}",
        f"- 适用行业: {', '.join(candidate.get('sector_scope', ['all']))}",
        "",
        "### 新笔记正文",
        candidate.get("body", ""),
        "",
        "---",
        "",
    ]

    if not existing_notes:
        parts.extend(["## 现有笔记池", "（空，没有任何已有笔记）", ""])
    else:
        parts.append(f"## 现有笔记池 ({len(existing_notes)} 条)")
        for n in existing_notes:
            sectors = ", ".join(n.get("sector_scope", ["all"]))
            parts.append(
                f"### id=`{n['id']}`\n"
                f"- 情境: {n.get('situation', '')}\n"
                f"- 检索文本: {n.get('retrieval_text', '')}\n"
                f"- 适用: {sectors} | 置信: {n.get('confidence', 0.5)}"
            )
        parts.append("")

    parts.append("请按 JSON 格式输出 decision / target_id / reason。")
    return "\n".join(parts)


class ConflictResolver:
    """情境笔记冲突仲裁。"""

    def __init__(self, model: str = None, max_tokens: int = 600):
        cfg = load_config().get("agent", {})
        self.model = model or cfg.get("model", "deepseek-v4-pro")
        self.max_tokens = max_tokens
        self.client = make_anthropic_client() if (ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN) else None

    def resolve(self, candidate: dict, existing_notes: list[dict]) -> dict:
        """返回 {decision, target_id, reason}.

        失败 graceful：默认 decision=add，原因写明 LLM 失败。
        """
        # 现有池为空，直接 add（不浪费 LLM 调用）
        if not existing_notes:
            return {"decision": "add", "target_id": None, "reason": "笔记池为空"}

        if not self.client:
            return {"decision": "add", "target_id": None,
                    "reason": "Anthropic 客户端未初始化，默认 add"}

        user_msg = _build_user_message(candidate, existing_notes)

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=CONFLICT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            return {"decision": "add", "target_id": None,
                    "reason": f"ConflictResolver LLM 失败，默认 add: {e}"}

        parsed = _parse_json_loose(text)
        if not parsed:
            return {"decision": "add", "target_id": None,
                    "reason": f"输出非 JSON，默认 add: {text[:100]}"}

        decision = str(parsed.get("decision", "add")).lower().strip()
        if decision not in ("add", "skip", "replace", "branch"):
            decision = "add"

        target_id = parsed.get("target_id") or None
        # replace/branch 必须有 target_id，且要在 existing 池里
        if decision in ("replace", "branch"):
            valid_ids = {n["id"] for n in existing_notes}
            if not target_id or target_id not in valid_ids:
                return {"decision": "add", "target_id": None,
                        "reason": f"{decision} 但 target_id 无效，降级为 add"}

        return {
            "decision": decision,
            "target_id": target_id,
            "reason": str(parsed.get("reason", ""))[:300],
        }
