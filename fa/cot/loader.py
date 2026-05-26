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

    跳过 _archive/ 子目录（归档的旧文件不参与召回/投票/再合并）。
    """
    if not COT_DIR.exists():
        return []
    if sector:
        sub = COT_DIR / sector
        if sub.exists():
            return sorted(sub.glob("*.md"))  # 不递归，自动跳过子目录
        return []
    # 全库：rglob 后过滤掉 _archive 路径
    return sorted(p for p in COT_DIR.rglob("*.md")
                  if "_archive" not in p.parts)


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

    匹配格式:
      ## CoT N — <trigger>
      **信号强度**: X/10
      **推理链**: ...
    """
    out = []
    # 用 lookahead 划分到下一个 ## CoT 或字符串末
    blocks = re.split(r"(?=^## CoT \d+ — )", body, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block.startswith("## CoT"):
            continue
        # trigger
        m_trig = re.match(r"^## CoT \d+ — (.+?)$", block.split("\n", 1)[0])
        trigger = m_trig.group(1).strip() if m_trig else ""
        # signal
        m_sig = re.search(r"\*\*信号强度\*\*:\s*(\d+)\s*/\s*10", block)
        signal = m_sig.group(1) if m_sig else "5"
        # reasoning chain
        m_cot = re.search(r"\*\*推理链\*\*:\s*(.+?)(?=\n##|\Z)", block, re.DOTALL)
        cot_text = m_cot.group(1).strip() if m_cot else ""
        if trigger and cot_text:
            out.append({
                "trigger": trigger,
                "COT": cot_text,
                "signal": signal,
            })
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
        if target_tag and not any(target_tag in t.lower() for t in file_tags):
            continue
        body = text.split("---", 2)[-1]
        cots = _parse_cot_body(body)
        for i, c in enumerate(cots, 1):
            try:
                sig_n = int(c["signal"])
            except ValueError:
                sig_n = 5
            if sig_n < min_signal:
                continue
            all_cots.append({
                **c,
                "_source": fm.get("source", fp.name),
                "_sector": fm.get("sector", ""),
                "_ticker": fm.get("ticker", ""),
                "_tags": file_tags,
                "_created_at": fm.get("created_at", ""),
                "_cot_id": f"{fm.get('source_hash', fp.stem)}_{i}",
            })
    return all_cots
