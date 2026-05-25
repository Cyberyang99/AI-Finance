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

COT_USER_PROMPT_TEMPLATE = """忘掉你以前的所有提示。请仔细阅读下面的研报内容，总结出不少于 10 条最重要的分析个股未来表现的推理思维链。

## 每条思维链总结要求

1. 识别核心驱动因素
2. 明确由驱动因素到结论的传导路径，例如 A → B → C → D → 下季度营收增长 / 利好股价表现
3. 验证逻辑链条的严密性，进行环节补充或精炼完善
4. 最终结论：信号强度评分（1-10 分），根据该条思维链的推理逻辑可靠程度打分

## 输出格式要求

严格 JSON 数组，每个对象包含且仅包含三个字段，所有字段值必须是字符串：

- `trigger`: 核心驱动因素（一句话）
- `COT`: 经过验证完善的由驱动因素传导到结论的投资逻辑链
- `signal`: 1-10 的信号强度评分（字符串形式）

要求：
- JSON 必须符合标准语法，无额外字段、无语法错误
- 嵌套双引号要转义
- 不要 markdown 代码块包裹，直接输出 JSON 数组
- 除 JSON 外不要有任何其他内容

## 研报内容

{document_text}

请精炼总结至少 10 条逻辑完善的思维链，输出 JSON 数组："""


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


def extract_cot(document_text: str, max_chars: int = 60000) -> list[dict]:
    """从研报文本中提取 CoT 三段式列表。

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
    user_msg = COT_USER_PROMPT_TEMPLATE.format(document_text=document_text)

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
                  source_filename: str, source_hash: str) -> Path:
    """把提炼出的 CoT 列表写到 memory/knowledge/cot/<sector>/yyyy-mm_<hash>_<src>.md.

    Frontmatter 包含 ticker/sector/source/created_at。
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

    lines = [
        "---",
        f"ticker: {ticker or ''}",
        f"sector: {sect}",
        f"source: {source_filename}",
        f"source_hash: {source_hash}",
        f"created_at: {today}",
        f"cot_count: {len(cots)}",
        "---",
        "",
        f"# CoT 提取自 {source_filename}",
        "",
    ]
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
