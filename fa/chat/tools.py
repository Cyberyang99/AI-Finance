"""Chat 工具集 — 把 fa 命令包装成 Anthropic tool use 的工具.

每个工具：
  - tool spec (传给 LLM)
  - run(args, state) → 返回字符串结果 (传回 LLM)

state 是个 dict，保存对话级状态（最近 ticker / 最近 sector 等）。
"""

from __future__ import annotations
from typing import Callable

# ── 工具实现 ──


def _do_find_ticker(args: dict, state: dict) -> str:
    from .resolver import resolve
    q = args.get("query", "").strip()
    if not q:
        return "错误：缺少 query 参数"
    res = resolve(q, limit=5)
    if not res:
        return f"未找到匹配 '{q}' 的股票。请尝试更精确的名称、拼音或代码。"
    lines = [f"找到 {len(res)} 个候选："]
    for i, r in enumerate(res, 1):
        lines.append(f"  {i}. {r['ticker']:14} {r['name']:30} ({r.get('country', '')})")
    # 自动设置 state 为 top-1
    state["last_ticker"] = res[0]["ticker"]
    state["last_ticker_candidates"] = [r["ticker"] for r in res]
    lines.append(f"\n已暂存最近 ticker = {res[0]['ticker']}（若不对请告诉我用第几个）")
    return "\n".join(lines)


def _resolve_ticker(arg_value: str | None, state: dict) -> str | None:
    """工具内部的 ticker 标准化：直接 ticker 就用；模糊词调 resolver。"""
    if not arg_value:
        return state.get("last_ticker")
    from .resolver import resolve, TICKER_RE
    v = arg_value.strip()
    if TICKER_RE.match(v):
        # normalize
        from .resolver import _normalize_ticker
        return _normalize_ticker(v)
    res = resolve(v, limit=1)
    return res[0]["ticker"] if res else None


def _do_add_note(args: dict, state: dict) -> str:
    """录入用户笔记。"""
    from ..ingest.user_note import save_user_note, auto_structure, auto_structure_from_doc, DIMENSIONS
    from ..ingest import ingest_file, SUPPORTED_EXT
    from pathlib import Path

    ticker = _resolve_ticker(args.get("ticker"), state)
    if not ticker:
        return "错误：找不到 ticker。请先用 find_ticker 解析公司名，或直接给标准 ticker。"

    message = (args.get("message") or "").strip()
    file_path = (args.get("file_path") or "").strip()
    comment = (args.get("comment") or "").strip()
    sector = (args.get("sector") or "").strip() or state.get("last_sector")

    raw_text = ""
    source_doc = ""
    structured = {k: "" for k, _ in DIMENSIONS}

    if file_path:
        p = Path(file_path).expanduser().resolve()
        if not p.exists():
            return f"错误：文件不存在 {p}"
        ext = p.suffix.lower()
        if ext in {".md", ".txt", ""}:
            raw_text = p.read_text(encoding="utf-8-sig")
            payload = raw_text if not comment else f"[评论] {comment}\n\n{raw_text}"
            extracted = auto_structure(ticker, payload)
            structured.update(extracted)
        elif ext in SUPPORTED_EXT:
            doc = ingest_file(p)
            raw_text = doc["text"]
            source_doc = doc["filename"]
            extracted = auto_structure_from_doc(ticker, raw_text, comment)
            structured.update(extracted)
        else:
            return f"错误：不支持的文件类型 {ext}"
    elif message:
        raw_text = message
        payload = message if not comment else f"[评论] {comment}\n\n{message}"
        extracted = auto_structure(ticker, payload)
        structured.update(extracted)
    elif comment:
        # 只有评论也允许入库
        pass
    else:
        return "错误：需要 message / file_path / comment 至少其一"

    try:
        path = save_user_note(
            ticker=ticker, **structured,
            raw_text=raw_text, sector=sector or None,
            user_comment=comment, source_doc=source_doc,
        )
        state["last_ticker"] = ticker
        if sector:
            state["last_sector"] = sector
        filled = [k for k, v in structured.items() if v]
        return (f"✓ 笔记已保存 → {path.name}\n"
                f"  ticker={ticker}, sector={sector or '(无)'}\n"
                f"  LLM 填充维度: {', '.join(filled) if filled else '(无)'}")
    except ValueError as e:
        return f"错误：{e}"


def _do_list_notes(args: dict, state: dict) -> str:
    from ..ingest.user_note import load_user_notes
    ticker = _resolve_ticker(args.get("ticker"), state) if args.get("ticker") else None
    notes = load_user_notes(ticker)
    if not notes:
        return f"无笔记记录{f' (ticker={ticker})' if ticker else ''}"
    lines = [f"=== 用户笔记 ({len(notes)} 条) ==="]
    for n in notes[:10]:
        body = n["content"].split("---", 2)[-1].strip().split("\n")[0:3]
        lines.append(f"\n[{n['created_at']}] {n['ticker']}")
        for ln in body:
            if ln.strip():
                lines.append(f"  {ln[:100]}")
    if len(notes) > 10:
        lines.append(f"\n... 还有 {len(notes) - 10} 条")
    return "\n".join(lines)


def _do_ingest_doc(args: dict, state: dict) -> str:
    """单文件投喂 — 抽文 + 自动分类 (sector + tags) + LLM 提 CoT + 入库.

    流程：抽文 → classify_doc (LLM 选主板块 + tags) → extract_cot → save_cot_file → 入库
    用户给的 sector 会作为"提示"传给 classify（不强制覆盖）；用户给的 tags 会合并。
    """
    from pathlib import Path
    from ..ingest import ingest_file, SUPPORTED_EXT
    from ..ingest.cot_extractor import extract_cot, save_cot_file
    from ..memory.store import MemoryStore
    from ..sectors import classify_doc, resolve_alias, display_sector

    file_path = (args.get("file_path") or "").strip()
    if not file_path:
        return "错误：缺少 file_path"
    p = Path(file_path).expanduser().resolve()
    if not p.exists():
        return f"错误：文件不存在 {p}"
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXT:
        return f"错误：不支持的格式 {ext} (支持 {sorted(SUPPORTED_EXT)})"

    ticker = _resolve_ticker(args.get("ticker"), state) if args.get("ticker") else None
    user_sector_hint_raw = (args.get("sector") or "").strip()
    user_tags_hint = list(args.get("tags") or [])
    comment = (args.get("comment") or "").strip()

    # 如果用户给的 sector 其实是个主题（如"光模块"→Theme_AIInterconnect），
    # 当 tag 用，不要覆盖业务主板块归属
    user_sector_hint = ""
    if user_sector_hint_raw:
        from ..sectors import resolve_alias as _ra, get_sector as _gs
        _resolved = _ra(user_sector_hint_raw)
        if _resolved:
            _info = _gs(_resolved)
            if _info and _info.get("parent") == "Theme":
                # 转为 tag
                if _info["name_cn"] not in user_tags_hint:
                    user_tags_hint.append(_info["name_cn"])
            else:
                user_sector_hint = user_sector_hint_raw  # 真业务一级，保留

    # ── [1/6] 抽文 ──
    print(f"     [1/6] 抽文中: {p.name}")
    try:
        doc = ingest_file(p)
    except Exception as e:
        return f"抽文失败: {e}"
    print(f"           ✓ {len(doc['text'])} 字 / {doc['pages']} 页 / hash={doc['hash']}")

    store = MemoryStore()
    existing = [r for r in store.list_ingested(limit=10000) if r["file_hash"] == doc["hash"]]
    if existing and existing[0].get("cot_count", 0) > 0:
        return (f"⚠ 这份文档之前已摄入并提炼过 {existing[0]['cot_count']} 条 CoT (hash 相同)。\n"
                f"   原 CoT 文件: {existing[0].get('cot_file')}\n"
                f"   想重新提炼请告诉我 'force 重抽'，或在终端跑 fa ingest <path> --force")

    # ── [2/6] 自动分类 ──
    print(f"     [2/6] 自动分类（GICS 主板块 + tags）...")
    # 用户的 sector hint 也喂给 LLM 作为额外上下文（用 alias 先归一）
    sector_hint_normalized = resolve_alias(user_sector_hint) if user_sector_hint else None
    cls_comment = comment
    if user_sector_hint and not sector_hint_normalized:
        cls_comment = f"[用户提示板块: {user_sector_hint}] {cls_comment}".strip()
    cls = classify_doc(doc["filename"], doc["text"], cls_comment)
    sector_id = cls["sector_id"]
    # 如果用户硬指定了 alias 能解析的 sector，尊重用户的（覆盖 LLM）
    if sector_hint_normalized:
        if sector_hint_normalized != sector_id:
            print(f"           ℹ 用户指定 sector={sector_hint_normalized}，覆盖 LLM 建议 {sector_id}")
        sector_id = sector_hint_normalized
    tags = list(cls.get("tags") or [])
    for t in user_tags_hint:
        if t and t not in tags:
            tags.append(t)
    print(f"           ✓ sector = {display_sector(sector_id)}")
    if tags:
        print(f"           ✓ tags   = {tags}")
    if cls.get("reasoning"):
        print(f"           ℹ 理由: {cls['reasoning']}")

    # ── [3/6] 提 CoT ──
    print(f"     [3/6] LLM 提炼 CoT{'（围绕用户角度）' if comment else ''}...")
    cots = extract_cot(doc["text"], user_comment=comment)
    if not cots:
        store.save_ingested_doc(
            source_path=doc["path"], filename=doc["filename"],
            file_type=doc["ext"], file_hash=doc["hash"],
            ticker=ticker, sector=sector_id, pages=doc["pages"],
            cot_count=0, cot_file=None,
        )
        return f"⚠ LLM 未能提炼出 CoT。文本太碎或 LLM 抽风，可换个角度重试。"
    print(f"           ✓ 提炼 {len(cots)} 条")

    # ── [4/6] 判断是否个股深度研究 + 抽 12 维度 note ──
    print(f"     [4/6] 判断是否个股深度研究...")
    from ..note_extractor import is_individual_research, extract_12d
    from ..ingest import save_note_12d, inherit_sector_tags
    from ..note_template import filled_dims
    from ..chat.resolver import resolve as ticker_resolve

    check = is_individual_research(doc["filename"], doc["text"], comment)
    note_path = None
    note_payload = None
    note_ticker = ticker  # 默认沿用用户给的

    # 决策树：
    # 1. LLM 判断 yes → 抽 note
    # 2. LLM 判断 no 但用户明确给了 ticker → 强制抽 note（兜底）
    # 3. LLM 判断 no 且用户没给 ticker → 真跳过
    should_extract = False
    if check["is_individual_research"]:
        should_extract = True
        if not note_ticker and check.get("company_name_cn"):
            tres = ticker_resolve(check["company_name_cn"], limit=1)
            if tres:
                note_ticker = tres[0]["ticker"]
                print(f"           ℹ 识别公司: {check['company_name_cn']} → {note_ticker}")
        print(f"           ✓ 是个股深度研究（{check.get('confidence', '?')} 置信度），目标 ticker = {note_ticker or '?'}")
    elif ticker:
        should_extract = True
        print(f"           ℹ LLM 判否但用户明确给了 ticker={ticker}，仍尝试抽 note（兜底）")
        print(f"             (LLM 理由: {check.get('reasoning', '')[:60]})")
    else:
        print(f"           ✓ 非个股研究 ({check.get('reasoning', '')[:60]})，跳过 note")

    if should_extract and note_ticker:
        print(f"     [5/6] 抽 12 维度 note...")
        note_payload = extract_12d(note_ticker, doc["text"], comment)
        filled = filled_dims(note_payload)
        if filled:
            print(f"           ✓ 填了 {len(filled)}/12 维度: {', '.join(filled[:6])}{'...' if len(filled) > 6 else ''}")
        else:
            print(f"           ⚠ 12 维度全空（可能 LLM 抽取失败或文档信息不足）")
            note_payload = None
    else:
        print(f"     [5/6] 跳过 note 抽取")

    # ── [6/6] 写文件 + 入库 ──
    print(f"     [6/6] 写 CoT + (如有) note + 入库...")
    cot_path = save_cot_file(cots, ticker, sector_id, doc["filename"], doc["hash"],
                             user_comment=comment, tags=tags)
    rel = str(cot_path.relative_to(cot_path.parents[3]))
    print(f"           ✓ CoT → {cot_path.name}")

    if note_payload and note_ticker:
        try:
            note_path = save_note_12d(
                ticker=note_ticker,
                payload=note_payload,
                sector=sector_id,
                tags=tags,
                user_comment=comment,
                source_doc=doc["filename"],
                source="llm_ingest",
            )
            print(f"           ✓ note → {note_path.name}")
        except ValueError as e:
            print(f"           ⚠ note 保存失败: {e}")
            note_path = None

    store.save_ingested_doc(
        source_path=doc["path"], filename=doc["filename"],
        file_type=doc["ext"], file_hash=doc["hash"],
        ticker=ticker, sector=sector_id, pages=doc["pages"],
        cot_count=len(cots), cot_file=rel,
    )
    print(f"           ✓ 摄入记录入库")

    # 更新会话状态
    if note_ticker:
        state["last_ticker"] = note_ticker
    elif ticker:
        state["last_ticker"] = ticker
    state["last_sector"] = sector_id
    if tags:
        state["last_tags"] = tags

    # 输出总结
    summary_lines = [
        f"\n✓ 完成。{len(cots)} 条 CoT 已入库" +
        (f" + 12 维度 note ({len(filled_dims(note_payload))}/12 维度)" if note_payload else ""),
        f"  主板块: {display_sector(sector_id)}",
        f"  tags:   {tags if tags else '(无)'}",
        f"  ticker: {note_ticker or ticker or '(未绑定)'}",
        f"  CoT 文件: {cot_path.name}",
    ]
    if note_path:
        summary_lines.append(f"  note 文件: {note_path.name}")
    if comment:
        summary_lines.append(f"  你的角度: {comment[:80]}")

    # 前 3 条 CoT 摘要
    summary_lines.append(f"\n前 3 条 CoT 摘要：")
    for i, c in enumerate(cots[:3], 1):
        summary_lines.append(f"  {i}. [信号 {c['signal']}/10] {c['trigger']}")
        cot_preview = c['COT'][:100].replace('\n', ' ')
        summary_lines.append(f"     → {cot_preview}{'...' if len(c['COT']) > 100 else ''}")
    if len(cots) > 3:
        summary_lines.append(f"  ... 还有 {len(cots) - 3} 条")

    # 如果产出了 note，提示已填关键量化字段
    if note_payload:
        from ..note_template import is_filled, JSON_DIM_IDS
        json_filled = [k for k in JSON_DIM_IDS if is_filled(note_payload.get(k))]
        if json_filled:
            summary_lines.append(f"\n📊 note 量化字段已填: {json_filled}")
    return "\n".join(summary_lines)


def _do_import_files(args: dict, state: dict) -> str:
    """批量导入目录/文件。chat 里也能真跑（流式），不再踢用户回终端。

    dry_run=true: 只扫描预览
    dry_run=false (默认): 真跑，循环调用底层 ingest，每个文件流式进度
    """
    from pathlib import Path
    from ..ingest.runner import scan_dir, classify_file
    path = args.get("path", "").strip()
    if not path:
        return "错误：缺少 path"
    sector = (args.get("sector") or "").strip() or state.get("last_sector") or ""
    ticker = _resolve_ticker(args.get("ticker"), state) if args.get("ticker") else None
    comment = (args.get("comment") or "").strip()
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return f"错误：路径不存在 {target}"

    files = [target] if target.is_file() else scan_dir(target, recursive=True)
    if not files:
        return f"无可识别文件: {target}"

    research = [f for f in files if classify_file(f) == "research"]
    user_notes = [f for f in files if classify_file(f) == "user_note"]

    if args.get("dry_run"):
        lines = [f"[预览] 扫到 {len(files)} 个文件：研报 {len(research)}, 笔记 {len(user_notes)}"]
        for f in files[:20]:
            lines.append(f"  {classify_file(f):<10} {f.name}")
        if len(files) > 20:
            lines.append(f"  ... 还有 {len(files) - 20} 个")
        return "\n".join(lines)

    # 真跑
    print(f"     批量入库：{len(research)} 篇研报{f'，sector={sector}' if sector else ''}")
    if len(research) > 5:
        print(f"     ⚠ 数量较多 ({len(research)} 篇)，预估耗时 {len(research) * 30}s 左右...")

    ok_count = 0
    skip_count = 0
    fail_count = 0
    total_cots = 0
    for i, f in enumerate(research, 1):
        print(f"\n     [{i}/{len(research)}] {f.name}")
        result = _do_ingest_doc(
            {"file_path": str(f), "ticker": ticker, "sector": sector, "comment": comment},
            state,
        )
        if result.startswith("✓") or "已入库" in result or "完成" in result:
            ok_count += 1
            # 解析 CoT 数量
            import re as _re
            m = _re.search(r"(\d+) 条 CoT 已入库", result)
            if m:
                total_cots += int(m.group(1))
        elif "已摄入并提炼过" in result:
            skip_count += 1
        else:
            fail_count += 1
        # 简短回显（避免噪音）
        print(f"     {result.splitlines()[0] if result.splitlines() else '?'}")

    return (f"\n✓ 批量完成。成功 {ok_count} / 跳过 {skip_count} / 失败 {fail_count}，"
            f"共入库 {total_cots} 条 CoT")


def _do_deep(args: dict, state: dict) -> str:
    ticker = _resolve_ticker(args.get("ticker"), state)
    if not ticker:
        return "错误：找不到 ticker"
    return (f"⚠ fa deep 需要 3-5 分钟，建议退出 chat 用命令：\n"
            f"  fa deep {ticker}\n"
            f"  跑完再回来 chat 问分析结论。")


def _do_list_cot(args: dict, state: dict) -> str:
    from ..cot import load_cots
    from ..sectors import resolve_alias, get_sector
    sector_raw = (args.get("sector") or "").strip() or state.get("last_sector")
    sector = None
    tag_from_sector = None
    if sector_raw:
        resolved = resolve_alias(sector_raw)
        if resolved:
            info = get_sector(resolved)
            if info and info.get("parent") == "Theme":
                # 用户给的"板块"其实是个主题 → 转成 tag 查询
                tag_from_sector = info["name_cn"]
            else:
                sector = resolved
        else:
            sector = sector_raw  # 解析不到也允许直接用（兼容历史目录）
    tag = (args.get("tag") or "").strip() or tag_from_sector or None
    min_signal = int(args.get("min_signal") or 0)
    cots = load_cots(sector=sector, min_signal=min_signal, tag=tag)
    if not cots:
        return f"无 CoT (sector={sector}, tag={tag}, min_signal={min_signal})"
    lines = [f"=== CoT 列表 ({len(cots)} 条) ==="]
    for c in cots[:15]:
        lines.append(f"  [{c['signal']}/10] {c['trigger']}")
        tags_str = f", tags={c.get('_tags')}" if c.get('_tags') else ""
        lines.append(f"    sector={c['_sector']}{tags_str}")
    if len(cots) > 15:
        lines.append(f"... 还有 {len(cots) - 15} 条")
    if sector:
        state["last_sector"] = sector
    return "\n".join(lines)


def _do_status(args: dict, state: dict) -> str:
    import io, contextlib
    from ..cli import _cmd_status
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_status()
    return buf.getvalue()


def _do_dash(args: dict, state: dict) -> str:
    import io, contextlib
    from ..cli import _cmd_dash
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_dash()
    return buf.getvalue()


def _do_sectors(args: dict, state: dict) -> str:
    import io, contextlib
    from ..cli import _cmd_sectors
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_sectors()
    return buf.getvalue()


# ── Tool registry ──

TOOLS_SPEC = [
    {
        "name": "find_ticker",
        "description": "根据公司中文名/拼音/英文名/数字代码查股票 ticker。支持 A 股 (SHG/SHE)、港股 (HK)、美股 (US)。返回最多 5 个候选并自动暂存 top-1 为当前会话的最近 ticker。当用户提到任何公司名而你不确定 ticker 时调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "公司名/拼音/代码，例如：茅台、moutai、600519、Apple、智谱"}
            },
            "required": ["query"]
        },
    },
    {
        "name": "add_note",
        "description": "录入用户对某只股票的投资笔记。三种用法（互斥优先级 file > message > 仅 comment）：1) message: 一句话快录，LLM 自动拆 4 维度。2) file_path: 上传外部研报 PDF/PPT/DOCX/MD，LLM 围绕 comment 角度拆 4 维度。3) 只给 comment 也行，作为主观锚点入库。ticker 若不给则用最近 ticker。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码 (可选，默认用最近 ticker)。可以是标准 ticker 或公司名"},
                "message": {"type": "string", "description": "一句话快录"},
                "file_path": {"type": "string", "description": "外部文件路径，绝对路径或 ~ 开头"},
                "comment": {"type": "string", "description": "一句话评论/角度提示，LLM 拆 4 维度时优先围绕这个角度"},
                "sector": {"type": "string", "description": "所属板块 (可选)"}
            },
            "required": []
        },
    },
    {
        "name": "list_notes",
        "description": "列出用户笔记。不传 ticker 列全部，传 ticker 只列该票。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码 (可选)"}
            },
            "required": []
        },
    },
    {
        "name": "ingest_doc",
        "description": "【最常用】单文件投喂：抽文 + 自动分类（GICS 主板块 + 主题 tags）+ LLM 提炼 CoT + 入库。一气呵成。当用户给一个具体文件路径（pdf/pptx/docx/xlsx）+ 一段描述时，**默认就用这个**。不要拆成 import_files + dry_run。sector 通常不用你指定——LLM 内部分类器会从文档内容自动选主板块；只有用户明确指定时才覆盖。tags 类似。会流式 print 进度并返回前 3 条 CoT 摘要。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件绝对路径"},
                "ticker": {"type": "string", "description": "股票代码（可选，默认用 last_ticker）"},
                "sector": {"type": "string", "description": "主板块（**绝大多数情况不用填**，让自动分类器决定。只有用户明确说『归到 X 板块』才指定）"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "用户额外想加的主题标签（可选；自动分类器会自己生成 tags，这里是补充）"},
                "comment": {"type": "string", "description": "用户角度提示，LLM 提 CoT 时会优先围绕这个角度。用户的原始描述就是最好的 comment。"}
            },
            "required": ["file_path"]
        },
    },
    {
        "name": "import_files",
        "description": "批量扫描目录/文件并导入。**仅在用户明确说『批量导入』『整个目录』时用**。单个文件请用 ingest_doc。chat 模式下也是真跑——不要把命令贴给用户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径"},
                "sector": {"type": "string", "description": "板块（应用到所有文件）"},
                "ticker": {"type": "string", "description": "可选 ticker 绑定"},
                "comment": {"type": "string", "description": "用户角度（应用到所有文件）"},
                "dry_run": {"type": "boolean", "description": "true 时只预览不入库；用户说『预览』『先看看』才用 true，默认 false"}
            },
            "required": ["path"]
        },
    },
    {
        "name": "deep_analyze",
        "description": "对某只股票做五维深度分析。耗时 3-5 分钟，会提示用户去命令行执行。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码（可选，默认最近 ticker）"}
            },
            "required": []
        },
    },
    {
        "name": "list_cot",
        "description": "列出已提炼的思维链 CoT。可按主板块 (sector) 或主题 (tag) 过滤；tag 是跨板块召回的关键能力。",
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "主板块 (sector_id 或别名)，例：CapitalGoods / 半导体"},
                "tag": {"type": "string", "description": "主题 tag，跨板块召回。例：'AI 主题' / '燃气轮机'"},
                "min_signal": {"type": "integer", "description": "信号强度下限 1-10"}
            },
            "required": []
        },
    },
    {
        "name": "status",
        "description": "系统状态：模型、API Key、数据库位置、活跃论点数等。",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "dashboard",
        "description": "仪表盘：活跃论点数、待回顾数、胜率、平均超额收益。",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_sectors",
        "description": "列出已知板块/主题。",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


HANDLERS: dict[str, Callable[[dict, dict], str]] = {
    "find_ticker": _do_find_ticker,
    "add_note": _do_add_note,
    "list_notes": _do_list_notes,
    "ingest_doc": _do_ingest_doc,
    "import_files": _do_import_files,
    "deep_analyze": _do_deep,
    "list_cot": _do_list_cot,
    "status": _do_status,
    "dashboard": _do_dash,
    "list_sectors": _do_sectors,
}


def dispatch(tool_name: str, tool_input: dict, state: dict) -> str:
    """根据 tool_use block 派发到对应 handler。"""
    handler = HANDLERS.get(tool_name)
    if not handler:
        return f"错误：未知工具 {tool_name}"
    try:
        return handler(tool_input or {}, state)
    except Exception as e:
        import traceback
        return f"工具 {tool_name} 执行出错：{e}\n{traceback.format_exc()[:500]}"
