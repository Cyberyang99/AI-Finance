"""PDF 抽文 — PyMuPDF (fitz)."""

from pathlib import Path


def load_pdf(path: Path) -> tuple[str, int]:
    """返回 (纯文本, 页数)。"""
    import fitz

    doc = fitz.open(str(path))
    n = len(doc)
    parts = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if text:
            parts.append(f"## Page {i+1}\n\n{text}")
    doc.close()
    return "\n\n".join(parts), n
