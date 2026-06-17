"""多份 report-level note → company-level synthesis。

底层 note 是证据切片：一份报告一份，尽量不可变。
company synthesis 是可重算视图：汇总共识、保留分歧、标记过时信息。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

from .config import load_config, make_anthropic_client
from .ingest.user_note import USER_THESES_DIR, _safe_ticker
from .note_template import DIMENSIONS, SCHEMA_VERSION, parse_frontmatter


COMPANY_THESES_DIR = USER_THESES_DIR.parent / "company"
CONFLICTS_DIR = USER_THESES_DIR.parent / "conflicts"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        fm = parse_frontmatter(text)
    return fm or {}, parts[2]


def _section_map(body: str) -> dict[str, str]:
    """从 render_markdown 生成的正文中切出各维度文本。"""
    out: dict[str, str] = {}
    header_re = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
    matches = list(header_re.finditer(body))
    for idx, m in enumerate(matches):
        dim_idx = int(m.group(1)) - 1
        if dim_idx < 0 or dim_idx >= len(DIMENSIONS):
            continue
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if content.startswith("_(未填写"):
            content = ""
        out[DIMENSIONS[dim_idx]["id"]] = content
    return out


def load_report_notes(ticker: str, *, max_notes: int = 12) -> list[dict]:
    """加载某 ticker 的 report-level notes，最新在前。"""
    t = _safe_ticker(ticker)
    if not USER_THESES_DIR.exists():
        return []
    notes = []
    for fp in USER_THESES_DIR.glob(f"{t}_*.md"):
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = _split_frontmatter(text)
        if str(fm.get("ticker", "")).upper().strip() != t:
            continue
        notes.append({
            "path": fp,
            "fm": fm,
            "body": body,
            "sections": _section_map(body),
            "created_at": str(fm.get("created_at", "2000-01-01")),
            "source_doc": str(fm.get("source_doc", "")),
            "source_hash": str(fm.get("source_hash", "")),
            "raw_path": str(fm.get("raw_path", "")),
            "raw_text_path": str(fm.get("raw_text_path", "")),
            "template_version": str(fm.get("template_version", "")),
        })
    notes.sort(key=lambda n: (n["created_at"], n["path"].name), reverse=True)
    return notes[:max_notes]


def list_report_note_tickers() -> dict[str, list[dict]]:
    """按 ticker 分组加载 report-level notes。"""
    grouped: dict[str, list[dict]] = defaultdict(list)
    if not USER_THESES_DIR.exists():
        return {}
    for fp in USER_THESES_DIR.glob("*.md"):
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = _split_frontmatter(text)
        ticker = str(fm.get("ticker", "")).upper().strip()
        if not ticker:
            m = re.match(r"^(.+?)_\d{4}-\d{2}-\d{2}", fp.name)
            ticker = m.group(1).upper().strip() if m else ""
        if not ticker:
            continue
        grouped[ticker].append({
            "path": fp,
            "fm": fm,
            "body": body,
            "created_at": str(fm.get("created_at", "2000-01-01")),
            "source_doc": str(fm.get("source_doc", "")),
        })
    for ticker, notes in grouped.items():
        notes.sort(key=lambda n: (n["created_at"], n["path"].stat().st_mtime), reverse=True)
    return dict(grouped)


def list_company_syntheses(ticker: str) -> list[Path]:
    """列出某 ticker 已有 company synthesis，最新在前。"""
    t = _safe_ticker(ticker)
    if not COMPANY_THESES_DIR.exists():
        return []
    return sorted(
        COMPANY_THESES_DIR.glob(f"{t}_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def load_company_synthesis(ticker: str) -> Optional[dict]:
    """读取某 ticker 最新 company synthesis。"""
    paths = list_company_syntheses(ticker)
    if not paths:
        return None
    path = paths[0]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = _split_frontmatter(text)
    return {"path": path, "fm": fm, "body": body, "content": text}


def load_company_conflicts(ticker: str) -> Optional[dict]:
    """读取某 ticker 最新 conflicts jsonl。"""
    t = _safe_ticker(ticker)
    if not CONFLICTS_DIR.exists():
        return None
    paths = sorted(CONFLICTS_DIR.glob(f"{t}_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return None
    path = paths[0]
    try:
        lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        lines = []
    return {"path": path, "items": lines}


def stale_synthesis_candidates(
    *,
    min_notes: int = 2,
    recent_days: int = 30,
    limit: int = 5,
) -> list[dict]:
    """找出需要生成/更新 company synthesis 的 ticker。

    条件：
    - report-level note 数量 >= min_notes；
    - 最新 note 最近 recent_days 天内有改动；
    - 且没有 synthesis，或最新 note 比最新 synthesis 更新。
    """
    cutoff = datetime.now().timestamp() - recent_days * 86400
    out = []
    for ticker, notes in list_report_note_tickers().items():
        if len(notes) < min_notes:
            continue
        latest_note_mtime = max(n["path"].stat().st_mtime for n in notes)
        if latest_note_mtime < cutoff:
            continue
        latest_synth = list_company_syntheses(ticker)
        latest_synth_mtime = latest_synth[0].stat().st_mtime if latest_synth else 0
        if latest_note_mtime <= latest_synth_mtime:
            continue
        out.append({
            "ticker": ticker,
            "note_count": len(notes),
            "latest_note": max(n["path"].name for n in notes),
            "latest_note_mtime": latest_note_mtime,
            "latest_synthesis": str(latest_synth[0]) if latest_synth else "",
            "latest_synthesis_mtime": latest_synth_mtime,
        })
    out.sort(key=lambda x: x["latest_note_mtime"], reverse=True)
    return out[:limit]


def auto_consolidate_stale(
    *,
    min_notes: int = 2,
    recent_days: int = 30,
    limit: int = 3,
) -> dict:
    """对最近新增/更新且 note>=2 的公司自动跑 synthesis。"""
    candidates = stale_synthesis_candidates(min_notes=min_notes, recent_days=recent_days, limit=limit)
    results = []
    for c in candidates:
        res = build_company_synthesis(c["ticker"], save=True)
        results.append({
            "ticker": c["ticker"],
            "note_count": c["note_count"],
            "ok": not bool(res.get("error")),
            "path": res.get("path", ""),
            "conflict_path": res.get("conflict_path", ""),
            "error": res.get("error", ""),
        })
    return {"candidates": candidates, "results": results}


def _compact_note(n: dict, per_note_chars: int = 9000) -> str:
    fm = n["fm"]
    lines = [
        f"### {n['path'].name}",
        f"- date: {n['created_at']}",
        f"- source_doc: {n['source_doc'] or '(none)'}",
        f"- source_hash: {n['source_hash'] or '(none)'}",
        f"- raw_path: {n['raw_path'] or '(none)'}",
    ]
    for d in DIMENSIONS:
        did = d["id"]
        value = fm.get(did)
        if value:
            try:
                rendered = json.dumps(value, ensure_ascii=False)
            except Exception:
                rendered = str(value)
        else:
            rendered = n["sections"].get(did, "")
        rendered = (rendered or "").strip()
        if not rendered:
            continue
        lines.append(f"\n#### {did} / {d['name']}\n{rendered}")
    text = "\n".join(lines)
    if len(text) > per_note_chars:
        text = text[:per_note_chars] + "\n\n_(单份 note 过长，已截断)_"
    return text


CONSOLIDATE_SYSTEM = """你是基本面研究知识库的 synthesis agent。你的任务不是重写单份研报，
而是把同一家公司多份 report-level notes 合成为一份 company-level 当前视图。

原则：
1. 底层 note 是证据，不删除、不覆盖；综合稿只能引用来源 note。
2. 不强行填满 15 维。没有证据就写“暂无足够证据”。
3. 冲突不要平均掉：盈利预测、估值、市场空间、风险判断若分歧明显，保留为分歧项。
4. 新报告权重更高，但只有在假设更清楚、证据更强或数据更新时才压过旧报告。
5. 对过时信息标记 obsolete/low_weight，不把它从历史里抹掉。
6. 每个重要判断后用 [source: 文件名] 标出处。"""


CONSOLIDATE_TEMPLATE = """请基于下面 {note_count} 份 report-level notes，为 {ticker} 生成 company-level synthesis。

## 输出 JSON

只输出 JSON，不要 markdown 代码块：

{{
  "markdown": "# {ticker} 综合观点 ...",
  "conflicts": [
    {{
      "dimension": "financial_forecast|valuation_target|...",
      "topic": "冲突主题",
      "positions": [
        {{"source": "note文件名", "claim": "观点/数据", "assumption": "核心假设"}}
      ],
      "handling": "当前如何处理：区间/保留争议/标记过时/待补查"
    }}
  ],
  "obsolete": [
    {{"source": "note文件名", "claim": "被降权的旧信息", "reason": "为什么降权"}}
  ]
}}

## markdown 结构

# {ticker} 综合观点

## 一句话结论
## 共识
## 分歧与冲突
## 15 维综合
逐项按 canonical_15d_v1 的 15 个维度写。没有证据的维度写“暂无足够证据”，不要编。
## 当前最重要的跟踪问题
## 来源索引

## report-level notes

{notes_block}
"""


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception:
        return None


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"文件名冲突太多: {path}")


def build_company_synthesis(ticker: str, *, max_notes: int = 12, save: bool = True) -> dict:
    """生成 company-level synthesis，返回路径和冲突信息。"""
    t = _safe_ticker(ticker)
    notes = load_report_notes(t, max_notes=max_notes)
    if not notes:
        return {"error": f"{t} 没有 report-level notes"}

    cfg = load_config()
    model = cfg.get("cot", {}).get("extract_model") or cfg.get("agent", {}).get("model", "deepseek-v4-flash")
    notes_block = "\n\n".join(_compact_note(n) for n in notes)
    prompt = CONSOLIDATE_TEMPLATE.format(ticker=t, note_count=len(notes), notes_block=notes_block)

    try:
        client = make_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=CONSOLIDATE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return {"error": f"LLM 调用失败: {e}", "notes": notes}

    parsed = _parse_json(out)
    if not parsed:
        return {"error": f"LLM 输出非 JSON: {out[:300]}", "notes": notes}

    markdown = str(parsed.get("markdown") or "").strip()
    conflicts = parsed.get("conflicts") or []
    obsolete = parsed.get("obsolete") or []
    if not markdown:
        return {"error": "LLM 未返回 markdown", "notes": notes}

    today = date.today().isoformat()
    source_files = [n["path"].name for n in notes]
    fm_lines = [
        "---",
        f"ticker: {t}",
        f"created_at: {today}",
        "source: company_synthesis",
        f"note_schema: {SCHEMA_VERSION}",
        f"source_note_count: {len(notes)}",
        "source_notes:",
    ]
    fm_lines.extend(f"  - {name}" for name in source_files)
    fm_lines.append("---")
    full_md = "\n".join(fm_lines) + "\n\n" + markdown + "\n"

    result = {
        "ticker": t,
        "markdown": full_md,
        "conflicts": conflicts,
        "obsolete": obsolete,
        "source_notes": source_files,
    }
    if not save:
        return result

    COMPANY_THESES_DIR.mkdir(parents=True, exist_ok=True)
    CONFLICTS_DIR.mkdir(parents=True, exist_ok=True)
    synth_path = _unique_path(COMPANY_THESES_DIR / f"{t}_{today}.md")
    synth_path.write_text(full_md, encoding="utf-8")

    conflict_path = _unique_path(CONFLICTS_DIR / f"{t}_{today}.jsonl")
    with conflict_path.open("w", encoding="utf-8") as f:
        for item in conflicts:
            f.write(json.dumps({"ticker": t, "date": today, **item}, ensure_ascii=False) + "\n")
        for item in obsolete:
            f.write(json.dumps({"ticker": t, "date": today, "type": "obsolete", **item}, ensure_ascii=False) + "\n")

    result["path"] = str(synth_path)
    result["conflict_path"] = str(conflict_path)
    return result
