"""通用导入入口 — 按扩展名自动分流到 ingest 或 user note.

设计：
  .pdf/.docx/.xlsx/.xls/.pptx → 研报，走 cot_extractor 提炼 CoT
  .md/.txt → 用户论点，走 user_note (要求文件名前缀含 ticker 或 frontmatter 含 ticker)

文件名约定:
  600519.SHG_xxx.md         → ticker=600519.SHG
  2513.HK_我的看法.md        → ticker=2513.HK
  09988-HK_xxx.md           → ticker=09988.HK (兼容)
  没有 ticker 前缀 → 检查 frontmatter，没有就跳过并提示
"""

import re
from pathlib import Path
from typing import Optional

from .base import SUPPORTED_EXT


USER_NOTE_EXT = {".md", ".txt"}
ALL_IMPORT_EXT = SUPPORTED_EXT | USER_NOTE_EXT


TICKER_PATTERN = re.compile(
    r"^(\d{4,6}\.(?:HK|SHG|SHE|US)|[A-Z]{1,5}\.US|\d{4,6}[-_](?:HK|SHG|SHE|US))",
    re.IGNORECASE,
)


def detect_ticker_from_filename(fname: str) -> Optional[str]:
    """从文件名前缀提取 ticker。

    支持：
      600519.SHG_xxx.md
      2513.HK_xxx.md
      AAPL.US-xxx.md
      09988-HK_xxx.md (- 替代 .)
    """
    m = TICKER_PATTERN.match(fname)
    if not m:
        return None
    raw = m.group(1).upper()
    # 标准化 09988-HK → 09988.HK
    raw = raw.replace("-HK", ".HK").replace("_HK", ".HK")
    raw = raw.replace("-SHG", ".SHG").replace("_SHG", ".SHG")
    raw = raw.replace("-SHE", ".SHE").replace("_SHE", ".SHE")
    raw = raw.replace("-US", ".US").replace("_US", ".US")
    return raw


def detect_ticker_from_frontmatter(text: str) -> Optional[str]:
    """从 markdown frontmatter 读 ticker 字段。"""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].split("\n"):
        line = line.strip()
        m = re.match(r"^ticker\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            return m.group(1).strip().upper() or None
    return None


def classify_file(path: Path) -> str:
    """返回 'research'（研报）/ 'user_note' / 'skip'."""
    ext = path.suffix.lower()
    if ext in SUPPORTED_EXT:
        return "research"
    if ext in USER_NOTE_EXT:
        return "user_note"
    return "skip"


def scan_dir(directory: Path, recursive: bool = True) -> list[Path]:
    """扫目录返回所有支持的文件，跳过 . 开头的隐藏文件 + _archive/."""
    if not directory.is_dir():
        return []
    if recursive:
        candidates = directory.rglob("*")
    else:
        candidates = directory.iterdir()
    out = []
    for p in candidates:
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if "_archive" in p.parts:
            continue
        if p.suffix.lower() in ALL_IMPORT_EXT:
            out.append(p)
    return sorted(out)
