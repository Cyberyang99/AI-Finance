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
# v3：新增 falsifiability（可证伪性/具体性），专治"一家之言"软论断；给足权重才能压下去
DEFAULT_SCORE_WEIGHTS = {
    "transmission": 0.35,   # 传导明确性
    "falsifiability": 0.30, # 可证伪性/具体性（新增）
    "history": 0.20,        # 历史可验证性
    "recency": 0.15,        # 时效性
}
_SCORE_DIMS = ("transmission", "falsifiability", "history", "recency")

COT_SYSTEM_PROMPT = """你是擅长提取内在逻辑的资深股票分析师。"""

COT_USER_PROMPT_TEMPLATE = """忘掉你以前的所有提示。请仔细阅读下面的研报内容，按下述规则提取**行业层面可复用**的投资思维链。
{comment_section}

## 第 0 步：榨干硬数据（默认就要做，别等人提醒）

研报的价值密度往往在**图表、表格、数据页**里，而不是正文叙述。抽取前先通读一遍，主动捞出所有硬数据：
- 量化参数：参数量、上下文长度、价格/定价、市占率、增速、产能、出货量、毛利率等具体数字
- 时间线：产品发布/迭代节奏、roadmap、解禁/到期、产能投放、催化剂的具体时间点
- 横向对比：各家/各产品的基准分数、规格、定价对比（表格里的对比尤其关键）

处理要求：
- 输入文本可能来自 PDF/PPT，**版面错乱、表格被打散成碎片**——你要在脑子里把它们还原成结构，别被格式噪声带偏。
- 这些硬数据是逻辑的**锚**：trigger 仍保持行业层面泛化，但 **COT 传导链和 evidence 必须带上支撑性的具体数字/时间线**，不要泛化成空话。
- 能在表格/对比里查到的具体数值，优先放进 evidence（原文依据）字段逐字保留。

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

## 第 3 步：每条思维链的四档打分（关键！）

对每条 CoT 都要给四个 1-10 的子分。打分要克制——一批 CoT 里通常只有约 15% 该到 8+，约 50% 在 6-7，约 35% 在 5 以下。**不要给每条都打高分。**

- **transmission** (传导明确性): A → B → C → 股价的链条是否清晰、关键节点是否完整
  - 9-10: 链条完整且每一环都有公开数据可追踪
  - 6-8:  链条完整但部分环节需要主观判断
  - 1-5:  链条跳跃或某环节是黑盒

- **falsifiability** (可证伪性/具体性): 这条到底是"可观测可证伪的传导逻辑"还是"一家之言的价值判断"
  - 9-10: 有明确**可观测的触发条件**（具体数字/事件，能在公开数据里查到是否发生）+ 明确**反证条件**（什么发生就证明这条逻辑错了）
  - 6-8:  触发条件可观测，但反证条件模糊，或含部分主观判断
  - 1-5:  **纯价值判断/静态论断/不可证伪**——如"管理文化优秀""护城河强""话语权提升""竞争力被认可"这类没有可观测触发、无法证伪的陈述句，一律给低分
  > ⚠ 这一维专门压制"听起来对但无法验证"的软论断。遇到没有具体触发数字、不能被证伪的，falsifiability 必须 ≤4。

- **history** (历史可验证性): 同类逻辑在历史上是否被验证过、回测胜率
  - 9-10: 过去 10 年至少出现过 3 次且每次都驱动股价（如周期反转、产能爬坡）
  - 6-8:  历史上验证过 1-2 次或同类型不同领域有过
  - 1-5:  全新逻辑、无历史参照

- **recency** (时效性): 触发条件是否还在持续 / 多久会兑现
  - 9-10: 当前正在发生且 6 个月内会有进一步验证点
  - 6-8:  趋势成立但兑现节奏不确定
  - 1-5:  长期逻辑（>2 年）或时效已过

最终 signal = transmission*{w_t} + falsifiability*{w_f} + history*{w_h} + recency*{w_r}，向最近整数取整。

## 第 4 步：标注原文依据 (evidence)

每条 CoT 都要附一段 **evidence** —— 从研报里**逐字摘抄**最能支撑这条逻辑的原句（≤80 字），用于日后回溯和验证。
- 优先摘抄含**具体数字、事件、可观测触发条件**的原句（这正是 falsifiability 的来源）。
- 摘抄要忠于原文，不要改写、不要总结。
- 如果这条 CoT 是你跨段落归纳出来的、没有单一对应原句，evidence 留空字符串 ""。
> evidence 与 falsifiability 强相关：能摘到带数字/事件的原句，falsifiability 才有资格给高分；摘不到任何可观测原句的，falsifiability 必须 ≤4。

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
      "falsifiability": <1-10>,
      "history": <1-10>,
      "recency": <1-10>,
      "signal": <1-10，四档加权后向最近整数取整>,
      "evidence": "<支撑本条的研报原文摘录，逐字摘抄≤80字；无单一对应原句则留空>"
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
    for k in _SCORE_DIMS:
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
    """从子分计算 signal；若 LLM 已给出合理 signal 也接受。返回 1-10 整数。

    四维：transmission / falsifiability / history / recency。
    兼容旧 3 维文件：falsifiability 缺失时用 history 兜底（不凭空给高分）。
    """
    vals = {}
    any_sub = False
    for k in _SCORE_DIMS:
        try:
            v = float(c.get(k, 0))
        except (TypeError, ValueError):
            v = 0
        vals[k] = v
        if v > 0:
            any_sub = True
    # 旧 3 维文件没有 falsifiability：用 history 兜底，避免缺维被当 0 拖垮
    if vals["falsifiability"] == 0 and (vals["transmission"] > 0 or vals["history"] > 0):
        vals["falsifiability"] = vals["history"] or vals["transmission"]
    if any_sub:
        s = sum(vals[k] * weights[k] for k in _SCORE_DIMS)
        return max(1, min(10, round(s)))
    # 子分全空，退回 LLM 直给的 signal
    try:
        s = int(float(str(c.get("signal", "5"))))
        return max(1, min(10, s))
    except (TypeError, ValueError):
        return 5


def _chunk_text(text: str, size: int) -> list[str]:
    """把超长文本按段落边界切成 ≤size 的块；单段超长则硬切。"""
    paras = text.split("\n\n")
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:
        if len(c) <= size:
            out.append(c)
        else:  # 单段就超长，硬切
            out.extend(c[i:i + size] for i in range(0, len(c), size))
    return out


def _extract_once(document_text: str, comment_section: str, count_directive: str,
                  weights: dict, model: str, client) -> tuple[int, str, list]:
    """对单块文本跑一次 LLM 提取，返回 (quality_rating, quality_reason, cots_raw)。

    失败返回 (0, 原因, [])，由调用方决定是否致命。
    """
    user_msg = COT_USER_PROMPT_TEMPLATE.format(
        document_text=document_text,
        comment_section=comment_section,
        count_directive=count_directive,
        w_t=round(weights["transmission"], 2),
        w_f=round(weights["falsifiability"], 2),
        w_h=round(weights["history"], 2),
        w_r=round(weights["recency"], 2),
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=COT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [CoT] 提取失败: {e}")
        return 0, "提取失败", []

    parsed = _parse_json_flexible(text)
    if parsed is None:
        print(f"  [CoT] JSON 解析失败，原始输出前 300 字: {text[:300]}")
        return 0, "JSON 解析失败", []

    if isinstance(parsed, list):  # 兼容旧格式（顶层是 list）
        return 0, "(旧格式 LLM 输出，未提供质量评级)", parsed
    if isinstance(parsed, dict):
        cots_raw = parsed.get("cots") or parsed.get("merged_cots") or []
        try:
            qr = int(parsed.get("quality_rating", 0))
        except (TypeError, ValueError):
            qr = 0
        qreason = str(parsed.get("quality_reason", "")).strip()
        return qr, qreason, cots_raw if isinstance(cots_raw, list) else []
    return 0, "", []


def extract_cot(document_text: str, max_chars: Optional[int] = None, user_comment: str = "",
                min_cots: Optional[int] = None, max_cots: Optional[int] = None,
                force_count: Optional[int] = None) -> dict:
    """从研报文本中提取 CoT。

    返回 {"quality_rating": int, "quality_reason": str, "cots": list[dict]}
    每条 cot: {"trigger", "COT", "transmission", "falsifiability", "history",
              "recency", "signal", "evidence"}

    参数:
        min_cots/max_cots: 强制夹住数量范围（覆盖 LLM 自适应）
        force_count: 直接指定要 N 条（最高优先级）

    失败返回 {"quality_rating": 0, "quality_reason": "提取失败", "cots": []}.
    """
    empty = {"quality_rating": 0, "quality_reason": "提取失败", "cots": []}
    if not document_text or not document_text.strip():
        return empty

    cot_cfg = load_config().get("cot", {})
    agent_cfg = load_config().get("agent", {})
    # 提取模型：优先 [cot] extract_model（一次性重跑可临时切 pro），否则跟 [agent] model
    model = cot_cfg.get("extract_model") or agent_cfg.get("model", "deepseek-v4-pro")
    if max_chars is None:
        try:
            max_chars = int(cot_cfg.get("max_chars", 100000))
        except (TypeError, ValueError):
            max_chars = 100000
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

    # 超长文档分块提取（不再静默截断，避免后半篇逻辑丢失）
    if len(document_text) <= max_chars:
        chunks = [document_text]
    else:
        chunks = _chunk_text(document_text, max_chars)
        print(f"  [CoT] 文档 {len(document_text)} 字 > {max_chars}，分 {len(chunks)} 块提取后汇总")

    cots_raw: list = []
    quality_rating = 0
    quality_reason = ""
    for ci, chunk in enumerate(chunks, 1):
        # 多块时每块各自按内容质量自适应，最后统一裁剪；单块沿用 count_directive
        cd = count_directive if len(chunks) == 1 else \
            "请按本块内容质量决定条数（整篇会汇总后统一裁剪，本块不必凑数）。"
        qr, qreason, raw = _extract_once(chunk, comment_section, cd, weights, model, client)
        if qr > quality_rating:
            quality_rating, quality_reason = qr, qreason
        cots_raw.extend(raw)

    if not cots_raw:
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
        item = {"trigger": trigger, "COT": cot_text, "signal": str(signal),
                "evidence": str(c.get("evidence", "")).strip()}
        # 保留真实子分（含低分），仅在缺失/非数字时才用 signal 兜底
        for k in _SCORE_DIMS:
            raw = c.get(k)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                item[k] = signal
                continue
            try:
                item[k] = max(1, min(10, int(float(raw))))
            except (TypeError, ValueError):
                item[k] = signal
        valid.append(item)

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


def _normalize_incoming_tags(tags: list[str]) -> list[str]:
    """归一化新 tag：① 折叠多余空格；② 吸附到库里已有的拼写（despaced 键相同就用现有写法）。

    这样新摄入的 'AI算力' 会自动对齐到库里的 'AI 算力'，而 '3D世界模型' 等保持原样、
    全新主题原样放行——从源头防止同一主题被拼写差异拆开（见 DEV_NOTES 踩坑）。
    """
    import unicodedata
    from collections import Counter
    from ..cot.loader import COT_DIR as _CD, _parse_frontmatter as _pf, _parse_tags as _pt

    def nkey(t):
        return unicodedata.normalize("NFKC", t).replace(" ", "").replace("　", "").lower()

    spell: dict = {}
    try:
        counter: dict = {}
        for fp in _CD.rglob("*.md"):
            if any(x.startswith("_archive") for x in fp.parts):
                continue
            for t in _pt(_pf(fp.read_text(encoding="utf-8")).get("tags", "")):
                counter.setdefault(nkey(t), Counter())[t] += 1
        spell = {k: c.most_common(1)[0][0] for k, c in counter.items()}
    except Exception:
        spell = {}

    out, seen = [], set()
    for t in tags:
        t = re.sub(r"\s+", " ", (t or "").strip())
        if not t:
            continue
        canon = spell.get(nkey(t), t)
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


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

    # 文档级 frontmatter tags = 传入 tags，或各链链级 tag 的并集（向后兼容 + dash/概览用）
    union = list(tags or [])
    if not union:
        _seen_u = set()
        for c in cots:
            for t in (c.get("tags") or []):
                if t and t not in _seen_u:
                    _seen_u.add(t)
                    union.append(t)
    tags_list = _normalize_incoming_tags(union)

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
        lines.append(f"## CoT {i} — {c['trigger']}")
        lines.append("")
        chain_tags = [t for t in (c.get("tags") or []) if t]
        if chain_tags:
            lines.append(f"**主题**: {'、'.join(chain_tags)}")
            lines.append("")
        lines.extend([
            f"**信号强度**: {c['signal']}/10  "
            f"_(传导 {c.get('transmission', '?')} · 证伪 {c.get('falsifiability', '?')} · "
            f"历史 {c.get('history', '?')} · 时效 {c.get('recency', '?')})_",
            "",
            f"**推理链**: {c['COT']}",
            "",
        ])
        ev = str(c.get("evidence", "")).strip()
        if ev:
            lines.extend([f"**原文依据**: 「{ev}」", ""])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
