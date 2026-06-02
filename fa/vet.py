"""逻辑校验器 (vet) — 用已有 CoT + note 审视一只股票 / 一个投资想法.

定位：合成层。区别于「建仓→回顾→进化」闭环，这里不做业绩验证，只做知识检索 + 逻辑校验。

输入：
  - ticker（必填）
  - idea（选填）：你的投资想法/逻辑。可内联文本，也可是 word/pdf/pptx/txt/md 文件路径

流程：
  1. 拉基本面快照（fetch_fundamentals）
  2. 自然语言召回：把全库 CoT + note 的紧凑目录交给 LLM，按 ticker+想法语义挑相关项
  3. LLM 合成校验笔记：给你的逻辑打分 / 补充你漏掉的逻辑 / 写反逻辑与风险 / 同业共性

输出：一份 markdown 校验笔记，落盘到 output_dir（默认 ~/Desktop），**不自动入 note 库**。
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from .config import load_config, make_anthropic_client
from .cot.loader import load_cots
from .ingest.base import SUPPORTED_EXT
from .ingest.user_note import load_user_notes
from .tools.data import fetch_fundamentals


def _model() -> str:
    """合成用模型：复用 [cot] extract_model（当前 flash），否则跟 [agent] model。"""
    cfg = load_config()
    return cfg.get("cot", {}).get("extract_model") or cfg.get("agent", {}).get("model", "deepseek-v4-flash")


def _output_dir() -> Path:
    cfg = load_config().get("paths", {})
    return Path(cfg.get("output_dir", "~/Desktop")).expanduser()


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            return None
    return None


def load_idea(idea: str) -> str:
    """idea 可以是内联文本，或一个文件路径（docx/pdf/pptx/xlsx 走抽文器；txt/md 直接读）。"""
    if not idea or not idea.strip():
        return ""
    p = Path(idea.strip()).expanduser()
    if p.exists() and p.is_file():
        ext = p.suffix.lower()
        if ext in SUPPORTED_EXT:
            from .ingest import ingest_file
            try:
                return ingest_file(p)["text"]
            except Exception as ex:
                print(f"  [vet] 想法文件抽文失败({ex})，当作纯文本忽略")
                return ""
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    return idea  # 内联文本


def _fundamentals_snapshot(d: dict) -> str:
    lines = [f"代码: {d.get('ticker','?')}  名称: {d.get('name','?')}  行业: {d.get('sector') or '未知'}"]
    metrics = [
        ("市值", "market_cap_yi" if d.get("market_cap_yi") else "market_cap"),
        ("营收3yCAGR%", "revenue_cagr_3y"), ("毛利率%", "gross_margin"),
        ("净利率%", "net_margin"), ("ROE%", "roe"), ("负债率%", "debt_ratio"),
        ("PE", "pe"), ("股息率%", "div_yield"),
    ]
    parts = [f"{label}={d[key]}" for label, key in metrics if d.get(key) is not None]
    if parts:
        lines.append("  ".join(parts))
    return "\n".join(lines)


# ── 召回 ──

RECALL_SYSTEM = """你是投资知识库的检索助手。给你一只目标股票（含基本面）、用户的投资想法（可能为空），
以及全库 CoT（行业可复用逻辑）和【同业候选 note】（其它个股的研究笔记，已剔除目标公司自身）的紧凑目录。
你的任务：按语义相关性挑出最有用的 CoT，以及**最可比的同业 note**（同行业 / 同主题标签优先，用于横向对比）。
重点：note 要挑「能跟目标公司做对比、提炼共性的同类公司」，不是只看名字像。宁缺毋滥，但别漏掉强可比对象。"""

RECALL_TEMPLATE = """## 目标股票
{snapshot}

## 用户想法（可能为空）
{idea}

## 候选 CoT 目录（编号 C#）
{cot_catalog}

## 同业候选 note 目录（编号 N#，已剔除目标公司自己）
{note_catalog}

## 输出（严格 JSON，无 markdown 包裹）
{{
  "cot_idx": [相关 CoT 的编号数字, 最多 {cot_limit} 个, 按相关性排序],
  "note_idx": [可比同业 note 的编号数字, 最多 {note_limit} 个, 同行业/同标签优先],
  "reasoning": "<一句话说明为什么挑这些、可比在哪>"
}}
只输出 JSON。"""


def _recall(snapshot: str, idea: str, cots: list, notes: list,
            cot_limit: int, note_limit: int, model: str, client) -> tuple[list, list, str]:
    cot_cat = "\n".join(
        f"C{i}: (信号{c.get('signal','?')}|{c.get('_sector','')}|{'/'.join(c.get('_tags', []))}) {c['trigger']}"
        for i, c in enumerate(cots)
    )
    note_cat = "\n".join(
        f"N{i}: [{n['ticker']} | {n.get('sector','?')} | {'/'.join(n.get('tags', [])) or '无标签'}] "
        f"{re.sub(chr(92)+'s+', ' ', n['content'])[:130]}"
        for i, n in enumerate(notes)
    )
    msg = RECALL_TEMPLATE.format(
        snapshot=snapshot, idea=idea or "（无，做产业逻辑命中扫描）",
        cot_catalog=cot_cat or "（空）", note_catalog=note_cat or "（空）",
        cot_limit=cot_limit, note_limit=note_limit,
    )
    try:
        resp = client.messages.create(model=model, max_tokens=1500, system=RECALL_SYSTEM,
                                      messages=[{"role": "user", "content": msg}])
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        print(f"  [vet] 召回 LLM 失败({e})，回退：取信号最高的 CoT")
        top = sorted(range(len(cots)), key=lambda i: -int(cots[i].get("signal", 5)))[:cot_limit]
        return top, list(range(min(len(notes), note_limit))), "（召回失败，按信号兜底）"

    parsed = _parse_json(text) or {}
    cot_idx = [i for i in parsed.get("cot_idx", []) if isinstance(i, int) and 0 <= i < len(cots)]
    note_idx = [i for i in parsed.get("note_idx", []) if isinstance(i, int) and 0 <= i < len(notes)]
    if not cot_idx:  # 兜底
        cot_idx = sorted(range(len(cots)), key=lambda i: -int(cots[i].get("signal", 5)))[:cot_limit]
    return cot_idx[:cot_limit], note_idx[:note_limit], parsed.get("reasoning", "")


# ── 合成 ──

SYNTH_SYSTEM = """你是中性、客观、就事论事的投资分析师。立场不预设多空——只对照已沉淀的行业逻辑(CoT)和同业研究(note)，
判断每条逻辑是否站得住，再补充你自己从知识库里读出的新角度。

铁律：
1. **中性客观**：不附和、不唱衰。证据支持就说成立，证据不足就说存疑，没证据就说「库内无支撑」。
2. **逆 CoT 即风险**：如果用户的某条逻辑与某条 CoT 的结论相反/矛盾，**这本身就是一个潜在风险点**，必须在风险一节明确点出「你的判断 vs CoT 学到的，谁可能错」。
3. **引用要实**：每个判断都落到具体 CoT 编号(C#)+信号强度，或具体同业 note(写公司代码)，禁止泛泛而谈。CoT 自带「证伪条件」「原文依据」，是写风险和反证的弹药。
4. **先校验、后创造**：先逐条审用户的假设；然后**主动提出用户没说、但 CoT/note 支持的新观点**。
5. **横向对比**：用同业 note 提炼可比公司的共性与差异，不要只盯目标公司一家。
6. 没有用户想法时，转为「该股可能命中哪些产业逻辑 + 同业共性 + 主要风险」的扫描。"""

SYNTH_TEMPLATE = """## 目标股票基本面
{snapshot}

## 用户的投资想法（可能为空）
{idea}

## 用户对该公司已有的 note（自己的既有观点，按日期新→旧；勿当同业对比）
{own_notes}
（注：最新一条 = 用户「当前观点」；若多条且立场有变化，在结论里点出「观点演变」。）

## 召回的相关 CoT（行业可复用逻辑，含信号/证伪/原文依据）
{cots}

## 召回的【同业】note（其它可比公司，用于横向对比）
{notes}

---

输出一份 **markdown 校验笔记**，风格简洁、每条逻辑独立成段、善用 ✅/🟡/❌/⚠️ 与表格。结构如下（不要加代码块包裹）：

# 🔍 {ticker} 逻辑校验

## 一句话结论
结论 + 「逻辑整体可信度 X/10」（无用户想法则给「产业逻辑契合度」）。

## 逐条校验（你的逻辑）
把用户想法拆成「逻辑一/二/三…」，每条一段：
- 标题行用 ✅成立 / 🟡部分成立 / ❌存疑 / ⚠️库内无支撑 标注
- 叙述为什么，**引用具体 C# + 信号**或同业 note
- 每条末尾一句「结论：…」
（若无用户想法，本节改为「产业逻辑命中扫描」：该股可能命中哪些 CoT、强弱如何）

## 新增观点（你没提，但知识库支持）
基于 CoT/note 主动补 1-3 条用户没覆盖的角度或驱动，引用来源。

## 横向同业对比
表格：可比公司 | 共同逻辑/护城河 | 与目标的差异 | 来源note。提炼同类公司共性。

## 反逻辑与风险点
- 最强的看空/证伪理由（2-4 条），引用 CoT 证伪条件
- **⚠ 与知识库相悖之处**：用户逻辑里与某条 CoT 结论相反的地方（若有），点明分歧
- 崩溃的可观测信号：什么数据/事件出现就说明逻辑错了

## 综合
一个小表格（逻辑链 | 置信度 | 关键抓手）+ 一句话总结核心观测点。

## 待补充（后续接入）
- [ ] 基本面深析　- [ ] 估值（DCF/PE）　- [ ] 预期分析（可验证可进化）

要求：引用具体到 C#/note 代码；中性克制不堆砌。"""


def _fmt_cots(cots: list, idx: list) -> str:
    out = []
    for i in idx:
        c = cots[i]
        ev = (c.get("evidence") or "").strip()
        out.append(
            f"【C{i}】信号{c.get('signal','?')} "
            f"(传导{c.get('transmission','?')}/证伪{c.get('falsifiability','?')}/历史{c.get('history','?')}/时效{c.get('recency','?')}) "
            f"[{c.get('_sector','')}|{'/'.join(c.get('_tags', []))}]\n"
            f"  逻辑: {c['trigger']}\n"
            f"  传导链: {c['COT']}\n"
            + (f"  原文依据: 「{ev}」\n" if ev else "")
        )
    return "\n".join(out) or "（无相关 CoT）"


def _fmt_notes(notes: list, idx: list, max_chars: int = 1200) -> str:
    out = []
    for i in idx:
        n = notes[i]
        body = re.sub(r"\n{3,}", "\n\n", n["content"]).strip()[:max_chars]
        out.append(f"【note {n['ticker']} {n['created_at']}】\n{body}\n")
    return "\n".join(out) or "（无相关同业 note）"


def vet_stock(ticker: str, idea: str = "", cot_limit: int = 15, note_limit: int = 5,
              save: bool = True) -> dict:
    """主入口。返回 {ticker, markdown, path, recall_reasoning, n_cots, n_notes}。"""
    ticker = ticker.upper().strip()
    model = _model()
    client = make_anthropic_client()

    print(f"[VET] {ticker} — 拉基本面...")
    data = fetch_fundamentals(ticker) or {"ticker": ticker}
    snapshot = _fundamentals_snapshot(data)

    idea_text = load_idea(idea)
    if idea_text:
        print(f"  ✓ 想法输入 {len(idea_text)} 字")

    cots = load_cots()
    all_notes = load_user_notes()
    own_notes = load_user_notes(ticker=ticker)          # 目标公司自己的 note（背景）
    own_paths = {n["path"] for n in own_notes}
    peer_notes = [n for n in all_notes if n["path"] not in own_paths]  # 同业候选（横向对比）
    print(f"  知识库: {len(cots)} 条 CoT / 自有 note {len(own_notes)} 份 / 同业候选 {len(peer_notes)} 份 → 自然语言召回...")

    cot_idx, peer_idx, why = _recall(snapshot, idea_text, cots, peer_notes,
                                     cot_limit, note_limit, model, client)
    print(f"  ✓ 召回 {len(cot_idx)} 条 CoT / {len(peer_idx)} 份同业 note  {('— ' + why) if why else ''}")

    msg = SYNTH_TEMPLATE.format(
        ticker=ticker, snapshot=snapshot,
        idea=idea_text or "（无，做产业逻辑命中扫描）",
        own_notes=_fmt_notes(own_notes, range(len(own_notes))) if own_notes else "（无）",
        cots=_fmt_cots(cots, cot_idx),
        notes=_fmt_notes(peer_notes, peer_idx),
    )
    print(f"  合成校验笔记中...")
    try:
        resp = client.messages.create(model=model, max_tokens=6000, system=SYNTH_SYSTEM,
                                      messages=[{"role": "user", "content": msg}])
        md = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return {"ticker": ticker, "error": f"合成失败: {e}", "markdown": "", "path": None}

    # 去掉可能的代码块包裹
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md).strip()

    path = None
    if save and md:
        out_dir = _output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[\\/:*?\"<>|]", "_", ticker)
        path = out_dir / f"vet_{safe}_{date.today().isoformat()}.md"
        path.write_text(md, encoding="utf-8")

    return {
        "ticker": ticker, "markdown": md, "path": str(path) if path else None,
        "recall_reasoning": why, "n_cots": len(cot_idx), "n_notes": len(peer_idx),
    }
