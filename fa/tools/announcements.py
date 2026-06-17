"""Announcement helpers backed by the shared Wind DB skill.

Current scope is A-share announcements in ``financedata.ashareanninf``.
The DB stores HTML/text announcement bodies plus Wind links, unlike
``reportdata.report_info`` where research-report content is usually empty.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..chat.resolver import _normalize_ticker


SKILLS_ROOT = Path.home() / ".claude" / "skills"


def _load_wind_db():
    if str(SKILLS_ROOT) not in sys.path:
        sys.path.insert(0, str(SKILLS_ROOT))
    import wind_db  # type: ignore

    return wind_db


def _to_a_code(ticker: str) -> str:
    t = _normalize_ticker(ticker)
    if t.endswith(".SHG"):
        return t.replace(".SHG", ".SH")
    if t.endswith(".SHE"):
        return t.replace(".SHE", ".SZ")
    return t


def fetch_announcements(
    ticker: str,
    *,
    start: str = "2025-01-01",
    end: str = "2099-12-31",
    keyword: str = "",
    focus: str = "",
    limit: int = 20,
    include_text: bool = False,
    max_text_chars: int = 1200,
    pdf_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Fetch A-share announcements from Wind DB.

    focus: "" / "governance" / "fundamental".
    """
    code = _to_a_code(ticker)
    if not (code.endswith(".SH") or code.endswith(".SZ")):
        raise ValueError(f"当前 Wind 公告接口只支持 A 股，收到 ticker={ticker}")
    wind_db = _load_wind_db()
    rows = wind_db.get_a_announcements(
        code=code,
        start=start,
        end=end,
        keyword=keyword,
        focus=focus,
        limit=limit,
        include_text=include_text,
        max_text_chars=max_text_chars,
    )
    if include_text and pdf_fallback:
        for row in rows:
            if row.get("text") or not row.get("link"):
                continue
            text, status = _extract_pdf_text_from_url(str(row["link"]), max_chars=max_text_chars)
            row["pdf_text_status"] = status
            if text:
                row["text"] = text
    return rows


def _extract_pdf_text_from_url(url: str, *, max_chars: int = 1200, max_pages: int = 6) -> tuple[str, str]:
    if ".pdf" not in url.lower():
        return "", "not_pdf_link"
    try:
        import requests
        import fitz  # PyMuPDF
    except Exception as e:
        return "", f"missing_pdf_dependency:{e}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return "", f"http_{resp.status_code}"
        doc = fitz.open(stream=resp.content, filetype="pdf")
        parts: list[str] = []
        for page in doc[:max_pages]:
            parts.append(page.get_text("text"))
            if sum(len(p) for p in parts) >= max_chars:
                break
        text = "\n".join(parts).strip()
        return text[:max_chars], "pdf_extracted" if text else "pdf_empty_text"
    except Exception as e:
        return "", f"pdf_extract_error:{e}"


def format_announcements(rows: list[dict[str, Any]], *, show_text: bool = False) -> str:
    if not rows:
        return "未找到匹配公告。"
    lines = [f"=== 公告 ({len(rows)} 条) ==="]
    for i, r in enumerate(rows, 1):
        lines.append(f"\n{i}. [{r.get('date', '')}] {r.get('ticker', '')} {r.get('title', '')}")
        lines.append(f"   正文长度: {r.get('text_len', 0)} 字 · source={r.get('source', '')}")
        if r.get("link"):
            lines.append(f"   link: {r.get('link')}")
        if r.get("pdf_text_status"):
            lines.append(f"   pdf_text_status: {r.get('pdf_text_status')}")
        if show_text and r.get("text"):
            text = str(r["text"]).strip()
            lines.append("   摘要文本:")
            for line in text.splitlines()[:20]:
                if line.strip():
                    lines.append(f"   {line[:180]}")
    return "\n".join(lines)


def query_announcements(
    ticker: str,
    *,
    start: str = "2025-01-01",
    end: str = "2099-12-31",
    keyword: str = "",
    focus: str = "",
    limit: int = 20,
    show_text: bool = False,
) -> str:
    rows = fetch_announcements(
        ticker,
        start=start,
        end=end,
        keyword=keyword,
        focus=focus,
        limit=limit,
        include_text=show_text,
    )
    return format_announcements(rows, show_text=show_text)
