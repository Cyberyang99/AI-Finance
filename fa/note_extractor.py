"""从一份文档/一段文本中抽 canonical_15d_v1 个股投资逻辑 note.

两步：
  1. is_individual_research(doc_text) — LLM 快速判断是否针对某具体股票的深度研究
  2. extract_12d(doc_text, ticker, comment) — LLM 抽 15 个稀疏维度

为什么独立成一个模块：和 cot_extractor 并列。
ingest 时双产出（CoT + 可选 note）。
"""

from __future__ import annotations
import json
import re
from typing import Optional

from .config import load_config, make_anthropic_client
from .note_template import DIMENSIONS, JSON_DIM_IDS, JSON_SCHEMA_EXAMPLES, empty_payload


# ── Step 1: 判断是否个股深度研究 ──

CHECK_SYSTEM = "你是文档分类专家。判断输入资料是否为针对某只具体股票的深度研究。"

CHECK_TEMPLATE = """读下面这份资料的前 1500 字 + 文件名 + 用户描述。

判断它是否为"针对某只具体股票/公司的深度研究"，并尝试识别股票/公司名。

## 判断标准

是 (yes)：聚焦于某家公司（含股票代码或公司名），覆盖业务、竞争、财务、估值等深度内容。例如：
  - 「XX 公司投资逻辑分析」
  - 「[券商] - 公司深度报告」
  - 卖方分析师写的覆盖某家公司的 PPT/文档

不是 (no)：聚焦行业 / 主题 / 宏观 / 专家纪要 / 资料汇编，没有针对单一公司的深度判断。例如：
  - 「AI 大模型行业报告」
  - 「半导体周观察」
  - 「专家会议纪要」（除非通篇围绕一家公司）
  - 「策略月报」

## 资料

- 文件名: {filename}
- 用户描述: {user_comment}
- 内容前 1500 字:

---
{text_preview}
---

## 输出格式

严格 JSON（不要 markdown 代码块）：

```
{{
  "is_individual_research": true | false,
  "company_name_cn": "<公司中文名 if can identify, 否则空字符串>",
  "ticker_hint": "<可能的股票代码 if mentioned in text, 否则空字符串>",
  "confidence": "high | medium | low",
  "reasoning": "<一句话理由>"
}}
```

除 JSON 外不要任何其他内容。"""


def is_individual_research(filename: str, doc_text: str, user_comment: str = "") -> dict:
    """LLM 判断是否个股深度研究。

    返回 {is_individual_research: bool, company_name_cn, ticker_hint, confidence, reasoning}.
    失败兜底返回 is_individual_research=False。
    """
    preview = (doc_text or "")[:1500]
    cfg = load_config().get("agent", {})
    model = cfg.get("model", "deepseek-v4-flash")

    try:
        client = make_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=CHECK_SYSTEM,
            messages=[{"role": "user", "content": CHECK_TEMPLATE.format(
                filename=filename, user_comment=user_comment or "(无)", text_preview=preview
            )}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [is_individual] LLM 失败: {e}")
        return {"is_individual_research": False, "company_name_cn": "", "ticker_hint": "",
                "confidence": "low", "reasoning": f"LLM 失败: {e}"}

    parsed = _parse_json(out)
    if not parsed:
        return {"is_individual_research": False, "company_name_cn": "", "ticker_hint": "",
                "confidence": "low", "reasoning": "JSON 解析失败"}

    return {
        "is_individual_research": bool(parsed.get("is_individual_research", False)),
        "company_name_cn": str(parsed.get("company_name_cn", "")).strip(),
        "ticker_hint": str(parsed.get("ticker_hint", "")).strip(),
        "confidence": parsed.get("confidence", "low"),
        "reasoning": parsed.get("reasoning", ""),
    }


# ── Step 2: 15 维度抽取 ──

EXTRACT_SYSTEM = ("你是资深股票研究员，中性客观。从一份个股投资资料中抽出 15 个维度的结构化投资逻辑，"
                  "写不出来的字段留空，绝不编造。资料的价值密度常在图表/表格里——主动榨干硬数据。")

EXTRACT_TEMPLATE = """从下面这份针对 **{ticker}** 的深度研究资料中，抽出 15 维度的投资逻辑。

## 用户角度提示（可能影响重点）

{user_comment}

## ⚠ 提取前先榨干硬数据（默认就要做）

- 主动从**图表、表格、数据页**捞硬数据：市占率、收入/利润、定价、产能、增速、目标价、时间线、各业务对比——抠进对应维度（尤其量化 JSON 字段）。
- 输入可能来自 PDF/PPT，**版面错乱、表格被打散**，你要在脑中还原结构，别被格式噪声带偏。
- 资料后半段常是盈利预测/远期空间/估值/风险——务必读到底，别只填前半部分。

## 资料原文

---
{text}
---

## 15 个稀疏维度（每个都简明扼要，能填则填，不能则留空字符串/空数组）

{dim_list}

## 量化字段的 JSON Schema 示例（你必须严格按这个结构填）

### financial_forecast 示例
```json
{forecast_example}
```

### long_term_space 示例
```json
{space_example}
```

### valuation_target 示例
```json
{valuation_example}
```

### catalysts 示例
```json
{catalysts_example}
```

### competitive_rating 示例
```json
{competitive_example}
```

## 输出格式

严格 JSON（不要 markdown 代码块包裹）。结构（共 15 键）：

```
{{
  "core_thesis": "...",
  "business_breakdown": "...",
  "market_position": "...",
  "moat": "...",
  "management_governance": "...",
  "financial_quality": "...",
  "financial_forecast": [ ... ],
  "long_term_space": {{ ... }},
  "valuation_target": {{ ... }},
  "catalysts": [ ... ],
  "falsification": "...",
  "risks": "...",
  "competitive_rating": {{ ... }},
  "growth_history": "...",
  "tracking_metrics": "..."
}}
```

## 关键规则

1. **量化优先**：资料里有数字（市占率、收入、利润、市值、目标价）的地方，**必须**抠到 JSON 里
2. **没有就空**：资料里没明确说的不要瞎编。financial_forecast 没数据就 []，valuation_target 没数据就 {{}}
3. **note 是证据切片，不是最终观点**：只记录这份资料支持的内容；跨报告合并、冲突处理交给 fa consolidate
4. **指标单位用「亿元」**：revenue_yi=收入(亿元), profit_yi=利润(亿元), mcap_yi=市值(亿元)
5. **百分比用小数**：net_margin=0.22 (不是 22)，share_pct=0.30 (不是 30)
6. **业务拆解保留细节**：business_breakdown 里把每条业务线的占比/客户/景气分别写到一行
7. **风险条目写量化下跌空间**：能算就算（X% 下跌）

除 JSON 外不要任何其他内容。"""


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
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


def _build_dim_list() -> str:
    """生成给 LLM 看的 15 维度清单（含 hint）。"""
    lines = []
    for i, d in enumerate(DIMENSIONS, 1):
        tag = " (JSON)" if d["is_json"] else ""
        lines.append(f"  {i}. **{d['id']}**{tag} — {d['name']}: {d['hint']}")
    return "\n".join(lines)


def extract_12d(
    ticker: str,
    doc_text: str,
    user_comment: str = "",
    max_chars: Optional[int] = None,
) -> dict:
    """LLM 从文档抽 15 个稀疏维度（12 基础 + 评级/成长史/跟踪指标）。

    返回填了多少算多少的 payload (dict[dim_id -> 内容])；失败返回空 payload。
    """
    if not doc_text or not doc_text.strip():
        return empty_payload()

    if max_chars is None:
        try:
            max_chars = int(load_config().get("cot", {}).get("max_chars", 100000))
        except (TypeError, ValueError):
            max_chars = 100000
    text = doc_text[:max_chars]
    if len(doc_text) > max_chars:
        text += "\n\n_(文档过长，已截断)_"

    cfg = load_config().get("agent", {})
    model = load_config().get("cot", {}).get("extract_model") or cfg.get("model", "deepseek-v4-flash")

    prompt = EXTRACT_TEMPLATE.format(
        ticker=ticker,
        user_comment=user_comment or "(无)",
        text=text,
        dim_list=_build_dim_list(),
        forecast_example=json.dumps(JSON_SCHEMA_EXAMPLES["financial_forecast"], ensure_ascii=False, indent=2),
        space_example=json.dumps(JSON_SCHEMA_EXAMPLES["long_term_space"], ensure_ascii=False, indent=2),
        valuation_example=json.dumps(JSON_SCHEMA_EXAMPLES["valuation_target"], ensure_ascii=False, indent=2),
        catalysts_example=json.dumps(JSON_SCHEMA_EXAMPLES["catalysts"], ensure_ascii=False, indent=2),
        competitive_example=json.dumps(JSON_SCHEMA_EXAMPLES["competitive_rating"], ensure_ascii=False, indent=2),
    )

    try:
        client = make_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        out_text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [note] 15d 抽取 LLM 失败: {e}")
        return empty_payload()

    parsed = _parse_json(out_text)
    if not parsed:
        print(f"  [note] JSON 解析失败，原始: {out_text[:300]}")
        return empty_payload()

    # 把 LLM 输出归一到 schema：键白名单 + 类型对齐
    payload = empty_payload()
    for d in DIMENSIONS:
        v = parsed.get(d["id"])
        if v is None:
            continue
        if d["is_json"]:
            # 是 list/dict 才接受
            if isinstance(v, (list, dict)):
                payload[d["id"]] = v
            elif isinstance(v, str):
                # 容错：LLM 偶尔把 JSON 写成字符串
                try:
                    payload[d["id"]] = json.loads(v)
                except Exception:
                    pass
        else:
            payload[d["id"]] = str(v).strip()

    return payload
