"""文档摄入入口 — 按扩展名分发到对应 loader."""

import hashlib
from pathlib import Path
from typing import Optional

from .loaders.pdf import load_pdf
from .loaders.docx import load_docx
from .loaders.xlsx import load_xlsx
from .loaders.pptx import load_pptx
from .loaders.text import load_text

# 纯文本类（走 load_text，统一编码兜底）
TEXT_EXT = {".txt", ".md", ".markdown"}
# ingest_file 能抽文的全部格式（能力集）；研报/用户笔记的「路由」另由调用方/menu 决定
SUPPORTED_EXT = {".pdf", ".docx", ".xlsx", ".xls", ".pptx"} | TEXT_EXT


def clean_user_path(s: str) -> str:
    """清洗用户输入/拖拽的文件路径。

    - 去掉成对的引号（粘贴常见）
    - 还原 shell 转义：`\\ ` → 空格、`\\(` → `(` 等（拖文件进终端会转义元字符）
    - 只解「反斜杠+元字符」，不动「反斜杠+字母」，保住 Windows 路径分隔符 C:\\Users\\…
    """
    import re as _re
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    s = _re.sub(r"\\([ ()\[\]&'\"!$#@`;|*?<>])", r"\1", s)
    return s.strip()


def file_hash(path: Path) -> str:
    """16 位 md5 截断，去重用。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def archive_raw(src_path: str | Path, file_hash_val: str) -> str:
    """把原始研报文件归档到 memory/raw/<hash>_<原名>，返回相对 memory/ 的路径。

    memory/raw/ 已软链到 OneDrive，归档后随双机同步。
    同 hash 已归档则跳过拷贝（去重）。删了桌面原文也能从这里回溯原句。
    """
    import shutil
    from ..memory.store import PROJECT_DIR

    src = Path(src_path).expanduser()
    raw_dir = PROJECT_DIR / "memory" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / f"{file_hash_val}_{src.name}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return f"raw/{dest.name}"


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
    elif ext in TEXT_EXT:
        text, pages = load_text(p)
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
