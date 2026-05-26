"""CoT 提取器 — 研报文本 → 三段式思维链 (trigger / COT / signal 1-10).

Prompt 模板来自国金证券《主观投资框架验证与个股决策 Agent》。
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..config import load_config, make_anthropic_client
from ..memory.store import PROJECT_DIR

COT_DIR = PROJECT_DIR / "memory" / "knowledge" / "cot"

COT_SYSTEM_PROMPT = """你是擅长提取内在逻辑的资深股票分析师。"""

COT_USER_PROMPT_TEMPLATE = """忘掉你以前的所有提示。请仔细阅读下面的研报内容，总结出不少于 10 条**行业层面可复用**的投资思维链。
{comment_section}

## 核心要求：行业层面的泛化

**这一条最重要**：你的任务不是复述研报里某家公司的具体新闻或动作，而是抽象出**整个行业内任何一家公司都可能适用的投资逻辑**。

❌ 反例（公司特化，不要这么写）：
- "DeepSeek V4 采用 mHC、Muon 算法创新"
- "Apple 发布 Vision Pro 新产品"
- "宁德时代麒麟电池产能爬坡"

✅ 正例（行业层面，要这么写）：
- "AI 公司算法/架构层面的核心创新（不限于 mHC、MoE 等具体技术）"
- "消费电子公司发布全新形态产品并启动出货爬坡"
- "动力电池公司新一代产品产能爬坡 + 大客户绑定"

具体方法：当研报提到某家公司做了 X，问自己——**"X 这件事如果发生在同行业其他公司身上，是否也会驱动股价？传导路径是不是一样的？"** 如果答案是"是"，那 X 就是一条可复用逻辑；如果"否"（只是这家公司特有的事件），则不要写成 CoT。

## 每条思维链结构

1. 识别**行业可复用**的核心驱动因素（trigger）
2. 明确由驱动因素到股价/业绩结论的传导路径：A → B → C → D → 业绩变化 → 股价表现
3. 验证逻辑链条的严密性
4. 评估信号强度（1-10），根据"传导路径的明确性"+"历史回测可验证性"打分

## 输出格式

严格 JSON 数组，每个对象**且仅包含**三个字段：

- `trigger`: 核心驱动因素（必须是行业层面的现象/动作，不带公司名）
- `COT`: 完整的因果传导链
- `signal`: 1-10 评分（字符串）

要求：
- JSON 必须符合标准语法，无额外字段、无语法错误
- 嵌套双引号要转义
- 不要 markdown 代码块包裹
- 除 JSON 外不要有任何其他内容

## 研报内容

{document_text}

请精炼总结至少 10 条**可在同行业其他公司上复用**的思维链，输出 JSON 数组："""


def _parse_json_array(text: str) -> Optional[list]:
    """容错解析 JSON 数组，处理 markdown 代码块包裹的情况。"""
    if not text:
        return None
    # 去掉 markdown 代码块
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 找第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def extract_cot(document_text: str, max_chars: int = 60000, user_comment: str = "") -> list[dict]:
    """从研报文本中提取 CoT 三段式列表。

    user_comment: 可选，用户对该研报的一句话评论/角度提示，会注入 prompt 引导 LLM
                  优先围绕该角度提取 CoT。

    返回 list[{"trigger": str, "COT": str, "signal": str}]
    失败返回空 list。
    """
    if not document_text or not document_text.strip():
        return []

    # 截断超长文本（DeepSeek 上下文窗口考虑）
    if len(document_text) > max_chars:
        document_text = document_text[:max_chars] + "\n\n_(文档过长，已截断)_"

    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-pro")

    client = make_anthropic_client()
    comment_section = (
        f"\n## ⚠ 用户角度提示（请优先围绕这个角度提取 CoT）\n\n{user_comment.strip()}\n"
        if user_comment and user_comment.strip()
        else ""
    )
    user_msg = COT_USER_PROMPT_TEMPLATE.format(
        document_text=document_text, comment_section=comment_section
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=COT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [CoT] 提取失败: {e}")
        return []

    cots = _parse_json_array(text)
    if not cots:
        print(f"  [CoT] JSON 解析失败，原始输出前 200 字: {text[:200]}")
        return []

    # 过滤无效条目
    valid = []
    for c in cots:
        if not isinstance(c, dict):
            continue
        if not (c.get("trigger") and c.get("COT")):
            continue
        valid.append({
            "trigger": str(c.get("trigger", "")).strip(),
            "COT": str(c.get("COT", "")).strip(),
            "signal": str(c.get("signal", "5")).strip(),
        })
    return valid


def save_cot_file(cots: list[dict], ticker: Optional[str], sector: Optional[str],
                  source_filename: str, source_hash: str,
                  user_comment: str = "", tags: Optional[list] = None) -> Path:
    """把提炼出的 CoT 列表写到 memory/knowledge/cot/<sector>/yyyy-mm_<hash>_<src>.md.

    sector 应为标准化的 sector_id（来自 sectors.yaml），不是自由文本。
    tags 是细分主题（list[str]），写到 frontmatter，供 fa cot list --tag 跨板块召回。
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
    if tags_list:
        # YAML 数组单行格式，便于 grep
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
            f"**信号强度**: {c['signal']}/10",
            "",
            f"**推理链**: {c['COT']}",
            "",
        ])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
