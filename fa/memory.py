"""Memory 管理 — 结构化记忆读写。

目录结构:
  memory/
    theses/     — 个股投资论点
    scans/       — 板块横向扫描存档
    reviews/     — 定期回顾记录
    learnings/   — 经验教训
    framework/   — 可进化投资框架
"""

from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def save_thesis(ticker: str, content: str):
    """保存/更新个股投资论点."""
    d = MEMORY_DIR / "theses"
    _ensure_dir(d)
    path = d / f"{_clean(ticker)}.md"
    header = f"# {ticker} — 投资论点\n\n最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    existing_body = path.read_text(encoding="utf-8") if path.exists() else ""
    # 在前面追加新分析，保留历史
    new_entry = f"## 分析记录 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n{content}\n\n---\n\n"
    path.write_text(header + new_entry + existing_body.lstrip(header).lstrip(), encoding="utf-8")
    print(f"[MEMORY] 已写入: {path}")


def get_thesis(ticker: str) -> str | None:
    """读取个股已有论点."""
    path = MEMORY_DIR / "theses" / f"{_clean(ticker)}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def list_theses() -> list[str]:
    """列出已分析过的股票."""
    d = MEMORY_DIR / "theses"
    if not d.exists():
        return []
    return sorted([p.stem for p in d.glob("*.md")])


def save_scan(topic: str, content: str):
    """保存板块横向扫描结果."""
    d = MEMORY_DIR / "scans"
    _ensure_dir(d)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    path = d / f"{_clean(topic)}_{date_str}.md"
    path.write_text(content, encoding="utf-8")
    print(f"[MEMORY] 已写入: {path}")
    return str(path)


def save_review(content: str):
    """保存定期回顾记录."""
    d = MEMORY_DIR / "reviews"
    _ensure_dir(d)
    date_str = datetime.now().strftime("%Y%m%d")
    path = d / f"review_{date_str}.md"
    path.write_text(content, encoding="utf-8")
    print(f"[MEMORY] 已写入: {path}")


def list_pending_reviews(days_threshold: int = 90) -> list[str]:
    """找出需要回顾的股票（距上次分析超过阈值天数）。"""
    d = MEMORY_DIR / "theses"
    if not d.exists():
        return []
    pending = []
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days_threshold)
    for path in d.glob("*.md"):
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime < cutoff:
            pending.append(path.stem)
    return sorted(pending)


def save_learning(content: str):
    """记录经验教训."""
    d = MEMORY_DIR / "learnings"
    _ensure_dir(d)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    path = d / f"learning_{date_str}.md"
    path.write_text(content, encoding="utf-8")
    print(f"[MEMORY] 已写入: {path}")


def _clean(name: str) -> str:
    return name.replace(".", "_").replace("/", "_").replace("\\", "_")
