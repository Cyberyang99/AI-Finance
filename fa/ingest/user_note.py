"""用户论点录入 — 4 维度结构化笔记.

维度：
- core_thesis     核心论点（为什么看好/看坏）
- moat            护城河（最重要的 1-2 点）
- falsification   反证条件（什么情况证伪论点）
- horizon_size    预期时间窗口 + 最大仓位

存储路径: memory/theses/user/<ticker>_<yyyy-mm-dd>.md
召回权重: 默认 2.0（高于研报提取的 CoT，对齐用户思考逻辑）

自动结构化（P0 补强）：
- fa note -m "..." 单行快录时，LLM 自动把它拆到对应维度
- 原文同时保留到 raw_text 段，方便复原和审查
- 用户可加 --no-structure 跳过 LLM
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..memory.store import PROJECT_DIR

USER_THESES_DIR = PROJECT_DIR / "memory" / "theses" / "user"
# note 专属原文归档（独立于 CoT 的 memory/raw/）；放 user/ 下随 OneDrive 同步，
# load_user_notes 用非递归 glob("*.md") 不会扫到这里。
NOTE_RAW_DIR = USER_THESES_DIR / "_raw"


def archive_note_raw(src_path, file_hash_val: str) -> str:
    """把 note 的原始研报文件归档到 theses/user/_raw/<hash>_<原名>。

    返回相对 theses/user/ 的路径（如 _raw/<hash>_<名>），写入 note frontmatter 的 raw_path。
    同 hash 已归档则跳过拷贝。供回溯原文 + fa note --reextract 重抽。
    """
    import shutil
    src = Path(src_path).expanduser()
    NOTE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = NOTE_RAW_DIR / f"{file_hash_val}_{src.name}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return f"_raw/{dest.name}"

DIMENSIONS = [
    ("core_thesis", "核心论点（为什么看好/看坏，一两句话）"),
    ("moat", "护城河（最重要的 1-2 点，让它持续赚超额利润的根本原因）"),
    ("falsification", "反证条件（什么情况证伪论点，必须可观察可量化）"),
    ("horizon_size", "预期时间窗口 + 最大仓位（例：12 个月，最多 8%）"),
]


STRUCTURE_SYSTEM_PROMPT = """你是投资笔记整理员。你的工作是把用户随口写的一段话，拆解到 4 个固定维度。"""

STRUCTURE_USER_TEMPLATE = """用户对股票 {ticker} 的随手笔记：

---
{text}
---

请把上面这段话拆解到以下 4 个维度。**严格规则**：

1. **只用原文里出现的信息**，不要自己编造内容
2. 原文没提到某个维度的话，对应字段留空字符串
3. 用户原话里的关键短语尽量保留（保留语气和措辞）
4. 不要总结、不要重新组织、不要"美化"

## 4 个维度

- `core_thesis`: 核心论点（看好/看坏的根本判断）
- `moat`: 护城河 / 核心壁垒
- `falsification`: 反证条件（什么情况会证伪）
- `horizon_size`: 预期时间窗口或最大仓位

## 输出格式

严格 JSON（不要 markdown 代码块包裹）：

```
{{
  "core_thesis": "...",
  "moat": "...",
  "falsification": "...",
  "horizon_size": ""
}}
```

除 JSON 外不要任何其他内容。"""


def _parse_json_obj(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def auto_structure(ticker: str, freeform_text: str) -> dict:
    """LLM 把单行/短段笔记拆到 4 维度。失败返回空 dict（不阻塞，原文继续走 raw_text）。"""
    if not freeform_text or not freeform_text.strip():
        return {}

    from ..config import load_config, make_anthropic_client
    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")

    try:
        client = make_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            system=STRUCTURE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": STRUCTURE_USER_TEMPLATE.format(
                ticker=ticker, text=freeform_text
            )}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [NOTE] LLM 结构化失败: {e}（原文已保留到 raw_text）")
        return {}

    parsed = _parse_json_obj(text)
    if not parsed:
        print(f"  [NOTE] JSON 解析失败（原文已保留到 raw_text）")
        return {}

    out = {}
    for k, _ in DIMENSIONS:
        v = parsed.get(k, "")
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


STRUCTURE_DOC_TEMPLATE = """用户上传了一份外部研报/资料，用来形成对股票 {ticker} 的个人论点。

{comment_section}

## 资料原文（可能较长，已截断）

---
{text}
---

## 你的任务

请把这份资料 + 用户的评论（如有）整合成对 {ticker} 的 4 维度投资论点。**严格规则**：

1. **以用户评论为锚**：如果有用户评论，优先围绕评论里关心的角度组织 4 维度；评论里没提的内容可以补，但要克制
2. 没明确依据的字段留空字符串，不要瞎编
3. 用资料里的关键数据/表述支撑结论
4. 不要总结整篇研报，只抽出和投资判断相关的信息

## 4 个维度

- `core_thesis`: 核心论点（看好/看坏的根本判断）
- `moat`: 护城河 / 核心壁垒
- `falsification`: 反证条件（什么情况会证伪）
- `horizon_size`: 预期时间窗口或最大仓位

## 输出格式

严格 JSON（不要 markdown 代码块包裹）：

```
{{
  "core_thesis": "...",
  "moat": "...",
  "falsification": "...",
  "horizon_size": ""
}}
```

除 JSON 外不要任何其他内容。"""


def auto_structure_from_doc(
    ticker: str, doc_text: str, user_comment: str = "", max_chars: int = 30000
) -> dict:
    """从一份外部文档（PDF/PPT/DOCX 抽出来的文本）+ 可选用户评论拆 4 维度。

    与 auto_structure 区别：
    - 输入更长，需要截断
    - 优先围绕 user_comment 的角度组织
    - 上下文里明示 ticker，让 LLM 聚焦
    """
    if not doc_text or not doc_text.strip():
        return {}

    from ..config import load_config, make_anthropic_client
    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")

    text = doc_text
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n_(文档过长，已截断)_"

    comment_section = (
        f"## 用户评论（最重要的指引，请优先围绕这个角度组织 4 维度）\n\n{user_comment.strip()}\n"
        if user_comment and user_comment.strip()
        else "## 用户评论\n\n(无 — 自行从资料中提炼最重要的投资逻辑)\n"
    )

    try:
        client = make_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=STRUCTURE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": STRUCTURE_DOC_TEMPLATE.format(
                ticker=ticker, comment_section=comment_section, text=text
            )}],
        )
        out_text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [NOTE] LLM 结构化失败: {e}（原文已保留到 raw_text）")
        return {}

    parsed = _parse_json_obj(out_text)
    if not parsed:
        print(f"  [NOTE] JSON 解析失败（原文已保留到 raw_text）")
        return {}

    out = {}
    for k, _ in DIMENSIONS:
        v = parsed.get(k, "")
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def _safe_ticker(t: str) -> str:
    # 先过 ticker 规范化（港股去前导 0、A 股补零），再做文件名安全清洗
    try:
        from ..chat.resolver import _normalize_ticker
        t = _normalize_ticker(t.strip())
    except Exception:
        pass
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
    tags: Optional[list] = None,
    user_comment: str = "",
    source_doc: str = "",
    auto_classify: bool = True,
) -> Path:
    """保存用户论点到 memory/theses/user/<ticker>_<yyyy-mm-dd>.md.

    raw_text: 自由文本（fa note -m / -f 走这个），优先级高于结构化字段
    weight: 召回时的权重（默认 2.0 高于研报 CoT 的 1.0）
    user_comment: 上传外部文档时用户附的一句话评论，写到顶部最显著位置
    source_doc: 来源文件名（外部 PDF/PPT/DOCX 等），用于追溯
    """
    USER_THESES_DIR.mkdir(parents=True, exist_ok=True)
    t = _safe_ticker(ticker)
    today = date.today().isoformat()
    fname = f"{t}_{today}.md"
    path = USER_THESES_DIR / fname

    has_structured = any([core_thesis, moat, falsification, horizon_size])
    has_raw = bool(raw_text and raw_text.strip())
    has_comment = bool(user_comment and user_comment.strip())

    if not has_structured and not has_raw and not has_comment:
        raise ValueError("空论点：4 个维度、raw_text、user_comment 都为空")

    # 自动分类：sector/tags 缺失时，用与 CoT 同一套分类器给 note 打标（支持同业横向召回）
    tags = list(tags or [])
    if auto_classify and (not sector or not tags):
        try:
            from ..sectors import classify_doc
            blob = "\n".join(x for x in [user_comment, core_thesis, moat, raw_text] if x).strip()
            if blob:
                cls = classify_doc(f"{t}.md", blob, user_comment=user_comment)
                sector = sector or cls.get("sector_id")
                if not tags:
                    tags = cls.get("tags") or []
        except Exception as e:
            print(f"  [note] 自动分类失败（不影响保存）: {e}")

    lines = [
        "---",
        f"ticker: {t}",
        f"sector: {sector or ''}",
    ]
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    lines.append("source: user")
    if source_doc:
        lines.append(f"source_doc: {source_doc}")
    if has_comment:
        # frontmatter 里用单行存原始评论（转义换行），方便后续召回时直接读
        safe_comment = user_comment.strip().replace("\n", " ")
        lines.append(f"user_comment: {safe_comment}")
    lines.extend([
        f"created_at: {today}",
        f"weight: {weight}",
        f"confidence: high",
        "---",
        "",
        f"# {t} — 用户论点 ({today})",
        "",
    ])

    # 评论放在最顶部最显眼位置（如果有）
    if has_comment:
        lines.extend(["## 🗨 用户评论（投资角度的主观锚点）", "", user_comment.strip(), ""])

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
        # 来自外部文档时标注为"原文摘录"，否则"原始想法"
        section_title = "## 资料原文摘录" if source_doc else "## 备注 / 原始想法"
        lines.extend([section_title, "", raw_text.strip(), ""])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _rewrite_frontmatter_tags(text: str, sector: str, tags: list) -> str:
    """在 note 文本里就地写入/替换 frontmatter 的 sector 与 tags，保留 body 与其它字段。"""
    tags_line = f"tags: [{', '.join(tags)}]"
    parts = text.split("---", 2)
    if len(parts) < 3:  # 没有 frontmatter，补一个
        return f"---\nticker: \nsector: {sector}\n{tags_line}\nsource: user\n---\n\n{text}"
    out, seen_sector, seen_tags = [], False, False
    for ln in parts[1].strip("\n").split("\n"):
        if re.match(r"^\s*sector\s*:", ln):
            out.append(f"sector: {sector}"); seen_sector = True
        elif re.match(r"^\s*tags\s*:", ln):
            if tags:
                out.append(tags_line)
            seen_tags = True
        else:
            out.append(ln)
    if not seen_sector:
        out.append(f"sector: {sector}")
    if not seen_tags and tags:
        out.append(tags_line)
    return "---\n" + "\n".join(out) + "\n---" + parts[2]


def retag_all_notes(force: bool = False) -> dict:
    """给存量 note 补 sector+tags（与 CoT 同一套分类器）。

    force=False 只补缺标签的；force=True 连已有标签也重打。返回 {tagged, skipped, failed}。
    """
    from ..sectors import classify_doc
    from ..cot.loader import _parse_frontmatter, _parse_tags

    stats = {"tagged": 0, "skipped": 0, "failed": 0}
    if not USER_THESES_DIR.exists():
        return stats
    for p in sorted(USER_THESES_DIR.glob("*.md")):
        if not re.match(r"^(.+?)_(\d{4}-\d{2}-\d{2})\.md$", p.name):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            stats["failed"] += 1
            continue
        fm = _parse_frontmatter(text)
        if fm.get("sector") and _parse_tags(fm.get("tags", "")) and not force:
            stats["skipped"] += 1
            continue
        ticker = fm.get("ticker", p.stem)
        body = text.split("---", 2)[-1]
        try:
            cls = classify_doc(f"{ticker}.md", body, user_comment=fm.get("user_comment", ""))
        except Exception as e:
            print(f"  [retag] {p.name} 分类失败: {e}")
            stats["failed"] += 1
            continue
        sid = cls.get("sector_id") or fm.get("sector", "")
        tags = cls.get("tags") or _parse_tags(fm.get("tags", ""))
        p.write_text(_rewrite_frontmatter_tags(text, sid, tags), encoding="utf-8")
        print(f"  ✓ {p.name} → {sid} {tags}")
        stats["tagged"] += 1
    return stats


def load_user_notes(ticker: Optional[str] = None) -> list[dict]:
    """加载所有用户笔记。ticker 不空则过滤。

    返回 list[{"ticker", "path", "created_at", "content", "sector", "tags"}]，按时间倒序。
    sector/tags 来自 frontmatter（与 CoT 同一套分类），未打标的为 "" / []。
    """
    if not USER_THESES_DIR.exists():
        return []
    from ..cot.loader import _parse_frontmatter, _parse_tags

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
        fm = _parse_frontmatter(content)
        out.append({
            "ticker": t,
            "path": str(p),
            "created_at": d,
            "content": content,
            "sector": fm.get("sector", ""),
            "tags": _parse_tags(fm.get("tags", "")),
            "raw_path": fm.get("raw_path", ""),
            "source_doc": fm.get("source_doc", ""),
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def soft_delete_note(ticker: str, note_date: Optional[str] = None) -> dict:
    """软删除用户笔记：移到 theses/user/_archive/，不物理删除（可恢复）。

    note_date 指定 YYYY-MM-DD 只删那天的；不指定则删该 ticker 全部笔记。
    归档后 load_user_notes 不再返回（glob 不递归 _archive）。
    返回 {"archived": [路径...], "ticker": ...} 或 {"error": ...}。
    """
    import shutil
    from datetime import date as _date
    t = _safe_ticker(ticker)
    notes = [n for n in load_user_notes(ticker)
             if not note_date or n["created_at"] == note_date]
    if not notes:
        scope = f" {note_date}" if note_date else ""
        return {"error": f"没找到 {t}{scope} 的笔记"}
    archive_dir = USER_THESES_DIR / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = _date.today().strftime("%Y%m%d")
    archived = []
    for n in notes:
        src = Path(n["path"])
        target = archive_dir / f"deleted-{today}-{src.name}"
        i = 1
        while target.exists():
            target = archive_dir / f"deleted-{today}-{i}-{src.name}"
            i += 1
        shutil.move(str(src), str(target))
        archived.append(str(target))
    return {"ticker": t, "archived": archived}


# ── 12 维度 note 保存（新模板） ──

def save_note_12d(
    ticker: str,
    payload: dict,
    *,
    sector: Optional[str] = None,
    tags: Optional[list] = None,
    user_comment: str = "",
    source_doc: str = "",
    raw_path: str = "",
    source: str = "user",
    weight: float = 2.0,
    confidence: str = "high",
    filename_suffix: str = "",
) -> Path:
    """保存 12 维度结构化 note 到 memory/theses/user/<ticker>_<date>[_<suffix>].md.

    payload: {dim_id -> 内容}，参考 fa.note_template.empty_payload()
    sector/tags: 从关联 CoT 继承或人手指定
    filename_suffix: 文件名后缀，用于区分来源（如 "deep" → <ticker>_<date>_deep.md）
    """
    from ..note_template import render_markdown, filled_dims

    USER_THESES_DIR.mkdir(parents=True, exist_ok=True)
    t = _safe_ticker(ticker)
    today = date.today().isoformat()
    if filename_suffix:
        fname = f"{t}_{today}_{filename_suffix}.md"
    else:
        fname = f"{t}_{today}.md"
    path = USER_THESES_DIR / fname

    # 至少要填一个维度或有 comment
    if not filled_dims(payload) and not user_comment.strip():
        raise ValueError("空 note：12 维度全空且无 comment")

    md = render_markdown(
        ticker=t,
        payload=payload,
        sector=sector,
        tags=tags,
        created_at=today,
        user_comment=user_comment,
        source_doc=source_doc,
        raw_path=raw_path,
        source=source,
        weight=weight,
        confidence=confidence,
    )
    path.write_text(md, encoding="utf-8")
    return path


def append_to_today_note(ticker: str, raw_text: str = "", user_comment: str = "") -> Optional[Path]:
    """如果当日已有 note，追加 raw_text + comment 到末尾；否则返回 None。

    用于 fa note --append：同日多次写不覆盖，按时间戳追加段落。frontmatter 不动。
    """
    if not raw_text.strip() and not user_comment.strip():
        return None
    t = _safe_ticker(ticker)
    today = date.today().isoformat()
    path = USER_THESES_DIR / f"{t}_{today}.md"
    if not path.exists():
        return None

    from datetime import datetime as _dt
    now = _dt.now().strftime("%H:%M")
    existing = path.read_text(encoding="utf-8")
    parts = ["", "", "---", "", f"## 追加 ({now})", ""]
    if user_comment.strip():
        parts.extend([f"_角度提示_: {user_comment.strip()}", ""])
    if raw_text.strip():
        parts.append(raw_text.strip())
        parts.append("")
    path.write_text(existing + "\n".join(parts), encoding="utf-8")
    return path


def inherit_sector_tags(ticker: str) -> tuple[Optional[str], list]:
    """从该 ticker 已有的 CoT 文件读取 sector + tags，用于 note 继承。

    策略：找该 ticker 最近的 CoT 文件，读 frontmatter 拿 sector / tags。
    """
    from ..memory.store import PROJECT_DIR
    cot_root = PROJECT_DIR / "memory" / "knowledge" / "cot"
    if not cot_root.exists():
        return None, []

    t = _safe_ticker(ticker)
    matches = []
    for fp in cot_root.rglob("*.md"):
        if "_archive" in fp.parts:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        # 粗解 frontmatter，找 ticker
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        fm_text = parts[1]
        m = re.search(r"^ticker:\s*(.+)$", fm_text, re.MULTILINE)
        if not m:
            continue
        if m.group(1).strip().upper() != t:
            continue
        m_ca = re.search(r"^created_at:\s*(\S+)", fm_text, re.MULTILINE)
        created = m_ca.group(1) if m_ca else "2000-01-01"
        matches.append((created, fm_text))

    if not matches:
        return None, []

    matches.sort(reverse=True)
    fm_text = matches[0][1]

    sector = None
    m_s = re.search(r"^sector:\s*(\S+)", fm_text, re.MULTILINE)
    if m_s:
        sector = m_s.group(1).strip()

    tags = []
    m_t = re.search(r"^tags:\s*\[(.+?)\]", fm_text, re.MULTILINE)
    if m_t:
        tags = [t.strip() for t in m_t.group(1).split(",") if t.strip()]

    return sector, tags
