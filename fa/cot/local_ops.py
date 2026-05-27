"""CoT 本地操作 — 不重新调 LLM 抽取的情况下做"重组/重打分/编辑定位".

场景:
  - regroup: 用户对某份报告的现有 CoT 不满意（重复/矛盾），想在本地重新合并去重
  - rescore: 用户改了 config 里的打分权重后，想重新算 signal 但保留 trigger/COT 内容
  - edit:    用户想手动改一条 CoT，需要快速定位文件路径

设计上不动 LLM 调用以外的内容；merger.cluster_and_merge 已经能跑单文件，复用即可。
"""

import re
from datetime import date
from pathlib import Path
from typing import Optional

from .loader import COT_DIR, list_cot_files, _parse_frontmatter, _parse_cot_body, _parse_tags


def find_cot_file(query: str) -> Optional[Path]:
    """按 cot_id 前缀 / source 文件名片段 / sector 名 模糊定位 CoT 文件。

    优先级: source 片段 > sector 名 > cot_id 前缀。
    多个匹配时返回最新修改的；无匹配返回 None。
    """
    if not query:
        return None
    q = query.lower()
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
            matches.append((fp, 2))
            continue
        # 3) source_hash 前缀（即 cot_id 前缀）
        if q in source_hash:
            matches.append((fp, 2))
            continue
        # 4) sector 名（最弱匹配，可能多个）
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

    for i, c in enumerate(cots, 1):
        sub_line = ""
        if "transmission" in c and "history" in c and "recency" in c:
            sub_line = (f"  _(传导 {c['transmission']} · 历史 {c['history']} · "
                        f"时效 {c['recency']})_")
        lines.extend([
            f"## CoT {i} — {c['trigger']}",
            "",
            f"**信号强度**: {c['signal']}/10{sub_line}",
            "",
            f"**推理链**: {c['COT']}",
            "",
        ])

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
        for k in ("transmission", "history", "recency"):
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
    """对单文件的 CoT 重新打分（不改 trigger/COT 内容）。

    流程：
      1. 读出 cots
      2. 对每条调 LLM，只让 LLM 输出 transmission/history/recency 三档分
      3. 用 config.toml 权重计算 signal
      4. 写回原文件（备份原版到 _archive/）
    """
    import json
    from ..config import load_config, make_anthropic_client
    from ..ingest.cot_extractor import _get_score_weights, _parse_json_flexible, _coerce_signal

    fm, cots = load_file_cots(fp)
    if not cots:
        return {"skipped": "无 CoT 可打分"}

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")
    client = make_anthropic_client()
    weights = _get_score_weights()

    sys_prompt = """你是投资思维链打分员。对给定的每条 CoT 只评三档子分（不修改 trigger/COT 内容）。

每个 1-10 整数:
- transmission: 传导链清晰度
- history: 历史可验证性
- recency: 时效性

严格 JSON 输出: {"scores": [{"id": 1, "transmission": x, "history": x, "recency": x}, ...]} 不要 markdown。"""

    cot_list_str = "\n\n".join(
        f"[{i}] trigger: {c['trigger']}\nCOT: {c['COT']}"
        for i, c in enumerate(cots, 1)
    )
    user_msg = f"## 输入 CoT 列表\n\n{cot_list_str}\n\n请输出 JSON："

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
        for k in ("transmission", "history", "recency"):
            try:
                c[k] = max(1, min(10, int(s.get(k, 5))))
            except (TypeError, ValueError):
                c[k] = 5
        new_signal = str(_coerce_signal(c, weights))
        c["signal"] = new_signal
        if new_signal != str(old_signal):
            report["updated"] += 1
            report["diffs"].append({
                "trigger": c["trigger"][:60],
                "old_signal": old_signal,
                "new_signal": new_signal,
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
