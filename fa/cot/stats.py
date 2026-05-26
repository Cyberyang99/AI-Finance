"""CoT 全库统计 — fa cot dash 的数据后端.

聚合维度:
  - 总条数 / 总文件数
  - 按 sector 分布
  - 按 tag 分布（跨板块）
  - 按 signal 区间分布
  - 按 quality_rating 分布
  - 按 source（最近 ingest）的 top N
"""

from collections import Counter, defaultdict
from typing import Optional

from .loader import load_cots, list_cot_files


def _signal_bucket(s: str) -> str:
    """signal 字符串 → 区间标签。"""
    try:
        n = int(s)
    except (TypeError, ValueError):
        return "未知"
    if n >= 9:
        return "9-10 (强)"
    if n >= 7:
        return "7-8  (中强)"
    if n >= 4:
        return "4-6  (中)"
    return "1-3  (弱)"


def compute_stats(sector: Optional[str] = None, tag: Optional[str] = None,
                  min_signal: int = 0) -> dict:
    """聚合统计。返回:

    {
      "total_cots": int,
      "total_files": int,
      "by_sector": [(sector, count), ...],          # 倒序
      "by_tag":    [(tag, count), ...],             # 倒序
      "by_signal": [(bucket, count), ...],
      "by_quality":[(stars, count), ...],
      "recent_sources": [(source, date, count), ...] # 最多 10 条
    }
    """
    cots = load_cots(sector=sector, tag=tag, min_signal=min_signal)
    files = list_cot_files(sector=sector)

    sector_counter = Counter()
    tag_counter = Counter()
    signal_counter = Counter()
    quality_counter = Counter()
    source_meta = defaultdict(lambda: {"count": 0, "date": ""})

    for c in cots:
        sector_counter[c.get("_sector") or "uncategorized"] += 1
        for t in c.get("_tags", []):
            tag_counter[t] += 1
        signal_counter[_signal_bucket(c.get("signal", "5"))] += 1
        q = c.get("_quality_rating", 0)
        quality_counter[q if q > 0 else "未评级"] += 1
        src = c.get("_source", "?")
        source_meta[src]["count"] += 1
        source_meta[src]["date"] = c.get("_created_at", "")

    # 按日期倒序、相同日期按 count 倒序
    recent_sources = sorted(
        ((s, m["date"], m["count"]) for s, m in source_meta.items()),
        key=lambda x: (x[1] or "0", x[2]),
        reverse=True,
    )[:10]

    # 把质量分单独排序：数字升序，"未评级" 放最后
    def _quality_key(item):
        k = item[0]
        return (1, 0) if k == "未评级" else (0, k)

    return {
        "total_cots": len(cots),
        "total_files": len(files),
        "by_sector": sector_counter.most_common(),
        "by_tag": tag_counter.most_common(),
        "by_signal": sorted(
            signal_counter.items(),
            key=lambda x: ["9-10 (强)", "7-8  (中强)", "4-6  (中)", "1-3  (弱)", "未知"].index(x[0])
            if x[0] in ["9-10 (强)", "7-8  (中强)", "4-6  (中)", "1-3  (弱)", "未知"]
            else 999,
        ),
        "by_quality": sorted(quality_counter.items(), key=_quality_key),
        "recent_sources": recent_sources,
    }


def render_dashboard(stats: dict, max_sector: int = 12, max_tag: int = 15) -> str:
    """把 stats dict 渲染成多行文本（用于 cli 打印）。"""
    lines = []
    lines.append(f"\n{'=' * 60}")
    lines.append(f"  CoT 全库统计")
    lines.append(f"{'=' * 60}")
    lines.append(f"  总条数: {stats['total_cots']}    总文件数: {stats['total_files']}")

    if stats["by_signal"]:
        lines.append(f"\n[按信号强度分布]")
        max_n = max((n for _, n in stats["by_signal"]), default=1)
        for bucket, n in stats["by_signal"]:
            bar = "▰" * max(1, int(n / max_n * 20))
            lines.append(f"  {bucket:<14} {n:4d}  {bar}")

    if stats["by_quality"]:
        lines.append(f"\n[按研报质量分布]")
        for stars, n in stats["by_quality"]:
            label = f"⭐ × {stars}" if isinstance(stars, int) else stars
            lines.append(f"  {label:<10} {n:4d}")

    if stats["by_sector"]:
        lines.append(f"\n[按板块分布 (top {max_sector})]")
        max_n = max((n for _, n in stats["by_sector"][:max_sector]), default=1)
        for sec, n in stats["by_sector"][:max_sector]:
            bar = "▰" * max(1, int(n / max_n * 20))
            lines.append(f"  {sec:<32} {n:4d}  {bar}")
        if len(stats["by_sector"]) > max_sector:
            lines.append(f"  ... 还有 {len(stats['by_sector']) - max_sector} 个 sector")

    if stats["by_tag"]:
        lines.append(f"\n[按主题 tag 分布 (top {max_tag}, 跨板块)]")
        max_n = max((n for _, n in stats["by_tag"][:max_tag]), default=1)
        for t, n in stats["by_tag"][:max_tag]:
            bar = "▰" * max(1, int(n / max_n * 20))
            lines.append(f"  #{t:<24} {n:4d}  {bar}")
        if len(stats["by_tag"]) > max_tag:
            lines.append(f"  ... 还有 {len(stats['by_tag']) - max_tag} 个 tag")

    if stats["recent_sources"]:
        lines.append(f"\n[最近 ingest 的来源 (最多 10 份)]")
        for src, dt, n in stats["recent_sources"]:
            lines.append(f"  [{dt or '?':<10}]  {n:3d} 条  {src[:60]}")

    lines.append("")
    return "\n".join(lines)
