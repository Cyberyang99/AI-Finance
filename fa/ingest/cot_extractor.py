"""CoT 提取器 — 研报文本 → 三段式思维链 (trigger / COT / signal 1-10).

Prompt 模板来自国金证券《主观投资框架验证与个股决策 Agent》。

v2 升级（2026-05-26）:
1. **自适应数量**：LLM 先评 1-5 星质量，再决定抽 5-20 条；CLI 可 --min/--max/--cot-count 强制覆盖
2. **显化打分维度**：每条输出三个子分（传导明确性 trans / 历史可验证性 hist / 时效性 recency），
   signal = 三者加权和（权重在 config.toml [cot.score_weights] 配置）
3. **JSON 兜底升级**：处理嵌套代码块、转义引号、断行
4. frontmatter 加 quality_rating 字段
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..config import load_config, make_anthropic_client
from ..memory.store import PROJECT_DIR

COT_DIR = PROJECT_DIR / "memory" / "knowledge" / "cot"

# 默认打分权重（用户可在 config.toml [cot.score_weights] 覆盖）
DEFAULT_SCORE_WEIGHTS = {
    "transmission": 0.5,   # 传导明确性
    "history": 0.3,        # 历史可验证性
    "recency": 0.2,        # 时效性
}

COT_SYSTEM_PROMPT = """你是擅长提取内在逻辑的资深股票分析师。"""

COT_USER_PROMPT_TEMPLATE = """忘掉你以前的所有提示。请仔细阅读下面的研报内容，按下述规则提取**行业层面可复用**的投资思维链。
{comment_section}

## 第 1 步：评定研报质量 (quality_rating, 1-5 星)

读完研报后，先给整份研报打质量分：
- ⭐⭐⭐⭐⭐ (5): 体系完整、逻辑严密、数据扎实、有独立判断 → 抽 15-20 条
- ⭐⭐⭐⭐  (4): 论证清晰、有数据支持 → 抽 10-15 条
- ⭐⭐⭐   (3): 中规中矩、信息密度普通 → 抽 8-12 条
- ⭐⭐    (2): 内容松散、多为复述 → 抽 5-8 条
- ⭐     (1): 公关稿/营销文 / 信息密度极低 → 抽 3-5 条（甚至可全部弃用）

{count_directive}

## 第 2 步：行业层面的泛化（最重要）

你的任务不是复述研报里某家公司的具体新闻或动作，而是抽象出**整个行业内任何一家公司都可能适用的投资逻辑**。

❌ 反例（公司特化，不要这么写）：
- "DeepSeek V4 采用 mHC、Muon 算法创新"
- "Apple 发布 Vision Pro 新产品"

✅ 正例（行业层面，要这么写）：
- "AI 公司算法/架构层面的核心创新（不限于 mHC、MoE 等具体技术）"
- "消费电子公司发布全新形态产品并启动出货爬坡"

当研报提到某家公司做了 X，问自己——**"X 这件事如果发生在同行业其他公司身上，是否也会驱动股价？"** 是 → 写成 CoT；否 → 弃用。

## 第 3 步：每条思维链的三档打分（关键改动！）

对每条 CoT 都要给三个 1-10 的子分，便于事后调权重：

- **transmission** (传导明确性): A → B → C → 股价的链条是否清晰、关键节点是否完整
  - 9-10: 链条完整且每一环都有公开数据可追踪
  - 6-8:  链条完整但部分环节需要主观判断
  - 1-5:  链条跳跃或某环节是黑盒

- **history** (历史可验证性): 同类逻辑在历史上是否被验证过、回测胜率
  - 9-10: 过去 10 年至少出现过 3 次且每次都驱动股价（如周期反转、产能爬坡）
  - 6-8:  历史上验证过 1-2 次或同类型不同领域有过
  - 1-5:  全新逻辑、无历史参照

- **recency** (时效性): 触发条件是否还在持续 / 多久会兑现
  - 9-10: 当前正在发生且 6 个月内会有进一步验证点
  - 6-8:  趋势成立但兑现节奏不确定
  - 1-5:  长期逻辑（>2 年）或时效已过

最终 signal = transmission*{w_t} + history*{w_h} + recency*{w_r}，向最近整数取整。

## 输出格式

严格 JSON 对象（不要 markdown 代码块包裹）：

{{
  "quality_rating": <1-5 整数>,
  "quality_reason": "<一句话说明给几星的原因>",
  "cots": [
    {{
      "trigger": "<行业层面驱动因素，不带公司名>",
      "COT": "<完整因果传导链>",
      "transmission": <1-10>,
      "history": <1-10>,
      "recency": <1-10>,
      "signal": <1-10，三档加权后向最近整数取整>
    }}
  ]
}}

要求：
- JSON 必须符合标准语法
- 嵌套引号一律转义
- 不要 markdown 代码块包裹
- cots 数组长度严格遵守第 1 步定的范围
- 除 JSON 外不要任何其他内容

## 研报内容

{document_text}

请输出 JSON："""


def _get_score_weights() -> dict:
    """从 config.toml [cot.score_weights] 读权重，缺失走默认。"""
    cfg = load_config().get("cot", {}).get("score_weights", {}) or {}
    w = dict(DEFAULT_SCORE_WEIGHTS)
    for k in ("transmission", "history", "recency"):
        v = cfg.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            w[k] = float(v)
    # 归一化（防用户瞎写）
    total = sum(w.values())
    if total <= 0:
        return dict(DEFAULT_SCORE_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def _strip_json_comments(text: str) -> str:
    """剥掉 // 行注释 + /* */ 块注释 — LLM 偶尔会塞进去导致 JSON 解析失败。"""
    # 块注释
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # 行注释（但不要碰 // 在字符串内的，简单实现就好）
    out_lines = []
    for line in text.split("\n"):
        # 找第一个不在字符串中的 //
        in_str = False
        escape = False
        idx = -1
        for i, ch in enumerate(line):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                idx = i
                break
        if idx >= 0:
            out_lines.append(line[:idx])
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _parse_json_flexible(text: str):
    """容错解析 JSON（dict 或 list 都接受）。

    处理：markdown 代码块包裹、注释、首尾噪声、嵌套大括号。
    返回 dict / list / None。
    """
    if not text:
        return None
    # 1) 去 markdown 代码块
    for pat in (r"```(?:json)?\s*(\{.*\})\s*```", r"```(?:json)?\s*(\[.*\])\s*```"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            inner = _strip_json_comments(m.group(1))
            try:
                return json.loads(inner)
            except Exception:
                pass
    # 2) 找第一个 { ... } 或 [ ... ]
    cleaned = _strip_json_comments(text)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        if start < 0:
            continue
        # 用括号配对找匹配位置（处理嵌套）
        depth = 0
        end = -1
        in_str = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                # 兜底：尝试自动闭合可能的截断
                try:
                    return json.loads(cleaned[start:end + 1] + close_ch)
                except Exception:
                    pass
    return None


def _coerce_signal(c: dict, weights: dict) -> int:
    """从子分计算 signal；若 LLM 已给出合理 signal 也接受。返回 1-10 整数。"""
    try:
        t = float(c.get("transmission", 0))
        h = float(c.get("history", 0))
        r = float(c.get("recency", 0))
    except (TypeError, ValueError):
        t = h = r = 0
    if t > 0 or h > 0 or r > 0:
        s = t * weights["transmission"] + h * weights["history"] + r * weights["recency"]
        return max(1, min(10, round(s)))
    # 子分全空，退回 LLM 直给的 signal
    try:
        s = int(float(str(c.get("signal", "5"))))
        return max(1, min(10, s))
    except (TypeError, ValueError):
        return 5


def extract_cot(document_text: str, max_chars: int = 60000, user_comment: str = "",
                min_cots: Optional[int] = None, max_cots: Optional[int] = None,
                force_count: Optional[int] = None) -> dict:
    """从研报文本中提取 CoT。

    返回 {"quality_rating": int, "quality_reason": str, "cots": list[dict]}
    每条 cot: {"trigger", "COT", "transmission", "history", "recency", "signal"}

    参数:
        min_cots/max_cots: 强制夹住数量范围（覆盖 LLM 自适应）
        force_count: 直接指定要 N 条（最高优先级）

    失败返回 {"quality_rating": 0, "quality_reason": "提取失败", "cots": []}.
    """
    empty = {"quality_rating": 0, "quality_reason": "提取失败", "cots": []}
    if not document_text or not document_text.strip():
        return empty

    if len(document_text) > max_chars:
        document_text = document_text[:max_chars] + "\n\n_(文档过长，已截断)_"

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-pro")
    weights = _get_score_weights()

    client = make_anthropic_client()
    comment_section = (
        f"\n## ⚠ 用户角度提示（请优先围绕这个角度提取 CoT）\n\n{user_comment.strip()}\n"
        if user_comment and user_comment.strip()
        else ""
    )

    # 数量指令
    if force_count and force_count > 0:
        count_directive = f"⚠ 用户强制要求：本次必须输出恰好 {force_count} 条 CoT，质量评级仅作参考不约束数量。"
    elif min_cots and max_cots:
        count_directive = (
            f"⚠ 用户给定的数量上下限：本次输出条数必须落在 [{min_cots}, {max_cots}]，"
            f"质量评级在该区间内自行决定。"
        )
    elif min_cots:
        count_directive = f"⚠ 用户要求至少 {min_cots} 条，质量评级决定最多多少。"
    elif max_cots:
        count_directive = f"⚠ 用户要求最多 {max_cots} 条，质量评级决定具体多少。"
    else:
        count_directive = "请严格按质量评级决定数量。"

    user_msg = COT_USER_PROMPT_TEMPLATE.format(
        document_text=document_text,
        comment_section=comment_section,
        count_directive=count_directive,
        w_t=weights["transmission"],
        w_h=weights["history"],
        w_r=weights["recency"],
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=10000,
            system=COT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [CoT] 提取失败: {e}")
        return empty

    parsed = _parse_json_flexible(text)
    if parsed is None:
        print(f"  [CoT] JSON 解析失败，原始输出前 300 字: {text[:300]}")
        return empty

    # 兼容旧格式（顶层是 list）→ 包成新格式
    if isinstance(parsed, list):
        cots_raw = parsed
        quality_rating = 0
        quality_reason = "(旧格式 LLM 输出，未提供质量评级)"
    elif isinstance(parsed, dict):
        cots_raw = parsed.get("cots") or parsed.get("merged_cots") or []
        try:
            quality_rating = int(parsed.get("quality_rating", 0))
        except (TypeError, ValueError):
            quality_rating = 0
        quality_reason = str(parsed.get("quality_reason", "")).strip()
    else:
        return empty

    if not isinstance(cots_raw, list):
        return empty

    # 标准化每条
    valid = []
    for c in cots_raw:
        if not isinstance(c, dict):
            continue
        trigger = str(c.get("trigger", "")).strip()
        cot_text = str(c.get("COT", "") or c.get("cot", "")).strip()
        if not trigger or not cot_text:
            continue
        signal = _coerce_signal(c, weights)
        valid.append({
            "trigger": trigger,
            "COT": cot_text,
            "transmission": int(c.get("transmission", signal)) if c.get("transmission") else signal,
            "history": int(c.get("history", signal)) if c.get("history") else signal,
            "recency": int(c.get("recency", signal)) if c.get("recency") else signal,
            "signal": str(signal),
        })

    # 强制数量约束（force_count 或 min/max）—— 只能裁剪，无法补充
    if force_count and len(valid) > force_count:
        valid = sorted(valid, key=lambda c: -int(c["signal"]))[:force_count]
    elif max_cots and len(valid) > max_cots:
        valid = sorted(valid, key=lambda c: -int(c["signal"]))[:max_cots]

    return {
        "quality_rating": quality_rating,
        "quality_reason": quality_reason,
        "cots": valid,
    }


def save_cot_file(cots: list[dict], ticker: Optional[str], sector: Optional[str],
                  source_filename: str, source_hash: str,
                  user_comment: str = "", tags: Optional[list] = None,
                  quality_rating: int = 0, quality_reason: str = "") -> Path:
    """把提炼出的 CoT 列表写到 memory/knowledge/cot/<sector>/yyyy-mm_<hash>_<src>.md.

    sector 应为标准化的 sector_id（来自 sectors.yaml），不是自由文本。
    tags 是细分主题（list[str]），写到 frontmatter，供 fa cot list --tag 跨板块召回。
    quality_rating: 1-5 星，由 extract_cot 返回。
    """
    sect = sector or "uncategorized"
    safe_sect = re.sub(r"[\\/:*?\"<>|]", "_", sect)
    target_dir = COT_DIR / safe_sect
    target_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    yyyymm = today[:7]
    stem = Path(source_filename).stem[:40]
    safe_stem = re.sub(r"[\\/:*?\"<>|]", "_", stem)
    fname = f"{yyyymm}_{source_hash}_{safe_stem}.md"
    path = target_dir / fname

    tags_list = [t.strip() for t in (tags or []) if t and t.strip()]

    lines = [
        "---",
        f"ticker: {ticker or ''}",
        f"sector: {sect}",
        f"source: {source_filename}",
        f"source_hash: {source_hash}",
        f"created_at: {today}",
        f"cot_count: {len(cots)}",
    ]
    if quality_rating > 0:
        lines.append(f"quality_rating: {quality_rating}")
    if tags_list:
        lines.append(f"tags: [{', '.join(tags_list)}]")
    if user_comment and user_comment.strip():
        safe_c = user_comment.strip().replace("\n", " ")
        lines.append(f"user_comment: {safe_c}")
    lines.extend([
        "---",
        "",
        f"# CoT 提取自 {source_filename}",
        "",
    ])
    if quality_rating > 0:
        stars = "⭐" * quality_rating
        lines.extend([
            f"**研报质量**: {stars} ({quality_rating}/5){' — ' + quality_reason if quality_reason else ''}",
            "",
        ])
    if tags_list:
        lines.extend(["**主题 tags**: " + " · ".join(f"#{t}" for t in tags_list), ""])
    if user_comment and user_comment.strip():
        lines.extend([
            "## 🗨 用户角度提示",
            "",
            user_comment.strip(),
            "",
        ])
    for i, c in enumerate(cots, 1):
        lines.extend([
            f"## CoT {i} — {c['trigger']}",
            "",
            f"**信号强度**: {c['signal']}/10  "
            f"_(传导 {c.get('transmission', '?')} · 历史 {c.get('history', '?')} · 时效 {c.get('recency', '?')})_",
            "",
            f"**推理链**: {c['COT']}",
            "",
        ])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
