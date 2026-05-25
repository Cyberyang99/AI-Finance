"""文档摄入入口 — 按扩展名分发到对应 loader."""

import hashlib
from pathlib import Path
from typing import Optional

from .loaders.pdf import load_pdf
from .loaders.docx import load_docx
from .loaders.xlsx import load_xlsx
from .loaders.pptx import load_pptx

SUPPORTED_EXT = {".pdf", ".docx", ".xlsx", ".xls", ".pptx"}


def file_hash(path: Path) -> str:
    """16 位 md5 截断，去重用。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def ingest_file(path: str | Path) -> dict:
    """从单个文件抽取纯文本。

    返回 {
        "path": 原路径,
        "filename": 文件名,
        "ext": 扩展名,
        "text": 抽取的纯文本,
        "pages": 页/Sheet/Slide 数,
        "hash": 文件 md5 前 16 位,
    }
    """
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")

    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"不支持的格式: {ext}（支持 {', '.join(sorted(SUPPORTED_EXT))}）")

    if ext == ".pdf":
        text, pages = load_pdf(p)
    elif ext == ".docx":
        text, pages = load_docx(p)
    elif ext in (".xlsx", ".xls"):
        text, pages = load_xlsx(p)
    elif ext == ".pptx":
        text, pages = load_pptx(p)
    else:
        raise ValueError(f"未实现: {ext}")

    return {
        "path": str(p),
        "filename": p.name,
        "ext": ext,
        "text": text,
        "pages": pages,
        "hash": file_hash(p),
    }
