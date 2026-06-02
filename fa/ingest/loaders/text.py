"""纯文本 loader — .txt（研报/资料的纯文本投喂）."""

from pathlib import Path


def load_text(path: Path) -> tuple[str, int]:
    """读取 .txt，返回 (text, pages)。pages 按换页符估算，无则计 1。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            text = Path(path).read_text(encoding=enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    pages = max(1, text.count("\f") + 1)
    return text, pages
