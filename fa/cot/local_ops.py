"""CoT 本地操作 — 不重新调 LLM 抽取的情况下做"重组/重打分/编辑定位".

场景:
  - regroup: 用户对某份报告的现有 CoT 不满意（重复/矛盾），想在本地重新合并去重
  - rescore: 用户改了 config 里的打分权重后，想重新算 signal 但保留 trigger/COT 内容
  - edit:    用户想手动改一条 CoT，需要快速定位文件路径

设计上不动 LLM 调用以外的内容；merger.cluster_and_merge 已经能跑单文件，复用即可。
"""

import re
import secrets
from datetime import date
from pathlib import Path
from typing import Optional

from .loader import COT_DIR, list_cot_files, _parse_frontmatter, _parse_cot_body, _parse_tags


def _gen_uid(existing: Optional[set] = None) -> str:
    """生成 6 位 hex 持久 chain uid，避开同文件内已有的。"""
    existing = existing or set()
    while True:
        uid = secrets.token_hex(3)
        if uid not in existing:
            return uid


def _chain_uids_in(text: str) -> set:
    """抽出一段文本里所有 `**id**:` uid，用于同文件查重。"""
    return set(re.findall(r"(?m)^\*\*id\*\*:\s*([0-9a-f]{4,8})\b", text))


def find_cot_file(query: str) -> Optional[Path]:
    """按 cot_id / source 文件名片段 / 标题或推理文本 / sector 名 模糊定位 CoT 文件。

    优先级: source 片段 / cot_id > 标题·推理文本 > sector 名。
    多个匹配时返回最新修改的；无匹配返回 None。

    注意 cot_id 形如 `<source_hash>_<n>`（n 是文件内第几条链），定位文件时尾部
    `_<n>` 要先剥掉再和 source_hash 比，否则 search_memory/list_cot 给出的 id
    传进来必然匹配不上（曾导致问答时反复重试、死循环）。
    """
    if not query:
        return None
    q = query.lower()
    # 剥掉 cot_id 尾部的链标识（持久 uid `_<6hex>` 或旧位置号 `_<数字>`），得到 source_hash 段
    q_hash = re.sub(r"_([0-9a-f]{4,8}|\d+)$", "", q)
    files = list_cot_files()
    if not files:
        return None

    matches = []
    for fp in files:
        # 1) 直接是路径
        if str(fp).lower().endswith(query.lower()) or query.lower() in fp.name.lower():
            matches.append((fp, 3))  # 高优先级
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        source = (fm.get("source") or "").lower()
        sector = (fm.get("sector") or "").lower()
        source_hash = (fm.get("source_hash") or "").lower()
        # 2) source 文件名片段
        if q in source:
            matches.append((fp, 3))
            continue
        # 3) source_hash（兼容传入完整 cot_id `<hash>_<n>`）
        if source_hash and (q_hash == source_hash or q_hash in source_hash):
            matches.append((fp, 3))
            continue
        # 4) 标题 / 推理文本片段（让"按 CoT 标题问"也能定位到文件）
        body = text.split("---", 2)[-1]
        if q in body.lower():
            matches.append((fp, 2))
            continue
        # 5) sector 名（最弱匹配，可能多个）
        if q in sector:
            matches.append((fp, 1))

    if not matches:
        return None
    matches.sort(key=lambda x: (-x[1], -x[0].stat().st_mtime))
    return matches[0][0]


def load_file_cots(fp: Path) -> tuple[dict, list[dict]]:
    """读单个 CoT 文件，返回 (frontmatter, cots)."""
    text = fp.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    body = text.split("---", 2)[-1]
    cots = _parse_cot_body(body)
    return fm, cots


def render_file_full(query: str) -> dict:
    """定位 CoT 文件并返回全文（frontmatter 摘要 + 所有链），供问答/查看。

    返回 {"file": str, "source": str, "sector": str, "tags": str,
          "source_hash": str, "text": str(可读全文)} 或 {"error": ...}。
    """
    fp = find_cot_file(query)
    if not fp:
        return {"error": f"找不到匹配 '{query}' 的 CoT 文件"}
    fm, cots = load_file_cots(fp)
    lines = [
        f"# {fm.get('source', fp.name)}",
        f"主题(主): {fm.get('tags') or '(未打主题)'}  |  一级行业(兜底): {fm.get('sector', '')}",
        f"ticker: {fm.get('ticker') or '(未绑定)'}  |  质量: {fm.get('quality_rating') or '?'}/5"
        f"  |  created_at: {fm.get('created_at', '')}  |  source_hash: {fm.get('source_hash', '')}",
        "",
    ]
    if fm.get("user_comment"):
        lines.append(f"用户角度: {fm['user_comment']}")
        lines.append("")
    for i, c in enumerate(cots, 1):
        sub = ""
        if "transmission" in c and "history" in c and "recency" in c:
            sub = f"  (传导{c['transmission']}·历史{c['history']}·时效{c['recency']})"
        lines.append(f"## CoT {i} — {c['trigger']}  [信号 {c['signal']}/10]{sub}")
        lines.append(f"{c['COT']}")
        lines.append("")
    return {
        "file": str(fp), "source": fm.get("source", fp.name),
        "sector": fm.get("sector", ""), "tags": fm.get("tags", ""),
        "source_hash": fm.get("source_hash", ""), "cot_count": len(cots),
        "text": "\n".join(lines),
    }


def _rewrite_with_chain_tags(text: str, chain_tags: list, union: list) -> str:
    """把链级 tag 写回单个 CoT 文件全文：

    1. frontmatter `tags:` 改为 union（各链 tag 的并集，作为文件级快路径过滤的超集）
    2. 每条 `## CoT N —` 标题后插入/替换 `**主题**: a、b` 行（空 tag 的链不插）

    保留其余所有内容（信号、推理链、原文依据、来源 id 等）不动。
    """
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text  # 非预期格式，不动
    pre, fm_str, body = parts[0], parts[1], parts[2]

    # 1) frontmatter tags 行
    fm_lines = fm_str.split("\n")
    out_fm, has_tags = [], False
    for ln in fm_lines:
        if re.match(r"^\s*tags\s*:", ln):
            has_tags = True
            if union:
                out_fm.append(f"tags: [{', '.join(union)}]")
            # union 空则丢弃该行（调用方已保证不会无谓清空）
        else:
            out_fm.append(ln)
    if not has_tags and union:
        # 没有 tags 行：插在 cot_count / source_hash 之后，否则末尾
        insert_at = len(out_fm)
        for i, ln in enumerate(out_fm):
            if re.match(r"^\s*(cot_count|source_hash)\s*:", ln):
                insert_at = i + 1
        out_fm.insert(insert_at, f"tags: [{', '.join(union)}]")
    new_fm = "\n".join(out_fm)

    # 2) 正文每条链插入 **主题** 行
    blocks = re.split(r"(?m)(?=^## CoT \d+ — )", body)
    new_blocks, ci = [], 0
    for blk in blocks:
        if not blk.lstrip().startswith("## CoT"):
            # preamble：同步文档级「**主题 tags**:」装饰行（loader 不读，但避免与 frontmatter 不一致）
            if re.search(r"(?m)^\*\*主题 tags\*\*:", blk):
                if union:
                    repl = "**主题 tags**: " + " · ".join(f"#{t}" for t in union)
                    blk = re.sub(r"(?m)^\*\*主题 tags\*\*:.*$", repl, blk)
                else:
                    blk = re.sub(r"(?m)^\*\*主题 tags\*\*:.*\n\n?", "", blk)
            new_blocks.append(blk)
            continue
        tags_for = chain_tags[ci] if ci < len(chain_tags) else []
        ci += 1
        # 先去掉已有的 **主题** 行（重跑可覆盖）
        blk = re.sub(r"(?m)^\*\*主题\*\*:[^\n]*\n", "", blk)
        if tags_for:
            m = re.match(r"(?s)(^## CoT \d+ — [^\n]*\n)", blk)
            if m:
                blk = m.group(1) + f"\n**主题**: {'、'.join(tags_for)}\n" + blk[m.end(1):]
        new_blocks.append(blk)
    new_body = "".join(new_blocks)

    return pre + "---" + new_fm + "---" + new_body


def retag_file_chains(fp: Path, dry_run: bool = False) -> dict:
    """对单个 CoT 文件做链级主题回填：调 classify_chains 给每条链打 tag 并写回。

    防丢保护：LLM 归类全空且文件原有 tag 非空 → 跳过不改（疑似 LLM 失败）。
    返回报告 dict（dry_run 时不写盘）。
    """
    from ..sectors import classify_chains
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception as e:
        return {"file": fp.name, "skipped": f"读取失败: {e}"}
    fm = _parse_frontmatter(text)
    cots = _parse_cot_body(text.split("---", 2)[-1])
    if not cots:
        return {"file": fp.name, "skipped": "无可解析 CoT"}

    ch = classify_chains(
        cots,
        doc_context=f"{fm.get('source', fp.name)} / {fm.get('sector', '')}",
        user_comment=fm.get("user_comment", ""),
    )
    chain_tags = ch["chain_tags"]
    union, _seen = [], set()
    for tg in chain_tags:
        for t in tg:
            if t not in _seen:
                _seen.add(t)
                union.append(t)
    old_tags = _parse_tags(fm.get("tags", ""))

    report = {"file": fp.name, "n_chains": len(cots), "chain_tags": chain_tags,
              "union": union, "old_tags": old_tags, "suggested": ch.get("suggested_tags", [])}

    if not union and old_tags:
        report["skipped"] = "链级归类全空（疑似 LLM 失败），保留原状"
        return report
    if dry_run:
        return report

    new_text = _rewrite_with_chain_tags(text, chain_tags, union)
    fp.write_text(new_text, encoding="utf-8")
    report["written"] = True
    return report


def retag_all_chains(dry_run: bool = False) -> dict:
    """全库链级 tag 回填。真跑前把所有目标文件备份到 _archive_retag_bak_YYYYMMDD/。

    备份目录以 _archive 开头 → loader 自动跳过，不污染召回。
    """
    import shutil
    files = list_cot_files()
    if not files:
        return {"files": [], "note": "CoT 库为空"}

    backup_root = None
    if not dry_run:
        today = date.today().strftime("%Y%m%d")
        backup_root = COT_DIR / f"_archive_retag_bak_{today}"
        for fp in files:
            rel = fp.relative_to(COT_DIR)
            dst = backup_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, dst)

    reports = []
    for fp in files:
        reports.append(retag_file_chains(fp, dry_run=dry_run))
    return {"files": reports, "backup": str(backup_root) if backup_root else None,
            "dry_run": dry_run}


def stamp_file_ids(fp: Path) -> dict:
    """给单个 CoT 文件里缺 `**id**` 的链补发持久 uid（已有的不动）。就地改文本，不重排。"""
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception as e:
        return {"file": fp.name, "skipped": f"读取失败: {e}"}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {"file": fp.name, "skipped": "无 frontmatter"}
    pre, fm_str, body = parts
    existing = _chain_uids_in(body)
    segs = re.split(r"(?m)(?=^## CoT \d+ — )", body)
    stamped = 0
    for i, blk in enumerate(segs):
        if not blk.lstrip().startswith("## CoT"):
            continue
        if re.search(r"(?m)^\*\*id\*\*:\s*[0-9a-f]{4,8}\b", blk):
            continue
        uid = _gen_uid(existing)
        existing.add(uid)
        segs[i] = re.sub(r"(^## CoT \d+ — [^\n]*\n\n?)",
                         lambda m: m.group(1) + f"**id**: {uid}\n", blk, count=1)
        stamped += 1
    if stamped:
        fp.write_text(pre + "---" + fm_str + "---" + "".join(segs), encoding="utf-8")
    return {"file": fp.name, "stamped": stamped}


def stamp_ids_all_files(dry_run: bool = False) -> dict:
    """全库回填持久 chain uid。真跑前全量备份到 _archive_stampid_bak_YYYYMMDD/。"""
    import shutil
    files = list_cot_files()
    if not files:
        return {"files": [], "note": "CoT 库为空"}
    if dry_run:
        reports = []
        for fp in files:
            try:
                body = fp.read_text(encoding="utf-8").split("---", 2)[-1]
            except Exception:
                continue
            n_chains = len(re.findall(r"(?m)^## CoT \d+ — ", body))
            n_have = len(_chain_uids_in(body))
            reports.append({"file": fp.name, "missing": n_chains - n_have, "total": n_chains})
        return {"files": reports, "dry_run": True,
                "total_missing": sum(r["missing"] for r in reports)}

    today = date.today().strftime("%Y%m%d")
    backup_root = COT_DIR / f"_archive_stampid_bak_{today}"
    for fp in files:
        dst = backup_root / fp.relative_to(COT_DIR)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fp, dst)
    reports = [stamp_file_ids(fp) for fp in files]
    return {"files": reports, "backup": str(backup_root),
            "total_stamped": sum(r.get("stamped", 0) for r in reports)}


def _chain_brief(block: str) -> dict:
    """从单个 CoT 块文本抽 trigger/signal/主题/COT 片段，用于改前回显。"""
    cots = _parse_cot_body(block)
    if not cots:
        return {}
    c = cots[0]
    return {"trigger": c.get("trigger", ""), "signal": c.get("signal", ""),
            "tags": c.get("_chain_tags", []), "cot": (c.get("COT", "") or "")[:120]}


def edit_chain(cot_id: str, set_tags=None, set_signal=None,
               set_trigger: Optional[str] = None, set_cot: Optional[str] = None,
               delete: bool = False, confirm: bool = False) -> dict:
    """链级纠错：按 cot_id 改/删单条 CoT 链。改前回显，delete 把被删块归档到 _archive/（可恢复）。

    - cot_id 形如 <source_hash>_<链标识>：链标识优先按持久 uid 解析（删兄弟链不偏移），
      回退旧位置号 _N。两者都从 list_cot/search_memory 最新输出取。
    - set_tags：主题（list[str]），过闭合词表 _valid_theme_tag，越界报错不写。
    - set_signal：1-10。set_trigger / set_cot：改标题 / 推理链文字。
    - delete：默认两段式——不带 confirm 只回预览不删；confirm=True 才真删（删块归档可恢复，
      其后链号前移、cot_count-1，删到 0 条则拒绝改用 delete_cot）。
    - 任一结构性变更后重算 frontmatter tags 并集 + 主题展示行 + cot_count。
    """
    from ..sectors import _valid_theme_tag

    fp = find_cot_file(cot_id)
    if not fp:
        return {"error": f"找不到匹配 '{cot_id}' 的 CoT 文件"}
    text = fp.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {"error": "文件格式异常（无 frontmatter）"}
    pre, fm_str, body = parts
    segs = re.split(r"(?m)(?=^## CoT \d+ — )", body)
    # segs[0]=preamble；其余每段一条链
    cot_idxs = [i for i, s in enumerate(segs) if s.lstrip().startswith("## CoT")]
    if not cot_idxs:
        return {"error": "该文件没有可解析的 CoT 链"}

    # 目标解析：优先按持久 uid（稳定，删兄弟链不偏移），回退旧位置号 _N
    tail = cot_id.rsplit("_", 1)[-1].lower()
    uid_to_seg = {}
    for si in cot_idxs:
        mu = re.search(r"(?m)^\*\*id\*\*:\s*([0-9a-f]{4,8})\b", segs[si])
        if mu:
            uid_to_seg[mu.group(1)] = si
    if tail in uid_to_seg:
        seg_i = uid_to_seg[tail]
        n = cot_idxs.index(seg_i) + 1
    elif tail.isdigit():
        n = int(tail)
        if not (1 <= n <= len(cot_idxs)):
            return {"error": f"该文件只有 {len(cot_idxs)} 条链，没有第 {n} 条"}
        seg_i = cot_idxs[n - 1]
    else:
        return {"error": f"cot_id '{cot_id}' 的链标识 '{tail}' 在该文件里找不到"
                         f"（uid 不匹配且非位置号）。请用 list_cot/search_memory 最新输出的 id"}
    block = segs[seg_i]
    before = _chain_brief(block)

    # 校验 tags（闭合词表守门）
    new_tags = None
    if set_tags is not None:
        new_tags, bad = [], []
        for t in set_tags:
            canon = _valid_theme_tag(str(t))
            if canon:
                if canon not in new_tags:
                    new_tags.append(canon)
            else:
                bad.append(str(t))
        if bad:
            return {"error": f"主题 {bad} 不在闭合词表里，未改。需先把它加入 memory/sectors.yaml"}

    action_parts = []

    if delete:
        if len(cot_idxs) <= 1:
            return {"error": "这是该文件唯一一条链，删它请用 delete_cot 软删整份（可恢复）"}
        if not confirm:
            # 两段式：先回显将删哪条，未落盘；确认后带 confirm=true 重发才真删
            return {"preview": True, "cot_id": cot_id, "n": n, "before": before,
                    "remaining_if_deleted": len(cot_idxs) - 1,
                    "note": "删除预览（未执行）。确认无误后带 confirm=true 重发同一 cot_id 才真删。"}
        removed_block = segs[seg_i].rstrip()
        # 链级删除也要可恢复：把被删块归档到 _archive/deleted-chains-YYYYMMDD.md（loader 跳过 _archive*）
        today = date.today().strftime("%Y%m%d")
        archive_dir = fp.parent / "_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        graveyard = archive_dir / f"deleted-chains-{today}.md"
        del_uid = (re.search(r"(?m)^\*\*id\*\*:\s*([0-9a-f]{4,8})\b", removed_block) or [None, "?"])[1]
        from datetime import datetime as _dt
        header = (f"\n\n<!-- 删除于 {_dt.now():%Y-%m-%d %H:%M:%S} · 源文件 {fp.name} · "
                  f"id {del_uid} · {before.get('trigger', '')[:50]} -->\n")
        with graveyard.open("a", encoding="utf-8") as f:
            f.write(header + removed_block + "\n")
        del segs[seg_i]
        action_parts.append("删除整条")
    else:
        if new_tags is not None:
            block = re.sub(r"(?m)^\*\*主题\*\*:[^\n]*\n", "", block)
            if new_tags:
                mh = re.match(r"(?s)(^## CoT \d+ — [^\n]*\n)", block)
                if mh:
                    block = mh.group(1) + f"\n**主题**: {'、'.join(new_tags)}\n" + block[mh.end(1):]
            action_parts.append(f"主题→{new_tags or '(清空)'}")
        if set_signal is not None:
            sig = max(1, min(10, int(set_signal)))
            block = re.sub(r"(\*\*信号强度\*\*:\s*)\d+(\s*/\s*10)",
                           lambda mm: f"{mm.group(1)}{sig}{mm.group(2)}", block, count=1)
            action_parts.append(f"信号→{sig}")
        if set_trigger is not None:
            ls = block.split("\n")
            mh = re.match(r"(## CoT \d+ — )(.*)", ls[0])
            if mh:
                suf = re.search(r"(\s*\(合并自 \d+ 条\))\s*$", mh.group(2))
                ls[0] = f"{mh.group(1)}{set_trigger}{suf.group(1) if suf else ''}"
                block = "\n".join(ls)
            action_parts.append("改 trigger")
        if set_cot is not None:
            block = re.sub(
                r"(\*\*推理链\*\*:\s*).+?(?=\n\*\*原文依据\*\*|\n_来源 CoT id|\n## |\Z)",
                lambda mm: mm.group(1) + set_cot, block, count=1, flags=re.DOTALL)
            action_parts.append("改推理链")
        if not action_parts:
            return {"error": "没指定要改什么（set_tags/set_signal/set_trigger/set_cot/delete 至少给一个）"}
        segs[seg_i] = block

    # 删除后重排链号 ## CoT K —
    if delete:
        k = 0
        for i, s in enumerate(segs):
            if s.lstrip().startswith("## CoT"):
                k += 1
                segs[i] = re.sub(r"^## CoT \d+ — ", f"## CoT {k} — ", s, count=1)

    new_body = "".join(segs)
    new_text = pre + "---" + fm_str + "---" + new_body

    # 重算 frontmatter tags 并集 + cot_count + 主题展示行
    remaining = _parse_cot_body(new_body)
    union, _seen = [], set()
    for c in remaining:
        for t in (c.get("_chain_tags") or []):
            if t and t not in _seen:
                _seen.add(t)
                union.append(t)
    new_text = _rewrite_with_chain_tags(new_text, [c.get("_chain_tags") or [] for c in remaining], union)
    new_text = re.sub(r"(?m)^cot_count:\s*\d+", f"cot_count: {len(remaining)}", new_text, count=1)

    fp.write_text(new_text, encoding="utf-8")

    report = {"file": fp.name, "cot_id": cot_id, "n": n,
              "action": " / ".join(action_parts), "before": before}
    if delete:
        report["removed_block"] = removed_block
        report["remaining"] = len(remaining)
        report["archived_to"] = f"{archive_dir.name}/{graveyard.name}"
    else:
        # after 快照
        new_segs = re.split(r"(?m)(?=^## CoT \d+ — )", new_body)
        new_cot_idxs = [i for i, s in enumerate(new_segs) if s.lstrip().startswith("## CoT")]
        report["after"] = _chain_brief(new_segs[new_cot_idxs[n - 1]]) if n - 1 < len(new_cot_idxs) else {}
    report["union"] = union
    return report


def soft_delete_file(query: str) -> dict:
    """软删除 CoT 文件：移到所在 sector 的 _archive/，加 deleted-YYYYMMDD- 前缀。

    不物理删除（可恢复）。归档后从 list/search/vote 中消失（loader 跳过 _archive*）。
    返回 {"file": 原路径, "archived_to": 归档路径, "source": ...} 或 {"error": ...}。
    """
    import shutil
    fp = find_cot_file(query)
    if not fp:
        return {"error": f"找不到匹配 '{query}' 的 CoT 文件"}
    fm, _ = load_file_cots(fp)
    archive_dir = fp.parent / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    target = archive_dir / f"deleted-{today}-{fp.name}"
    i = 1
    while target.exists():
        target = archive_dir / f"deleted-{today}-{i}-{fp.name}"
        i += 1
    shutil.move(str(fp), str(target))
    return {"file": str(fp), "archived_to": str(target), "source": fm.get("source", fp.name)}


def write_cots_to_file(fp: Path, fm: dict, cots: list[dict], extra_header: str = "") -> None:
    """重写 CoT 文件。保留 frontmatter 字段，仅替换 body。"""
    lines = ["---"]
    # 标准字段顺序
    field_order = ["ticker", "sector", "source", "source_hash", "created_at",
                   "cot_count", "quality_rating", "tags", "user_comment"]
    seen = set()
    for k in field_order:
        if k in fm:
            v = fm[k]
            if k == "cot_count":
                v = str(len(cots))
            lines.append(f"{k}: {v}")
            seen.add(k)
    # 其他未在 order 中的字段也保留
    for k, v in fm.items():
        if k not in seen:
            lines.append(f"{k}: {v}")
    lines.extend(["---", ""])

    source = fm.get("source", fp.name)
    lines.append(f"# CoT 提取自 {source}")
    lines.append("")
    if extra_header:
        lines.append(extra_header)
        lines.append("")

    qr = fm.get("quality_rating")
    if qr and str(qr).isdigit() and int(qr) > 0:
        stars = "⭐" * int(qr)
        lines.append(f"**研报质量**: {stars} ({qr}/5)")
        lines.append("")

    tags_str = fm.get("tags", "")
    if tags_str:
        tags = _parse_tags(tags_str)
        if tags:
            lines.append("**主题 tags**: " + " · ".join(f"#{t}" for t in tags))
            lines.append("")

    if fm.get("user_comment"):
        lines.extend(["## 🗨 用户角度提示", "", str(fm["user_comment"]).strip(), ""])

    existing_uids = {c["_uid"] for c in cots if c.get("_uid")}
    for i, c in enumerate(cots, 1):
        uid = c.get("_uid")
        if not uid:
            uid = _gen_uid(existing_uids)
            existing_uids.add(uid)
        lines.append(f"## CoT {i} — {c['trigger']}")
        lines.append("")
        lines.append(f"**id**: {uid}")
        chain_tags = [t for t in (c.get("_chain_tags") or []) if t]
        if chain_tags:
            lines.append(f"**主题**: {'、'.join(chain_tags)}")
        lines.append("")
        sub_line = ""
        if "transmission" in c and "history" in c and "recency" in c:
            fals = f" · 证伪 {c['falsifiability']}" if c.get("falsifiability") is not None else ""
            sub_line = (f"  _(传导 {c['transmission']}{fals} · 历史 {c['history']} · "
                        f"时效 {c['recency']})_")
        lines.extend([
            f"**信号强度**: {c['signal']}/10{sub_line}",
            "",
            f"**推理链**: {c['COT']}",
            "",
        ])
        ev = str(c.get("evidence", "")).strip()
        if ev:
            lines.extend([f"**原文依据**: 「{ev}」", ""])
        src_ids = c.get("_source_ids") or []
        if len(src_ids) > 1:
            lines.extend([f"_来源 CoT id: {', '.join(src_ids)}_", ""])

    fp.write_text("\n".join(lines), encoding="utf-8")


def reclassify_file(query: str, new_sector: Optional[str] = None,
                    new_tags: Optional[list[str]] = None) -> dict:
    """改 CoT 文件归类：sector + tags，必要时搬目录。

    new_sector / new_tags 至少给一个；空值不改。
    返回 {"file": str, "moved": bool, "old_sector": str, "new_sector": str,
          "old_tags": list, "new_tags": list}
    """
    fp = find_cot_file(query)
    if not fp:
        return {"error": f"找不到匹配的 CoT 文件: {query}"}
    if not new_sector and not new_tags:
        return {"error": "至少要给 new_sector 或 new_tags 之一"}

    fm, cots = load_file_cots(fp)
    old_sector = fm.get("sector", "")
    old_tags_str = fm.get("tags", "")
    old_tags = _parse_tags(old_tags_str)

    if new_sector:
        fm["sector"] = new_sector
    if new_tags is not None:
        fm["tags"] = "[" + ", ".join(new_tags) + "]" if new_tags else ""

    write_cots_to_file(fp, fm, cots)

    moved = False
    final_path = fp
    if new_sector and new_sector != old_sector:
        target_dir = COT_DIR / new_sector
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / fp.name
        if target.exists() and target.resolve() != fp.resolve():
            return {
                "error": f"目标已存在: {target}（未移动，请先处理冲突）",
                "file": str(fp),
            }
        fp.rename(target)
        final_path = target
        moved = True

    return {
        "file": str(final_path),
        "moved": moved,
        "old_sector": old_sector,
        "new_sector": new_sector or old_sector,
        "old_tags": old_tags,
        "new_tags": new_tags if new_tags is not None else old_tags,
    }


def regroup_file(fp: Path, dry_run: bool = False) -> dict:
    """对单文件做本地重组（合并同义 + 拆分混合）。

    复用 merger.cluster_and_merge：把单文件的 CoT 当输入跑一遍，重写回原文件。
    原文件备份到 ./_archive/regrouped-YYYYMMDD-<原名>。
    """
    from .merger import cluster_and_merge

    fm, cots = load_file_cots(fp)
    if len(cots) < 2:
        return {"skipped": f"CoT 数量不足 ({len(cots)} < 2)，无需重组"}

    # 给 cots 加 _cot_id 让 merger 用
    source_hash = fm.get("source_hash") or fp.stem
    for i, c in enumerate(cots, 1):
        c["_cot_id"] = f"{source_hash}_{i}"
        c["_source"] = fm.get("source", fp.name)

    sector = fm.get("sector") or "uncategorized"
    print(f"[REGROUP] {fp.name}: {len(cots)} 条 → LLM 重组中...")
    result = cluster_and_merge(cots, sector)
    if not result:
        return {"error": "LLM 调用或解析失败"}

    merged = result["merged_cots"]
    report = {
        "input_count": len(cots),
        "output_count": len(merged),
        "merged_groups": sum(1 for mc in merged if len(mc.get("_source_ids", [])) > 1),
    }
    report["reduction_pct"] = round((1 - len(merged) / len(cots)) * 100, 1)

    if dry_run:
        report["dry_run"] = True
        report["preview"] = [
            {"trigger": mc["trigger"], "signal": mc.get("signal", "?"),
             "merged_from": len(mc.get("_source_ids", []))}
            for mc in merged
        ]
        return report

    # 备份原文件
    archive_dir = fp.parent / "_archive"
    archive_dir.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    backup = archive_dir / f"regrouped-{today}-{fp.name}"
    i = 1
    while backup.exists():
        backup = archive_dir / f"regrouped-{today}-{i}-{fp.name}"
        i += 1
    fp.replace(backup)
    report["backup"] = str(backup.relative_to(COT_DIR.parent.parent))

    # 写新文件（保留子分如有）
    clean_cots = []
    for mc in merged:
        item = {"trigger": mc["trigger"], "COT": mc["COT"], "signal": str(mc.get("signal", "5"))}
        for k in ("transmission", "falsifiability", "history", "recency"):
            if k in mc:
                item[k] = mc[k]
        clean_cots.append(item)
    extra = f"_本文件经 fa cot regroup 重组 ({today})，原文件备份到 _archive/_"
    # 同名重写
    new_fp = fp.parent / fp.name
    write_cots_to_file(new_fp, fm, clean_cots, extra_header=extra)
    report["new_file"] = str(new_fp.relative_to(COT_DIR.parent.parent))
    return report


def rescore_file(fp: Path, dry_run: bool = False) -> dict:
    """对单文件的 CoT 重新打分（独立 Critic，不改 trigger/COT 内容）。

    v3：四维打分 + 抗通胀锚定 + 专治"一家之言"。
    流程：
      1. 读出 cots
      2. 独立 Critic LLM 评四档子分 (transmission/falsifiability/history/recency)
      3. 用 config.toml 权重计算 signal
      4. 写回原文件（备份原版到 _archive/）
    """
    import json
    from ..config import load_config, make_anthropic_client
    from ..ingest.cot_extractor import _get_score_weights, _parse_json_flexible, _coerce_signal, _SCORE_DIMS

    fm, cots = load_file_cots(fp)
    if not cots:
        return {"skipped": "无 CoT 可打分"}

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")
    client = make_anthropic_client()
    weights = _get_score_weights()

    sys_prompt = """你是严格的投资思维链评审员（Critic）。对每条 CoT 独立打四档子分（不修改 trigger/COT 内容）。

## 四个维度（各 1-10 整数）
- transmission 传导明确性：A→B→C→股价 链条是否清晰、每环是否有公开数据可追踪
- falsifiability 可证伪性/具体性：**这条是"可观测可证伪的传导逻辑"还是"一家之言的价值判断"**
    9-10 = 有明确可观测触发条件(具体数字/事件)+明确反证条件；
    1-5  = 纯价值判断/静态论断/不可证伪（如"管理优秀""护城河强""话语权提升""竞争力被认可"），必须 ≤4
- history 历史可验证性：同类逻辑历史上是否被验证过（≥3次=9-10，1-2次=6-8，全新=1-5）
- recency 时效性：触发是否在持续、多久兑现（6个月内有验证点=9-10，长期>2年=1-5）

## 抗通胀铁律（非常重要）
你过去倾向于给所有 CoT 打 7-9 分，这是错的。一批 CoT 里**通常只有约 15% 该到 8+，约 50% 在 6-7，约 35% 在 5 以下**。
- 不要给每条都打高分。看到"一家之言/陈述句/无法证伪的观点"，falsifiability 直接压到 1-4。
- 对每条都要在 why_not_higher 字段写一句"为什么不给更高分"（强制自我质疑）。

## 严格 JSON 输出（不要 markdown）
{"scores": [{"id": 1, "transmission": x, "falsifiability": x, "history": x, "recency": x, "why_not_higher": "一句话"}, ...]}"""

    cot_list_str = "\n\n".join(
        f"[{i}] trigger: {c['trigger']}\nCOT: {c['COT']}"
        for i, c in enumerate(cots, 1)
    )
    user_msg = f"## 输入 CoT 列表（共 {len(cots)} 条，记住抗通胀铁律）\n\n{cot_list_str}\n\n请输出 JSON："

    print(f"[RESCORE] {fp.name}: {len(cots)} 条 → LLM 重新打分...")
    try:
        resp = client.messages.create(
            model=model, max_tokens=4000,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        return {"error": f"LLM 调用失败: {e}"}

    parsed = _parse_json_flexible(text)
    if not parsed or "scores" not in parsed:
        return {"error": f"JSON 解析失败，原始: {text[:200]}"}

    score_map = {s.get("id"): s for s in parsed["scores"] if isinstance(s, dict)}
    report = {"count": len(cots), "updated": 0, "diffs": []}

    for i, c in enumerate(cots, 1):
        s = score_map.get(i)
        if not s:
            continue
        old_signal = c.get("signal", "5")
        for k in _SCORE_DIMS:
            try:
                c[k] = max(1, min(10, int(s.get(k, 5))))
            except (TypeError, ValueError):
                c[k] = 5
        new_signal = str(_coerce_signal(c, weights))
        c["signal"] = new_signal
        if new_signal != str(old_signal):
            report["updated"] += 1
            report["diffs"].append({
                "trigger": c["trigger"][:50],
                "old_signal": old_signal,
                "new_signal": new_signal,
                "falsifiability": c.get("falsifiability"),
                "why_not_higher": str(s.get("why_not_higher", ""))[:80],
            })

    if dry_run:
        report["dry_run"] = True
        return report

    # 备份 + 重写
    archive_dir = fp.parent / "_archive"
    archive_dir.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    backup = archive_dir / f"rescored-{today}-{fp.name}"
    i = 1
    while backup.exists():
        backup = archive_dir / f"rescored-{today}-{i}-{fp.name}"
        i += 1
    import shutil
    shutil.copy2(str(fp), str(backup))
    report["backup"] = str(backup.relative_to(COT_DIR.parent.parent))

    write_cots_to_file(fp, fm, cots,
                       extra_header=f"_本文件经 fa cot rescore 重打分 ({today})，原文件备份到 _archive/_")
    return report
