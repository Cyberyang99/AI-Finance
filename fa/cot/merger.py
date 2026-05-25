"""CoT 跨文档合并迭代 — 解决"摄入越多越乱"问题.

思路（参考 PDF1 §半年频合成 + Hermes/Mem-Palace 笔记压缩）:
  1. 同 sector 内 LLM 全量判重 + 合并（不跨 sector，因为投资逻辑差异大）
  2. trigger 相近的多条 → LLM 综合写一条更完整的
  3. 旧 CoT 归档到 cot/<sector>/_archive/，前缀 archived-YYYYMMDD-
  4. 新合并 CoT 写到 cot/<sector>/merged-YYYY-MM-DD.md
  5. 信号强度取簇内最高值

何时跑:
  - 摄入 5+ 份同 sector 资料后建议跑一次
  - 当一个 sector CoT 总数 > 30 条时强烈建议跑
  - 用户手动 fa cot merge
"""

import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Optional

from ..config import load_config, make_anthropic_client
from .loader import COT_DIR, list_cot_files, load_cots


MERGER_SYSTEM_PROMPT = """你是投资思维链整理员。你的工作是把同一行业内 trigger 相近、本质同一逻辑的 CoT 合并成更完整的版本，同时保留独立逻辑。

## 核心原则

1. **同义合并**: trigger 在描述同一个驱动因素或同一条传导路径，即使措辞不同，也应合并
2. **保留独立**: trigger 完全不同的（如"算法创新降本" vs "政策红利驱动渗透率"）必须保留各自
3. **不要过度合并**: 宁可保留 2 条相近条目，也不要硬合并成混乱的一条
4. **行业层面**: 合并后的 trigger 必须仍然是行业可复用的（不带某公司名）
5. **信号取最高**: 同簇 CoT 的 signal 取最高值（高信号在前的优先保留）
6. **传导链综合**: 合并后的 COT 应吸收所有来源中的关键细节，但不要冗长（建议 100-200 字）

## 输出格式

严格 JSON（不要 markdown 代码块包裹）：

```
{
  "merged_cots": [
    {
      "trigger": "<合并后或保留的 trigger>",
      "COT": "<合并后或保留的传导链>",
      "signal": "<1-10>",
      "_source_ids": ["<原 CoT id>", ...]
    },
    ...
  ],
  "summary": {
    "input_count": <整数>,
    "output_count": <整数>,
    "merged_groups": <合并发生的组数>
  }
}
```

要求：
- _source_ids 必须列出所有合并到这条的原 CoT 编号
- singleton (没合并的) 也要在 merged_cots 里，_source_ids 只有 1 个
- 不要漏掉任何输入 CoT — 所有 _source_ids 加起来必须 = 输入总数
- 除 JSON 外不要任何其他内容
"""


MERGER_USER_TEMPLATE = """以下是 {sector} 板块的 {n} 条 CoT，请按上述规则合并去重，输出 JSON。

## 输入 CoT 列表

{cot_list}

请输出合并后的 JSON：
"""


def _parse_json_obj(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
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


def _format_cots_for_prompt(cots: list[dict]) -> str:
    """把 CoT 列表格式化成 LLM 易读的编号列表。"""
    lines = []
    for c in cots:
        lines.append(
            f"### [{c['_cot_id']}] (signal={c['signal']}, src={c.get('_source', '?')})\n"
            f"- **trigger**: {c['trigger']}\n"
            f"- **COT**: {c['COT']}\n"
        )
    return "\n".join(lines)


def cluster_and_merge(cots: list[dict], sector: str) -> Optional[dict]:
    """一次 LLM 调用完成分组 + 合并。

    返回 {"merged_cots": [...], "summary": {...}} 或 None（失败）。
    """
    if not cots:
        return None
    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")
    client = make_anthropic_client()

    user_msg = MERGER_USER_TEMPLATE.format(
        sector=sector,
        n=len(cots),
        cot_list=_format_cots_for_prompt(cots),
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=12000,
            system=MERGER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [MERGE] LLM 调用失败: {e}")
        return None

    parsed = _parse_json_obj(text)
    if not parsed or "merged_cots" not in parsed:
        print(f"  [MERGE] JSON 解析失败，原始输出前 200 字: {text[:200]}")
        return None

    # 校验：所有 _source_ids 加起来必须等于输入数量
    all_sources = []
    for mc in parsed["merged_cots"]:
        all_sources.extend(mc.get("_source_ids", []))
    input_ids = {c["_cot_id"] for c in cots}
    output_ids = set(all_sources)
    missing = input_ids - output_ids
    if missing:
        print(f"  [MERGE] ⚠ LLM 漏掉了 {len(missing)} 条 CoT，作为 singleton 补回")
        # 把漏掉的当 singleton 加回
        id2cot = {c["_cot_id"]: c for c in cots}
        for mid in missing:
            c = id2cot[mid]
            parsed["merged_cots"].append({
                "trigger": c["trigger"],
                "COT": c["COT"],
                "signal": c["signal"],
                "_source_ids": [mid],
            })

    return parsed


def _archive_sector_files(sector: str) -> list[Path]:
    """把 sector 目录下所有非归档的 md 移到 _archive/，加日期前缀。

    返回归档后的路径列表。
    """
    safe_sect = re.sub(r"[\\/:*?\"<>|]", "_", sector)
    sector_dir = COT_DIR / safe_sect
    if not sector_dir.exists():
        return []

    archive_dir = sector_dir / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().strftime("%Y%m%d")
    archived = []
    for md in sector_dir.glob("*.md"):
        new_name = f"archived-{today}-{md.name}"
        target = archive_dir / new_name
        # 同名直接加序号
        i = 1
        while target.exists():
            target = archive_dir / f"archived-{today}-{i}-{md.name}"
            i += 1
        shutil.move(str(md), str(target))
        archived.append(target)
    return archived


def _write_merged_file(sector: str, merged_cots: list[dict]) -> Path:
    """把合并后的 CoT 写到 cot/<sector>/merged-YYYY-MM-DD.md。"""
    safe_sect = re.sub(r"[\\/:*?\"<>|]", "_", sector)
    sector_dir = COT_DIR / safe_sect
    sector_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    fname = f"merged-{today}.md"
    path = sector_dir / fname
    # 同日多次合并加序号
    i = 1
    while path.exists():
        path = sector_dir / f"merged-{today}-{i}.md"
        i += 1

    lines = [
        "---",
        f"ticker: ",
        f"sector: {sector}",
        f"source: merged ({today})",
        f"source_hash: merged_{today}",
        f"created_at: {today}",
        f"cot_count: {len(merged_cots)}",
        f"is_merged: true",
        "---",
        "",
        f"# CoT 合并集成 ({sector}) — {today}",
        "",
        f"_本文件由 fa cot merge 自动产出，整合自该板块下所有历史 CoT。原始文件归档到 ./_archive/_",
        "",
    ]
    for i, c in enumerate(merged_cots, 1):
        src_count = len(c.get("_source_ids", []))
        merge_tag = f" (合并自 {src_count} 条)" if src_count > 1 else ""
        lines.extend([
            f"## CoT {i} — {c['trigger']}{merge_tag}",
            "",
            f"**信号强度**: {c['signal']}/10",
            "",
            f"**推理链**: {c['COT']}",
            "",
        ])
        if src_count > 1:
            lines.append(f"_来源 CoT id: {', '.join(c['_source_ids'])}_")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def merge_sector(sector: str, dry_run: bool = False) -> dict:
    """对某 sector 做 CoT 合并。返回执行报告。"""
    cots = load_cots(sector=sector)
    if len(cots) < 2:
        return {"sector": sector, "skipped": f"CoT 数量不足 ({len(cots)} < 2)，无需合并"}

    print(f"[MERGE] {sector} 板块: {len(cots)} 条 CoT → LLM 聚类合并...")

    result = cluster_and_merge(cots, sector)
    if not result:
        return {"sector": sector, "error": "LLM 调用或解析失败"}

    merged_cots = result["merged_cots"]
    merged_groups = sum(1 for mc in merged_cots if len(mc.get("_source_ids", [])) > 1)

    report = {
        "sector": sector,
        "input_count": len(cots),
        "output_count": len(merged_cots),
        "merged_groups": merged_groups,
        "reduction_pct": round((1 - len(merged_cots) / len(cots)) * 100, 1),
    }

    if dry_run:
        report["dry_run"] = True
        report["preview"] = [
            {
                "trigger": mc["trigger"],
                "signal": mc["signal"],
                "merged_from": len(mc.get("_source_ids", [])),
            }
            for mc in merged_cots
        ]
        return report

    # 归档旧文件
    archived = _archive_sector_files(sector)
    report["archived_files"] = [a.name for a in archived]

    # 写新合并文件
    new_path = _write_merged_file(sector, merged_cots)
    report["new_file"] = new_path.name

    return report


def list_sectors_with_cots() -> list[tuple[str, int]]:
    """列出所有有 CoT 的 sector 及其条数。"""
    if not COT_DIR.exists():
        return []
    out = []
    for sub in COT_DIR.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        cots = load_cots(sector=sub.name)
        if cots:
            out.append((sub.name, len(cots)))
    return sorted(out, key=lambda x: -x[1])
