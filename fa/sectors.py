"""主板块清单 + 同义词归一 + LLM 自动分类.

数据源: memory/sectors.yaml (31 个主板块: GICS 24 + 7 主题)
"""

from __future__ import annotations
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from .memory.store import PROJECT_DIR

SECTORS_YAML = PROJECT_DIR / "memory" / "sectors.yaml"
COT_DIR = PROJECT_DIR / "memory" / "knowledge" / "cot"


@lru_cache(maxsize=1)
def load_registry() -> dict:
    """加载 sectors.yaml；带 lru_cache，但允许手动 reload."""
    if not SECTORS_YAML.exists():
        raise FileNotFoundError(f"sectors.yaml 不存在: {SECTORS_YAML}")
    with open(SECTORS_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data


def reload_registry():
    load_registry.cache_clear()


def list_sectors() -> list[dict]:
    """返回所有主板块的 [{id, name_cn, parent, aliases, desc}] 列表."""
    reg = load_registry()
    out = []
    for sid, info in reg.get("sectors", {}).items():
        out.append({
            "id": sid,
            "name_cn": info.get("name_cn", sid),
            "parent": info.get("parent", ""),
            "aliases": info.get("aliases", []),
            "desc": info.get("desc", ""),
        })
    return out


def list_themes() -> list[dict]:
    """只返回 parent=Theme 的主题板块（用于打 tags 时给 LLM 参考）。"""
    return [s for s in list_sectors() if s["parent"] == "Theme"]


def get_sector(sid: str) -> Optional[dict]:
    reg = load_registry()
    info = reg.get("sectors", {}).get(sid)
    if not info:
        return None
    return {
        "id": sid,
        "name_cn": info.get("name_cn", sid),
        "parent": info.get("parent", ""),
        "aliases": info.get("aliases", []),
        "desc": info.get("desc", ""),
    }


def resolve_alias(raw: str) -> Optional[str]:
    """raw 文本归一到标准 sector id。

    匹配规则（按优先级）：
      1. 直接是 sector id（精确）
      2. keyword_fallback 完整短语命中（处理"AI-数据中心-电力-燃气轮机"这种自由拼接）
      3. aliases 精确匹配
      4. aliases 子串包含（双向，>=2 字符）
    """
    if not raw:
        return None
    reg = load_registry()
    raw_n = raw.strip()

    # 1. 直接 id 匹配
    if raw_n in reg.get("sectors", {}):
        return raw_n

    # 2. keyword_fallback 完整短语命中（**优先于** aliases 模糊匹配）
    for kw, sid in reg.get("keyword_fallback", {}).items():
        if kw in raw_n:
            return sid

    raw_lower = raw_n.lower()
    raw_norm = raw_lower.replace(" ", "").replace("-", "").replace("_", "")

    # 3. aliases 精确（大小写不敏感）
    for sid, info in reg.get("sectors", {}).items():
        for a in info.get("aliases", []):
            a_lower = a.lower()
            a_norm = a_lower.replace(" ", "").replace("-", "").replace("_", "")
            if raw_lower == a_lower or raw_norm == a_norm:
                return sid

    # 4. aliases 子串模糊
    for sid, info in reg.get("sectors", {}).items():
        for a in info.get("aliases", []):
            a_norm = a.lower().replace(" ", "").replace("-", "").replace("_", "")
            if raw_norm and (raw_norm in a_norm or a_norm in raw_norm):
                if min(len(raw_norm), len(a_norm)) >= 2:
                    return sid

    return None


# ── LLM 分类器 ──

CLASSIFY_SYSTEM = """你是股票投资研究的行业分类专家。任务：根据资料标题、内容摘要、用户描述，把它归到一个标准主板块 (GICS 风格)，并提取 1-4 个细分主题作为 tags。"""

CLASSIFY_USER_TEMPLATE = """## 候选主板块清单（必须从中选一个，且只能选一个）

{sector_list}

## 候选主题 tags（可多选，1-4 个；也可创造新的 tag，但优先用清单里的）

{theme_list}

## 资料

- 文件名: {filename}
- 用户描述: {user_comment}
- 内容摘要 (前 1500 字):

---
{text_preview}
---

## 输出格式

严格 JSON（不要 markdown 代码块包裹）：

```
{{
  "sector_id": "<必须是上面清单里的 id>",
  "tags": ["<主题 1>", "<主题 2>"],
  "confidence": "high|medium|low",
  "reasoning": "<一句话解释为什么选这个 sector>"
}}
```

关键规则：
1. sector_id 必须严格等于清单里的 id（大小写敏感），不要瞎写
2. tags 优先从 Theme_ 主题清单选；如果选 Theme 类，写 name_cn（不是 id）
3. 主行业（GICS 24 个）反映"公司业务在哪一行"，tags 反映"投资视角是什么"
4. 例子：豪迈科技（机械制造，下游受 AI 数据中心拉动）→ sector_id="CapitalGoods", tags=["AI 主题", "电力 / 能源主题"]

除 JSON 外不要任何其他内容。"""


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    # 去 markdown 包裹
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


def classify_doc(
    filename: str,
    text: str,
    user_comment: str = "",
    text_preview_chars: int = 1500,
) -> dict:
    """用 LLM 给一份资料分类，返回 {sector_id, tags, confidence, reasoning}.

    失败兜底：返回 {sector_id: 'CapitalGoods', tags: [], confidence: 'low', reasoning: 'LLM 失败'}。
    """
    from .config import load_config, make_anthropic_client

    sectors = list_sectors()
    # 给 LLM 看一个紧凑列表
    sector_list_str = "\n".join(
        f"- {s['id']:<28} {s['name_cn']} (parent={s['parent']})  -- 关键词: {', '.join(s['aliases'][:5])}"
        for s in sectors if s['parent'] != 'Theme'
    )
    theme_list_str = "\n".join(
        f"- {s['name_cn']:<20} (id={s['id']})  -- 关键词: {', '.join(s['aliases'][:5])}"
        for s in sectors if s['parent'] == 'Theme'
    )

    preview = (text or "")[:text_preview_chars]

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")
    try:
        client = make_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": CLASSIFY_USER_TEMPLATE.format(
                sector_list=sector_list_str,
                theme_list=theme_list_str,
                filename=filename,
                user_comment=user_comment or "(无)",
                text_preview=preview,
            )}],
        )
        out_text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [classify] LLM 失败: {e}")
        return {"sector_id": "CapitalGoods", "tags": [], "confidence": "low",
                "reasoning": f"LLM 调用失败: {e}"}

    parsed = _parse_json(out_text)
    if not parsed:
        print(f"  [classify] JSON 解析失败，原始: {out_text[:200]}")
        return {"sector_id": "CapitalGoods", "tags": [], "confidence": "low",
                "reasoning": "JSON 解析失败"}

    # 验证 sector_id 合法
    sid = parsed.get("sector_id", "")
    if sid not in [s["id"] for s in sectors]:
        # 尝试用 alias 反查
        resolved = resolve_alias(sid)
        if resolved:
            sid = resolved
        else:
            print(f"  [classify] LLM 返回了非法 sector_id={sid!r}，归到 CapitalGoods")
            sid = "CapitalGoods"

    tags = parsed.get("tags", []) or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [str(t).strip() for t in tags if str(t).strip()][:4]

    return {
        "sector_id": sid,
        "tags": tags,
        "confidence": parsed.get("confidence", "medium"),
        "reasoning": parsed.get("reasoning", ""),
    }


def display_sector(sid: str) -> str:
    """sid → '资本品 (Industrials.CapitalGoods)' 这种好读形式。"""
    s = get_sector(sid)
    if not s:
        return sid
    return f"{s['name_cn']} ({s['parent']}.{sid})"
