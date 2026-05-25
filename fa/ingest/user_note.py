"""用户论点录入 — 4 维度结构化笔记.

维度：
- core_thesis     核心论点（为什么看好/看坏）
- moat            护城河（最重要的 1-2 点）
- falsification   反证条件（什么情况证伪论点）
- horizon_size    预期时间窗口 + 最大仓位

存储路径: memory/theses/user/<ticker>_<yyyy-mm-dd>.md
召回权重: 默认 2.0（高于研报提取的 CoT，对齐用户思考逻辑）
"""

import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..memory.store import PROJECT_DIR

USER_THESES_DIR = PROJECT_DIR / "memory" / "theses" / "user"

DIMENSIONS = [
    ("core_thesis", "核心论点（为什么看好/看坏，一两句话）"),
    ("moat", "护城河（最重要的 1-2 点，让它持续赚超额利润的根本原因）"),
    ("falsification", "反证条件（什么情况证伪论点，必须可观察可量化）"),
    ("horizon_size", "预期时间窗口 + 最大仓位（例：12 个月，最多 8%）"),
]


def _safe_ticker(t: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", t.strip().upper())


def interactive_prompt() -> dict:
    """交互式 4 问。回车跳过即留空。"""
    print("\n=== fa note 交互录入 ===")
    print("(每个维度回车跳过；保存后可随时改文件)\n")
    answers = {}
    for key, label in DIMENSIONS:
        print(f"【{label}】")
        val = input("> ").strip()
        answers[key] = val
        print()
    return answers


def save_user_note(
    ticker: str,
    core_thesis: str = "",
    moat: str = "",
    falsification: str = "",
    horizon_size: str = "",
    raw_text: str = "",
    weight: float = 2.0,
    sector: Optional[str] = None,
) -> Path:
    """保存用户论点到 memory/theses/user/<ticker>_<yyyy-mm-dd>.md.

    raw_text: 自由文本（fa note -m / -f 走这个），优先级高于结构化字段
    weight: 召回时的权重（默认 2.0 高于研报 CoT 的 1.0）
    """
    USER_THESES_DIR.mkdir(parents=True, exist_ok=True)
    t = _safe_ticker(ticker)
    today = date.today().isoformat()
    fname = f"{t}_{today}.md"
    path = USER_THESES_DIR / fname

    has_structured = any([core_thesis, moat, falsification, horizon_size])
    has_raw = bool(raw_text and raw_text.strip())

    if not has_structured and not has_raw:
        raise ValueError("空论点：4 个维度和 raw_text 都为空")

    lines = [
        "---",
        f"ticker: {t}",
        f"sector: {sector or ''}",
        f"source: user",
        f"created_at: {today}",
        f"weight: {weight}",
        f"confidence: high",
        "---",
        "",
        f"# {t} — 用户论点 ({today})",
        "",
    ]

    if has_structured:
        if core_thesis:
            lines.extend(["## 核心论点", "", core_thesis, ""])
        if moat:
            lines.extend(["## 护城河", "", moat, ""])
        if falsification:
            lines.extend(["## 反证条件", "", falsification, ""])
        if horizon_size:
            lines.extend(["## 时间窗口 + 仓位", "", horizon_size, ""])

    if has_raw:
        lines.extend(["## 备注 / 原始想法", "", raw_text.strip(), ""])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def load_user_notes(ticker: Optional[str] = None) -> list[dict]:
    """加载所有用户笔记。ticker 不空则过滤。

    返回 list[{"ticker", "path", "created_at", "content"}]，按时间倒序。
    """
    if not USER_THESES_DIR.exists():
        return []

    out = []
    for p in USER_THESES_DIR.glob("*.md"):
        # 文件名格式 <TICKER>_<YYYY-MM-DD>.md
        m = re.match(r"^(.+?)_(\d{4}-\d{2}-\d{2})\.md$", p.name)
        if not m:
            continue
        t, d = m.group(1), m.group(2)
        if ticker and t != _safe_ticker(ticker):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            content = ""
        out.append({
            "ticker": t,
            "path": str(p),
            "created_at": d,
            "content": content,
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out
