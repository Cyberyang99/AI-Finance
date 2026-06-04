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
    _theme_tag_lookup.cache_clear()


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

## 候选主题 tags（必须从下面清单里选，用 name_cn；不要创造新主题）

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
2. tags 必须从上面 Theme 主题清单里选，写 name_cn（不是 id）。**套得上才填；清单里没有任何贴合的主题时，tags 留空数组 []，并在 reasoning 写明『建议新主题：XXX』——不要硬凑、不要自己造词。**
   ⚠ **tags 必须是行业/主题层面，严禁用公司名或股票代码做 tag**（如「智谱」「腾讯」「阿里巴巴」「MiniMax」都不行）。
   讲某家或多家公司时，打它们所属的主题，例如：智谱/腾讯大模型/MiniMax → 「AI 大模型与云」；宁德时代 → 「电力能源及设备」。
3. 主行业（GICS 24 个）反映"公司业务在哪一行"，tags 反映"投资视角是什么"
4. ⚠ **按业务本质归类，不要往 CapitalGoods 里塞**：CapitalGoods（资本品）只用于真正的工业机械 / 装备制造 / 电气设备 / 军工。新兴科技主题要按本质归对行业：
   - 量子计算 / 量子信息 / 量子通信 → 多为芯片或设备公司，归 Semiconductors 或 TechHardware（tag 用「量子」）
   - 世界模型 / 具身智能 / 大模型 / AI 应用 / AI 云 → 归 SoftwareServices（tag 用「AI 大模型与云」「机器人」等）
   - 数字人民币 / 稳定币 / 跨境支付 / 数字货币 → 归 DiversifiedFinancials 或 SoftwareServices（tag 用「加密货币」）
   - 光模块 / CPO / 存储 / 算力芯片 → 归 Semiconductors 或 TechHardware（tag 用对应 AI 算力 / 存储 / 互联）
5. 例子：
   - 豪迈科技（机械制造，下游受 AI 数据中心拉动）→ sector_id="CapitalGoods", tags=["AI 算力", "电力能源及设备"]
   - 某量子计算公司 → sector_id="Semiconductors", tags=["量子"]
   - 数字人民币 / 稳定币研究 → sector_id="DiversifiedFinancials", tags=["加密货币"]
6. ⚠ 实在判断不出归哪个 GICS 行业时，sector_id 填 "Uncategorized"（清单里有），**不要**用 CapitalGoods 兜底——填 Uncategorized 是为了让我事后能一眼看到并手动修正。

除 JSON 外不要任何其他内容。"""


def _norm_tag_key(t: str) -> str:
    """tag 归一键：NFKC + 去空格 + lower，用于和白名单做精确比对。"""
    import unicodedata
    return unicodedata.normalize("NFKC", t or "").replace(" ", "").replace("　", "").lower()


@lru_cache(maxsize=1)
def _theme_tag_lookup() -> dict:
    """{归一键: 主题 name_cn 规范写法}，覆盖每个 Theme 的 name_cn + 全部 aliases。"""
    out: dict = {}
    for s in list_themes():
        canon = s["name_cn"]
        out[_norm_tag_key(canon)] = canon
        for a in s.get("aliases", []):
            out.setdefault(_norm_tag_key(a), canon)
    return out


def _valid_theme_tag(tag: str) -> Optional[str]:
    """tag 能精确归到某个 Theme 主题就返回其规范 name_cn，否则 None。

    只做归一后的精确匹配（name_cn + aliases），不做模糊子串，避免误命中把
    无关文档塞进主题。这是闭合词表的守门：LLM 不在白名单里现编的 tag 一律拦下。
    """
    if not tag or not tag.strip():
        return None
    return _theme_tag_lookup().get(_norm_tag_key(tag))


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
        return {"sector_id": "Uncategorized", "tags": [], "suggested_tags": [],
                "confidence": "low", "reasoning": f"LLM 调用失败: {e}"}

    parsed = _parse_json(out_text)
    if not parsed:
        print(f"  [classify] JSON 解析失败，原始: {out_text[:200]}")
        return {"sector_id": "Uncategorized", "tags": [], "suggested_tags": [],
                "confidence": "low", "reasoning": "JSON 解析失败"}

    # 验证 sector_id 合法
    sid = parsed.get("sector_id", "")
    if sid not in [s["id"] for s in sectors]:
        # 尝试用 alias 反查
        resolved = resolve_alias(sid)
        if resolved:
            sid = resolved
        else:
            print(f"  [classify] LLM 返回了非法 sector_id={sid!r}，归到 Uncategorized")
            sid = "Uncategorized"

    raw_tags = parsed.get("tags", []) or []
    if not isinstance(raw_tags, list):
        raw_tags = [str(raw_tags)]
    raw_tags = [str(t).strip() for t in raw_tags if str(t).strip()]

    # 闭合词表守门：只收能精确归到白名单主题的 tag，规范成 name_cn；
    # 其余作为「疑似新主题」拦下，交给调用方提示用户手动加入 sectors.yaml。
    accepted: list[str] = []
    suggested: list[str] = []
    for t in raw_tags:
        canon = _valid_theme_tag(t)
        if canon:
            if canon not in accepted:
                accepted.append(canon)
        elif t not in suggested:
            suggested.append(t)

    return {
        "sector_id": sid,
        "tags": accepted[:4],
        "suggested_tags": suggested,
        "confidence": parsed.get("confidence", "medium"),
        "reasoning": parsed.get("reasoning", ""),
    }


def display_sector(sid: str) -> str:
    """sid → '资本品 (Industrials.CapitalGoods)' 这种好读形式。"""
    s = get_sector(sid)
    if not s:
        return sid
    return f"{s['name_cn']} ({s['parent']}.{sid})"
