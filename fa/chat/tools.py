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

    from ..ingest import clean_user_path
    message = (args.get("message") or "").strip()
    file_path = clean_user_path(args.get("file_path") or "")
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
        if ext in SUPPORTED_EXT:
            # 研报文件 → 15 维深度 note（pdf/docx/pptx/xlsx/txt）
            from ..note_extractor import extract_12d
            from ..note_template import filled_dims
            from ..ingest import save_note_12d
            from ..ingest.user_note import archive_note_raw
            from ..sectors import classify_doc, display_sector
            doc = ingest_file(p)
            cls = classify_doc(doc["filename"], doc["text"], user_comment=comment)
            sid = sector or cls.get("sector_id")
            tags = cls.get("tags") or []
            if cls.get("suggested_tags"):
                print(f"     ⚠ 疑似新主题未归类: {cls['suggested_tags']} — 未自动建 tag。"
                      f"如确需，请加入 memory/sectors.yaml 后重抽")
            print(f"     抽 15 维 note...")
            payload = extract_12d(ticker, doc["text"], comment)
            filled = filled_dims(payload)
            if not filled and not comment:
                return "⚠ 15 维全空（文档信息不足或抽取失败，可换角度重试或检查文档）"
            # 归档原文（note 专属目录），供回溯 + 重抽
            raw_rel = ""
            try:
                raw_rel = archive_note_raw(p, doc["hash"])
            except Exception as e:
                print(f"     ⚠ 原文归档失败（不影响 note）: {e}")
            note_path = save_note_12d(ticker=ticker, payload=payload, sector=sid, tags=tags,
                                      user_comment=comment, source_doc=doc["filename"], raw_path=raw_rel)
            state["last_ticker"] = ticker
            if sid:
                state["last_sector"] = sid
            return (f"✓ 15 维 note 已保存 → {note_path.name}\n"
                    f"  ticker={ticker} · sector={display_sector(sid) if sid else '(无)'} · "
                    f"tags={'/'.join(tags) or '(无)'}\n"
                    f"  填了 {len(filled)}/15 维: {', '.join(filled[:8])}"
                    f"{'…' if len(filled) > 8 else ''}")
        elif ext in {".md", ""}:
            raw_text = p.read_text(encoding="utf-8-sig")
            payload = raw_text if not comment else f"[评论] {comment}\n\n{raw_text}"
            extracted = auto_structure(ticker, payload)
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
        # 读保存后实际分到的 sector/tags（save_user_note 内部会自动分类，本地 sector 变量可能为空）
        from ..cot.loader import _parse_frontmatter, _parse_tags
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        saved_sector = fm.get("sector", "") or "(无)"
        saved_tags = _parse_tags(fm.get("tags", ""))
        state["last_ticker"] = ticker
        if saved_sector and saved_sector != "(无)":
            state["last_sector"] = saved_sector
        filled = [k for k, v in structured.items() if v]
        return (f"✓ 笔记已保存 → {path.name}\n"
                f"  ticker={ticker} · sector={saved_sector} · tags={'/'.join(saved_tags) or '(无)'}\n"
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

    from ..ingest import clean_user_path
    file_path = clean_user_path(args.get("file_path") or "")
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
    force = bool(args.get("force"))

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
    if existing and existing[0].get("cot_count", 0) > 0 and not force:
        return (f"⚠ 这份文档之前已摄入并提炼过 {existing[0]['cot_count']} 条 CoT (hash 相同)。\n"
                f"   原 CoT 文件: {existing[0].get('cot_file')}\n"
                f"   想重新提炼请告诉我 'force 重抽'，或在终端跑 fa ingest <path> --force")
    if existing and force and existing[0].get("cot_file"):
        from ..memory.store import PROJECT_DIR
        old_cot = PROJECT_DIR / "memory" / existing[0]["cot_file"]
        if old_cot.exists():
            old_cot.unlink()
            print(f"           ↺ force 重抽：删除旧 CoT 文件 {old_cot.name}")

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
    if cls.get("suggested_tags"):
        print(f"           ⚠ 疑似新主题未归类: {cls['suggested_tags']} — 未自动建 tag。"
              f"如确需，请加入 memory/sectors.yaml 后重抽")
    if cls.get("reasoning"):
        print(f"           ℹ 理由: {cls['reasoning']}")

    # ── [3/6] 提 CoT ──
    print(f"     [3/6] LLM 提炼 CoT{'（围绕用户角度）' if comment else ''}...")
    result = extract_cot(doc["text"], user_comment=comment)
    cots = result["cots"]
    quality_rating = result.get("quality_rating", 0)
    quality_reason = result.get("quality_reason", "")
    if not cots:
        store.save_ingested_doc(
            source_path=doc["path"], filename=doc["filename"],
            file_type=doc["ext"], file_hash=doc["hash"],
            ticker=ticker, sector=sector_id, pages=doc["pages"],
            cot_count=0, cot_file=None,
        )
        return f"⚠ LLM 未能提炼出 CoT。文本太碎或 LLM 抽风，可换个角度重试。"
    if quality_rating > 0:
        print(f"           ★ 研报质量: {'⭐' * quality_rating} ({quality_rating}/5) {quality_reason}")
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
                             user_comment=comment, tags=tags,
                             quality_rating=quality_rating, quality_reason=quality_reason)
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
    keyword = (args.get("keyword") or "").strip().lower()
    cots = load_cots(sector=sector, min_signal=min_signal, tag=tag)
    if keyword:
        cots = [c for c in cots
                if keyword in f"{c.get('trigger','')} {c.get('COT','')}".lower()]
    if not cots:
        return f"无 CoT (sector={sector}, tag={tag}, min_signal={min_signal}, keyword={keyword or '无'})"
    full = bool(args.get("full"))
    # full=true 时直接给完整推理链（含子分），省去逐条再 get_cot 的来回；
    # 标题模式 cap 15，全文模式 cap 8（避免一次刷屏）。
    cap = 8 if full else 15
    cots.sort(key=lambda c: -int(c.get("signal", 0) or 0))
    lines = [f"=== CoT 列表 ({len(cots)} 条{('，完整推理' if full else '')}) ==="]
    for c in cots[:cap]:
        tags = c.get("_tags") or []
        theme = "、".join(tags) if tags else "(未打主题)"
        if full:
            sub = ""
            if all(k in c for k in ("transmission", "history", "recency")):
                sub = f"  (传导{c['transmission']}·历史{c['history']}·时效{c['recency']})"
            lines.append(f"\n[{c['signal']}/10] {c['trigger']}{sub}")
            lines.append(f"  主题={theme} · 行业={c['_sector']} · 来源={c.get('_source','?')} · id={c.get('_cot_id','?')}")
            lines.append(f"  {c.get('COT','').strip()}")
        else:
            lines.append(f"  [{c['signal']}/10] {c['trigger']}")
            lines.append(f"    主题={theme}  ·  行业={c['_sector']}")
    if len(cots) > cap:
        lines.append(f"\n... 还有 {len(cots) - cap} 条（缩小 tag/提高 min_signal 或分批看）")
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
                "comment": {"type": "string", "description": "用户角度提示，LLM 提 CoT 时会优先围绕这个角度。用户的原始描述就是最好的 comment。"},
                "force": {"type": "boolean", "description": "强制重抽：该文档之前已提炼过 CoT 时，删旧的重新提炼。用户说『force 重抽』『重新提炼』『覆盖重跑』时传 true，默认 false。"}
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
        "description": ("列出已提炼的思维链 CoT。可按主板块 (sector) 或主题 (tag) 过滤；tag 是跨板块召回的关键能力。"
                        "**用户要看某筛选集（如某主题 + 高分）的完整推理链时，直接传 full=true，一次返回全文，"
                        "不要再逐条 get_cot。** 默认按 signal 倒序。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "主板块 (sector_id 或别名)，例：CapitalGoods / 半导体"},
                "tag": {"type": "string", "description": "主题 tag，跨板块召回。例：'AI 主题' / '燃气轮机'"},
                "min_signal": {"type": "integer", "description": "信号强度下限 1-10"},
                "keyword": {"type": "string", "description": "在 trigger+正文里再做关键词过滤（可选）"},
                "full": {"type": "boolean", "description": "true 则输出每条的完整推理链 + 子分（最多 8 条），否则只列标题（最多 15 条）"}
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
    {
        "name": "reclassify_cot",
        "description": ("改 CoT 文件的归类（sector 一级 + tags 二级）。当用户说'把豪迈那份重新归到 X 板块'"
                        "或'给那个 CoT 加上 AI 算力 tag' 时用。query 可以是文件名片段 / source_hash 前缀 /"
                        "公司名（用于定位文件）。new_sector 或 new_tags 至少给一个。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "文件名片段 / source_hash 前缀 / ticker 字符等用于定位文件"},
                "new_sector": {"type": "string", "description": "新的 GICS sector，可空"},
                "new_tags": {"type": "array", "items": {"type": "string"}, "description": "新的主题 tags 列表，可空（不传则不改）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_memory",
        "description": ("【召回】跨 CoT 正文 + 用户笔记做关键词检索，返回带定位 id 的命中片段。"
                        "当用户问『有没有关于 X 的研究/逻辑』『提到 Y 的都有哪些』时用。"
                        "拿到命中后，要看某条全文用 get_cot，看笔记全文用 get_note。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，例：燃气轮机 / 算力租赁 / 国产替代"},
                "scope": {"type": "string", "enum": ["all", "cot", "note"], "description": "检索范围，默认 all"},
                "limit": {"type": "integer", "description": "每类最多返回多少条，默认 12"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_cot",
        "description": ("【问答关键】返回某份 CoT 文件的全文（所有思维链正文），让你能据此回答用户的具体问题。"
                        "当用户问『X 的核心逻辑/推理链是什么』『那份研报讲了啥』时，先 get_cot 拿全文再回答，不要凭空答。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "cot_id 前缀 / source 文件名片段 / 公司名，用于定位文件"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_note",
        "description": ("返回某 ticker 的用户笔记全文（12 维度/核心论点）。用户问『我对 X 的看法/论点是什么』时用。"
                        "同一公司可能有多条不同日期的笔记（观点会随时间变）：默认给最新一条；"
                        "用户说『看历史/之前怎么写的/观点怎么变的』传 all=true；要某天那条传 date。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码或公司名，可空（默认最近 ticker）"},
                "date": {"type": "string", "description": "YYYY-MM-DD，取该日期的历史笔记全文（可选）"},
                "all": {"type": "boolean", "description": "true 时返回全部笔记（新→旧），用于看观点演变（可选）"},
            },
            "required": [],
        },
    },
    {
        "name": "merge_cot",
        "description": ("同一 sector 内把多份 CoT 做 LLM 聚类合并去重（产出 merged 文件，原文件归档）。"
                        "用户说『合并 X 板块的 CoT』『去重』时用。默认真跑；用户说『预览/先看看』传 dry_run=true。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "板块 (sector_id 或别名)，可空则用最近 sector"},
                "dry_run": {"type": "boolean", "description": "true 只预览不写盘，默认 false"},
            },
            "required": [],
        },
    },
    {
        "name": "regroup_cot",
        "description": "单份 CoT 文件内部重新分组合并去重（不重新调 LLM 抽取，纯本地）。用户对某份报告里 CoT 重复/冗余不满时用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "cot_id 前缀 / source 文件名片段 定位文件"},
                "dry_run": {"type": "boolean", "description": "true 只预览，默认 false"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "rescore_cot",
        "description": "对单份 CoT 文件仅重新打分（signal），保留 trigger/正文不变。改了打分权重后用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "cot_id 前缀 / source 文件名片段 定位文件"},
                "dry_run": {"type": "boolean", "description": "true 只预览，默认 false"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "delete_cot",
        "description": ("软删除一份 CoT（移到 _archive/，可恢复，绝不物理删）。从 list/搜索/投票中移除。"
                        "用户说『删掉 X 那份 CoT』时用。删前简短确认一下删的是哪份。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "cot_id 前缀 / source 文件名片段 / 公司名 定位要删的文件"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "delete_note",
        "description": "软删除某 ticker 的用户笔记（移到 _archive/，可恢复）。不给 date 删该 ticker 全部，给 date 只删那天。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "股票代码或公司名，可空（默认最近 ticker）"},
                "date": {"type": "string", "description": "YYYY-MM-DD，可空；只删指定日期那条"},
            },
            "required": [],
        },
    },
]


def _do_reclassify_cot(args: dict, state: dict) -> str:
    from ..cot.local_ops import reclassify_file
    query = args.get("query") or ""
    new_sector = args.get("new_sector") or None
    new_tags = args.get("new_tags")
    if new_tags is not None and not isinstance(new_tags, list):
        return "工具参数错误：new_tags 必须是字符串数组"
    res = reclassify_file(query, new_sector=new_sector, new_tags=new_tags)
    if "error" in res:
        return f"✗ {res['error']}"
    lines = [f"✓ 已改 → {res['file']}"]
    if res["moved"]:
        lines.append(f"  目录: {res['old_sector']} → {res['new_sector']}")
    elif new_sector:
        lines.append(f"  sector: {res['old_sector']} → {res['new_sector']} (同板块，未搬目录)")
    if new_tags is not None:
        lines.append(f"  tags: {res['old_tags']} → {res['new_tags']}")
    return "\n".join(lines)


# ── Block 3: 召回 / 查询 ──

def _do_search_memory(args: dict, state: dict) -> str:
    """跨 CoT / note 关键词检索，返回带定位 id 的命中片段。"""
    from ..cot import load_cots
    from ..ingest.user_note import load_user_notes
    q = (args.get("query") or "").strip()
    if not q:
        return "错误：缺少 query"
    scope = (args.get("scope") or "all").lower()
    ql = q.lower()
    # 按空格分词，全部命中才算（AND）；单词查询即退化为原来的子串匹配。
    # 修复：多词查询（如"云计算 涨价 毛利率"）整串永远不是连续子串，过去必然落空。
    tokens = [t for t in ql.split() if t]

    def _hit(hay: str) -> bool:
        return all(tok in hay for tok in tokens) if tokens else False

    limit = int(args.get("limit") or 12)
    lines = []

    if scope in ("all", "cot"):
        cot_hits = []
        for c in load_cots():
            hay = f"{c.get('trigger','')} {c.get('COT','')} {' '.join(c.get('_tags') or [])} {c.get('_source','')}".lower()
            if _hit(hay):
                cot_hits.append(c)
        cot_hits.sort(key=lambda c: -int(c.get("signal", 0) or 0))
        if cot_hits:
            lines.append(f"=== CoT 命中 {len(cot_hits)} 条（按信号排序，展示前 {min(limit, len(cot_hits))}）===")
            for c in cot_hits[:limit]:
                lines.append(f"  [{c['signal']}/10] {c['trigger']}")
                tags = c.get("_tags") or []
                theme = "、".join(tags) if tags else "(未打主题)"
                lines.append(f"    id={c['_cot_id']}  主题={theme}  行业={c['_sector']}  来源={c['_source']}")
                snippet = c.get("COT", "")[:120].replace("\n", " ")
                lines.append(f"    {snippet}{'...' if len(c.get('COT',''))>120 else ''}")

    if scope in ("all", "note"):
        note_hits = [n for n in load_user_notes() if _hit(n["content"].lower())]
        if note_hits:
            lines.append(f"\n=== 笔记命中 {len(note_hits)} 条 ===")
            for n in note_hits[:limit]:
                # 抓含关键词的那一行做片段
                snippet = ""
                for ln in n["content"].split("\n"):
                    if ln.strip() and not ln.startswith("#") and any(tok in ln.lower() for tok in tokens):
                        snippet = ln.strip()[:120]
                        break
                lines.append(f"  [{n['created_at']}] {n['ticker']}  {snippet}")

    if not lines:
        return f"没有命中 '{q}' 的 CoT 或笔记（scope={scope}）。可换个关键词，或用 list_cot 看全量。"
    lines.append("\n（要看某条全文，用 get_cot 传 id/source 片段；看笔记全文用 get_note 传 ticker）")
    return "\n".join(lines)


def _do_get_cot(args: dict, state: dict) -> str:
    """返回某份 CoT 文件全文（所有链），供 LLM 据此回答问题。"""
    from ..cot.local_ops import render_file_full
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：缺少 query（cot_id 前缀 / source 文件名片段 / 公司名）"
    res = render_file_full(query)
    if "error" in res:
        return f"✗ {res['error']}"
    if res.get("sector"):
        state["last_sector"] = res["sector"]
    return res["text"]


def _do_get_note(args: dict, state: dict) -> str:
    """返回某 ticker 的笔记全文。

    默认最新一条；date=YYYY-MM-DD 取指定历史那条；all=true 取全部（按时间倒序，看观点演变）。
    """
    from ..ingest.user_note import load_user_notes
    ticker = _resolve_ticker(args.get("ticker"), state)
    if not ticker:
        return "错误：找不到 ticker"
    notes = load_user_notes(ticker)
    if not notes:
        return f"{ticker} 没有笔记"
    state["last_ticker"] = ticker

    def _body(n):
        return n["content"].split("---", 2)[-1].strip()

    want_all = bool(args.get("all"))
    want_date = (args.get("date") or "").strip()

    if want_date:
        hit = next((n for n in notes if n["created_at"] == want_date), None)
        if not hit:
            dates = ", ".join(n["created_at"] for n in notes)
            return f"{ticker} 无 {want_date} 的笔记。已有日期: {dates}"
        return f"=== {ticker} 笔记 [{want_date}]（共 {len(notes)} 条中的一条）===\n{_body(hit)}"

    if want_all:
        out = [f"=== {ticker} 全部笔记（{len(notes)} 条，新→旧，可见观点演变）==="]
        for n in notes:
            out.append(f"\n──── [{n['created_at']}] ────\n{_body(n)}")
        return "\n".join(out)

    # 默认：最新一条 + 提示如何看历史
    latest = notes[0]
    head = f"=== {ticker} 笔记 [{latest['created_at']}]"
    if len(notes) > 1:
        others = ", ".join(n["created_at"] for n in notes[1:6])
        head += f"（共 {len(notes)} 条，展示最新；看历史用 date=日期 或 all=true。其余: {others}）"
    head += " ==="
    return f"{head}\n{_body(latest)}"


# ── Block 4: 修改 / 合并 / 软删除 ──

def _do_merge_cot(args: dict, state: dict) -> str:
    """同 sector 内 CoT LLM 聚类合并去重（包 merge_sector）。"""
    import io, contextlib
    from ..cot.merger import merge_sector, list_sectors_with_cots
    from ..sectors import resolve_alias
    sector_raw = (args.get("sector") or "").strip() or state.get("last_sector")
    dry_run = bool(args.get("dry_run"))
    if not sector_raw:
        avail = list_sectors_with_cots()
        listing = "\n".join(f"  {s} ({n} 条)" for s, n in avail)
        return f"请指定要合并的 sector。当前有 CoT 的板块：\n{listing}"
    sector = resolve_alias(sector_raw) or sector_raw
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = merge_sector(sector, dry_run=dry_run)
    out = buf.getvalue().strip()
    if rep.get("skipped"):
        return f"[merge {sector}] 跳过：{rep['skipped']}"
    if rep.get("error"):
        return f"[merge {sector}] 失败：{rep['error']}\n{out}"
    state["last_sector"] = sector
    tail = f"{out}\n" if out else ""
    if dry_run:
        return f"{tail}[预览] {sector}：建议合并方案如上，未写盘。说『确认合并 {sector}』真跑。"
    return f"{tail}✓ {sector} 合并完成，原文件已归档到 _archive/。"


def _do_regroup_cot(args: dict, state: dict) -> str:
    """单文件内 CoT 本地重组合并去重（不重抽 LLM）。"""
    import io, contextlib
    from ..cot.local_ops import find_cot_file, regroup_file
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：缺少 query"
    fp = find_cot_file(query)
    if not fp:
        return f"找不到匹配 '{query}' 的 CoT 文件"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = regroup_file(fp, dry_run=bool(args.get("dry_run")))
    out = buf.getvalue().strip()
    if isinstance(rep, dict) and rep.get("error"):
        return f"✗ {rep['error']}\n{out}"
    return out or f"✓ 已重组 {fp.name}"


def _do_rescore_cot(args: dict, state: dict) -> str:
    """单文件仅重新打分（不重抽 CoT）。"""
    import io, contextlib
    from ..cot.local_ops import find_cot_file, rescore_file
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：缺少 query"
    fp = find_cot_file(query)
    if not fp:
        return f"找不到匹配 '{query}' 的 CoT 文件"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = rescore_file(fp, dry_run=bool(args.get("dry_run")))
    out = buf.getvalue().strip()
    if isinstance(rep, dict) and rep.get("error"):
        return f"✗ {rep['error']}\n{out}"
    return out or f"✓ 已重新打分 {fp.name}"


def _do_delete_cot(args: dict, state: dict) -> str:
    """软删除 CoT 文件：移到 _archive/（可恢复，不物理删）。"""
    from ..cot.local_ops import soft_delete_file
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：缺少 query"
    res = soft_delete_file(query)
    if "error" in res:
        return f"✗ {res['error']}"
    from pathlib import Path as _P
    return (f"✓ 已软删除（归档，可恢复）: {res['source']}\n"
            f"  归档到: {_P(res['archived_to']).parent.name}/{_P(res['archived_to']).name}\n"
            f"  已从 list/搜索/投票中移除。要彻底恢复把文件从 _archive/ 移回即可。")


def _do_delete_note(args: dict, state: dict) -> str:
    """软删除用户笔记：移到 theses/user/_archive/（可恢复）。"""
    from ..ingest.user_note import soft_delete_note
    ticker = _resolve_ticker(args.get("ticker"), state)
    if not ticker:
        return "错误：找不到 ticker"
    res = soft_delete_note(ticker, note_date=(args.get("date") or None))
    if "error" in res:
        return f"✗ {res['error']}"
    return (f"✓ 已软删除（归档，可恢复）{res['ticker']} 的 {len(res['archived'])} 条笔记 → theses/user/_archive/")


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
    "reclassify_cot": _do_reclassify_cot,
    "search_memory": _do_search_memory,
    "get_cot": _do_get_cot,
    "get_note": _do_get_note,
    "merge_cot": _do_merge_cot,
    "regroup_cot": _do_regroup_cot,
    "rescore_cot": _do_rescore_cot,
    "delete_cot": _do_delete_cot,
    "delete_note": _do_delete_note,
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
