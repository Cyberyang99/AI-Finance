"""Word 抽文 — python-docx."""

from pathlib import Path


def load_docx(path: Path) -> tuple[str, int]:
    """返回 (纯文本, 段落数)。表格按 markdown 表格输出。"""
    from docx import Document

    doc = Document(str(path))
    parts = []
    para_count = 0

    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "").lower()
        if "heading 1" in style:
            parts.append(f"# {text}")
        elif "heading 2" in style:
            parts.append(f"## {text}")
        elif "heading" in style:
            parts.append(f"### {text}")
        else:
            parts.append(text)
        para_count += 1

    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            rows.append("| " + " | ".join(cells) + " |")
        if rows:
            # markdown header separator
            if len(rows) >= 1:
                sep = "| " + " | ".join(["---"] * len(table.rows[0].cells)) + " |"
                rows.insert(1, sep)
            parts.append("\n".join(rows))

    return "\n\n".join(parts), para_count
