"""CoT 加载器 — 从 memory/knowledge/cot/ 读取所有思维链.

每个文件结构（由 fa/ingest/cot_extractor.py 写入）:

```
---
ticker: ...
sector: ...
source: <文件名>
source_hash: <hash>
created_at: YYYY-MM-DD
cot_count: N
---

# CoT 提取自 ...

## CoT 1 — <trigger>

**信号强度**: 9/10

**推理链**: 驱动 → ... → 股价表现
```
"""

import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..memory.store import PROJECT_DIR

COT_DIR = PROJECT_DIR / "memory" / "knowledge" / "cot"


def list_cot_files(sector: Optional[str] = None) -> list[Path]:
    """列出所有 CoT 文件路径，可按 sector 过滤。

    跳过任何 _archive 开头的子目录（_archive/、_archive_wipe_*/、_archive_regroup_* 等）。
    """
    if not COT_DIR.exists():
        return []
    if sector:
        sub = COT_DIR / sector
        if sub.exists():
            return sorted(sub.glob("*.md"))
        return []
    return sorted(p for p in COT_DIR.rglob("*.md")
                  if not any(part.startswith("_archive") for part in p.parts))


def _parse_frontmatter(text: str) -> dict:
    """解析 yaml frontmatter（手写，避免新依赖）。"""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fm = {}
    for line in parts[1].split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([\w_]+)\s*:\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return fm


def _parse_cot_body(body: str) -> list[dict]:
    """从 markdown body 抽出每条 CoT。

    匹配格式（兼容 v1 和 v2）:
      ## CoT N — <trigger>
      **信号强度**: X/10  _(传导 X · 历史 Y · 时效 Z)_   ← v2 子分（可选）
      **推理链**: ...
    """
    out = []
    blocks = re.split(r"(?=^## CoT \d+ — )", body, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block.startswith("## CoT"):
            continue
        m_trig = re.match(r"^## CoT \d+ — (.+?)$", block.split("\n", 1)[0])
        trigger = m_trig.group(1).strip() if m_trig else ""
        # 链级主题 tag（**主题**: a、b），v4 起；旧文件无此行 → chain_tag_line=False，由 load_cots 回退文件级
        m_ctag = re.search(r"^\*\*主题\*\*:\s*(.+)$", block, re.MULTILINE)
        chain_tag_line = m_ctag is not None
        chain_tags = ([t.strip() for t in re.split(r"[、,，]", m_ctag.group(1).strip()) if t.strip()]
                      if m_ctag else [])
        m_sig = re.search(r"\*\*信号强度\*\*:\s*(\d+)\s*/\s*10", block)
        signal = m_sig.group(1) if m_sig else "5"
        # 子分（可选）。v3 含「证伪」；兼容 v2 旧格式（无证伪）
        sub_scores = {}
        m_sub4 = re.search(r"传导\s*(\d+)\s*·\s*证伪\s*(\d+)\s*·\s*历史\s*(\d+)\s*·\s*时效\s*(\d+)", block)
        if m_sub4:
            sub_scores = {
                "transmission": int(m_sub4.group(1)),
                "falsifiability": int(m_sub4.group(2)),
                "history": int(m_sub4.group(3)),
                "recency": int(m_sub4.group(4)),
            }
        else:
            m_sub = re.search(r"传导\s*(\d+)\s*·\s*历史\s*(\d+)\s*·\s*时效\s*(\d+)", block)
            if m_sub:
                sub_scores = {
                    "transmission": int(m_sub.group(1)),
                    "history": int(m_sub.group(2)),
                    "recency": int(m_sub.group(3)),
                }
        # 推理链：截到「原文依据」或下一条之前
        m_cot = re.search(r"\*\*推理链\*\*:\s*(.+?)(?=\n\*\*原文依据\*\*|\n##|\Z)", block, re.DOTALL)
        cot_text = m_cot.group(1).strip() if m_cot else ""
        # 原文依据（v3.1 起，可选）
        m_ev = re.search(r"\*\*原文依据\*\*:\s*「(.+?)」", block, re.DOTALL)
        evidence = m_ev.group(1).strip() if m_ev else ""
        # 合并链的来源 cot id（merged 文件写了 `_来源 CoT id: a, b_`），用于回溯原文。
        # 整行匹配后再去掉尾部斜体下划线——cot_id 本身含 `_`，不能用 `.+?_` 非贪婪（会截断）。
        m_src = re.search(r"(?m)^_来源 CoT id:\s*(.+)$", block)
        source_ids = ([s.strip() for s in m_src.group(1).rstrip("_").split(",") if s.strip()]
                      if m_src else [])
        if trigger and cot_text:
            item = {"trigger": trigger, "COT": cot_text, "signal": signal, "evidence": evidence,
                    "_chain_tags": chain_tags, "_chain_tag_line": chain_tag_line}
            if source_ids:
                item["_source_ids"] = source_ids
            item.update(sub_scores)
            out.append(item)
    return out


def _parse_tags(tags_str: str) -> list[str]:
    """解析 frontmatter 里的 `tags: [a, b, c]` 行。"""
    if not tags_str:
        return []
    s = tags_str.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [t.strip() for t in s.split(",") if t.strip()]


def load_cots(sector: Optional[str] = None, ticker: Optional[str] = None,
              min_signal: int = 0, tag: Optional[str] = None) -> list[dict]:
    """加载所有 CoT，可按 sector / ticker / 最低信号强度 / tag 过滤。

    tag 过滤：精确匹配 frontmatter 里 tags 数组中的某一项（大小写不敏感）。
    """
    files = list_cot_files(sector)
    all_cots = []
    target_tag = (tag or "").strip().lower()
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        if ticker and fm.get("ticker") and fm["ticker"] != ticker:
            continue
        file_tags = _parse_tags(fm.get("tags", ""))
        # 文件级预过滤（frontmatter tags 是各链 tag 的并集，是有效快路径）
        if target_tag and not any(target_tag in t.lower() for t in file_tags):
            continue
        body = text.split("---", 2)[-1]
        cots = _parse_cot_body(body)
        # 文件是否启用了链级 tag：任一条链带 **主题** 行即为是
        file_chain_tagged = any(c.get("_chain_tag_line") for c in cots)
        for i, c in enumerate(cots, 1):
            try:
                sig_n = int(c["signal"])
            except ValueError:
                sig_n = 5
            if sig_n < min_signal:
                continue
            # 有效 tag：链级标注的文件以链为准，旧文件回退文件级
            eff_tags = (c.get("_chain_tags") or []) if file_chain_tagged else file_tags
            # 链级精过滤：讲硬件的链不会再被「AI 大模型与云」之类的文件 tag 误召回
            if target_tag and not any(target_tag in t.lower() for t in eff_tags):
                continue
            try:
                quality_rating = int(fm.get("quality_rating", 0))
            except (TypeError, ValueError):
                quality_rating = 0
            all_cots.append({
                **c,
                "_source": fm.get("source", fp.name),
                "_sector": fm.get("sector", ""),
                "_ticker": fm.get("ticker", ""),
                "_tags": eff_tags,
                "_created_at": fm.get("created_at", ""),
                "_cot_id": f"{fm.get('source_hash', fp.stem)}_{i}",
                "_quality_rating": quality_rating,
                "_file_path": str(fp),
            })
    return all_cots
