"""个股研究笔记生成 (fa report) — 愿景第二步的组装层.

三段式结构（2026-06-10 点评后定型）：
  第一部分 逻辑校验 — vet 结果（复用 vet_stock，不重复落盘）
  第二部分 材料与框架分析 — 路由命中专用框架则用框架全文；否则通用 9 维
    （核心论点/业务结构/财务质地/行业地位/护城河/管理层/成长史/风险清单/竞争优势评级）
    ——客观历史材料 + 主观分析
  第三部分 估值与预期分析 — **不分框架，统一**：盈利预测/估值与目标价/催化剂/
    反证复盘信号/跟踪指标+数据源/待人工补查清单；数据来自 fetch_forecast_pack
    （EODHD 历史利润表 + 卖方一致预期 Trend + 目标价评级，缺的进补查清单）

框架路由：注册表即闭合词表——LLM 只能从 memory/framework/frameworks/ 里选，
把握不大回退 "general"。不入 note 库、不注册预测——与 vet 同属合成层。
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from .config import load_config, make_anthropic_client
from .framework import inject_review_rules, list_analysis_frameworks
from .note_template import DIMENSIONS
from .vet import _model, _output_dir, _parse_json, vet_stock


GENERAL_NAME = "general"
GENERAL_TITLE = "通用 9 维材料分析"

# 前段（客观材料+主观分析）9 维；后段（估值与预期）5 维 + 补查清单——id 对齐 note_template
FRONT_GENERAL_IDS = ["core_thesis", "business_breakdown", "financial_quality", "market_position",
                     "moat", "management_governance", "growth_history", "risks", "competitive_rating"]
BACK_IDS = ["financial_forecast", "valuation_target", "catalysts", "falsification", "tracking_metrics"]


def _dims_guide(ids: list[str]) -> str:
    """从 note_template.DIMENSIONS 取维度名+提示（单一事实来源，不另抄一份）。"""
    by_id = {d["id"]: d for d in DIMENSIONS}
    return "\n".join(f"{i}. **{by_id[x]['name']}** — {by_id[x]['hint']}"
                     for i, x in enumerate(ids, 1))


def _general_body() -> str:
    return (
        "按以下 9 维结构逐节分析（客观历史材料 + 主观分析；盈利预测/估值/催化剂不在本部分，后续统一节处理）。\n"
        "行业不适用的维度写「不适用」加一句话原因；库内/快照没有的数据就地标注「待人工补查」，禁止编造。\n\n"
        + _dims_guide(FRONT_GENERAL_IDS)
    )


# ── 路由 ──

ROUTE_SYSTEM = """你是研究主管，为一家公司选择最合适的分析框架。
只能从给定清单的 name 中选一个，或选 "general"（通用 15 维研究模板）。
判据：公司业务属性、市值规模、市场 vs 各框架的适用(applies)/不适用(avoid)条件。
把握不大就选 general——错配的专用框架比通用模板更糟。"""

ROUTE_TEMPLATE = """## 目标公司
{snapshot}

## 用户想法（可能为空）
{idea}

## 可选框架清单
{catalog}

## 输出（严格 JSON，无 markdown 包裹）
{{"framework": "<name 或 general>", "reason": "<一句话：为什么选它/为什么回退通用>"}}
只输出 JSON。"""


def _route(snapshot: str, idea: str, frameworks: list[dict], model: str, client) -> tuple[Optional[dict], str]:
    """返回 (框架 dict 或 None=general, 理由)。路由失败一律回退 general，不中断。"""
    if not frameworks:
        return None, "注册表为空，使用通用模板"
    catalog = "\n".join(
        f"- name: {f['name']}\n  标题: {f['title']}\n  核心: {f['description']}\n"
        f"  适用: {f['applies']}\n  不适用: {f['avoid']}"
        for f in frameworks
    )
    msg = ROUTE_TEMPLATE.format(snapshot=snapshot, idea=idea or "（无）", catalog=catalog)
    try:
        resp = client.messages.create(model=model, max_tokens=2000, system=ROUTE_SYSTEM,
                                      messages=[{"role": "user", "content": msg}])
        text = "".join(b.text for b in resp.content if b.type == "text")
        parsed = _parse_json(text) or {}
    except Exception as e:
        return None, f"路由 LLM 失败({e})，回退通用模板"
    name = str(parsed.get("framework") or "").strip()
    reason = str(parsed.get("reason") or "").strip()
    if name == GENERAL_NAME or not name:
        return None, reason or "通用模板"
    chosen = next((f for f in frameworks if f["name"] == name), None)
    if chosen is None:  # LLM 编了一个不存在的名字 → 闭合词表守门，回退
        return None, f"路由返回未注册框架 '{name}'，回退通用模板"
    return chosen, reason


# ── 框架分析合成 ──

FW_SYNTH_SYSTEM = """你是中性、客观、就事论事的投资分析师。严格按给定的分析框架研究目标公司。

铁律：
1. **框架是硬约束**：透镜/评分标准/硬性原则/输出结构（Phase 4 Generate 或等效章节）都要遵循；
   框架自带的历史教训与假阳性提示要主动对照。
2. **引用要实**：判断落到基本面数字、CoT 编号(C#)、同业 note(公司代码)；
   库内/快照没有的数据写「库内无数据，需人工补查」，禁止编造，缺数据的维度不硬给分。
3. **不抹平分歧**：已完成的逻辑校验（vet）是输入之一，框架分析与 vet 结论冲突时明确点出谁可能错。
4. 中性克制，不预设多空。"""

FW_SYNTH_TEMPLATE = """## 分析框架（全文，严格遵循）
{fw_body}

## 目标公司基本面快照
{snapshot}

## 用户想法（可能为空）
{idea}

## 第一步逻辑校验结论（vet，已完成）
{vet_md}

## 召回的相关 CoT（与 vet 同一批）
{cots_block}

## 召回的同业 note
{notes_block}

---

按框架输出 markdown 分析（不要代码块包裹），首行标题用「# 📐 {ticker} 框架分析 — {fw_title}」。
本部分只做客观材料与逻辑分析；盈利预测/估值/目标价/催化剂不在本部分展开（报告后续有统一的估值与预期分析节）。
缺数据的项**就地标注**「待人工补查」即可，文末不用汇总（统一节会收集全篇）。"""


# ── 后段：估值与预期分析（统一，不分框架） ──

BACK_SYNTH_SYSTEM = """你是中性、客观的投资分析师，做个股报告的「估值与预期分析」统一后段。

铁律：
1. **每个数字有出处**：只能来自「行情与财务数据」块、自有 note 里的研报预测、CoT 引用（C#）或前文分析；
   出处写在数字旁（一致预期/研报note/自推）。库内没有的数据写进「待人工补查清单」，禁止编造。
2. **盈利预测三角验证**：卖方一致预期、note 里的研报预测、你基于逻辑校验的自主修正——三者并列呈现，
   有分歧就写明谁更可信、为什么。
3. **估值给三情景**：base/bull/bear——方法（PE/PS/EV 等按公司属性选）× 对应盈利 = 目标市值，
   对照当前市值算上行/下行空间（%）。亏损公司不硬套 PE，换 PS 或里程碑估值并说明局限。
4. **单位统一按上市地货币**：数据块已折算并注明币种与汇率，输出中所有金额必须带币种后缀，
   严禁混币计算估值倍数（市值与收入/利润必须同币种）；数据块标了 ⚠ 未折算的，先换算再算倍数。
5. 中性克制：催化剂与反证并重，反证条件必须可观测。"""

BACK_SYNTH_TEMPLATE = """## 行情与财务数据
{fin_block}

## 目标公司自有 note（可能含研报盈利预测，按日期新→旧）
{own_notes}

## 第一部分 逻辑校验结论（vet）
{vet_md}

## 第二部分 材料与框架分析
{front_md}

## 召回的相关 CoT（催化剂/证伪弹药）
{cots_block}

---

输出 markdown（不要代码块包裹），首行标题「# 💰 {ticker} 估值与预期分析」，依次六节：

## 盈利预测
{forecast_hint}。分年度表格：历史 2-3 年实际 + 未来 2-3 年预测，每个预测数字注明来源（一致预期/研报note/自推），有分部数据就分部列。

## 估值与目标价
{valuation_hint}。base/bull/bear 三情景表格 + 对照当前市值的上行/下行空间；写明估值方法选择理由。

## 催化剂 / 关键时点
{catalysts_hint}

## 反证 / 复盘信号
{falsification_hint}

## 跟踪指标 + 数据源
{tracking_hint}

## 待人工补查清单
汇总全篇（含第一、二部分就地标注的）缺失数据项，每条写清要查什么、去哪查（年报/公告/期权链/做空数据等）；没有就写「无」。"""


def _yi(v) -> Optional[float]:
    """原始金额 → 亿（保留 2 位）。"""
    return round(v / 1e8, 2) if isinstance(v, (int, float)) else None


def _forecast_block(pack: Optional[dict], fund: dict) -> str:
    """把 fetch_forecast_pack + 基本面快照拼成后段的数据块。缺什么明说，不留空白。

    单位纪律：一律按**上市地货币**统一（财报原币不同则已在数据层按汇率折算）。
    市值以上市地行情源为准（fund，港股=东财；EODHD 仅作交叉核对，分歧>15% 明示预警）。
    """
    pack = pack or {}
    cur = pack.get("currency") or fund.get("currency") or ""
    mcap_local = fund.get("market_cap")
    mcap_eodhd = pack.get("market_cap")
    mcap = mcap_local or mcap_eodhd
    head = [f"当前市值: {_yi(mcap)} 亿{cur}" if mcap else "当前市值: 未知（待人工补查）"]
    for label, key in (("PE", "pe"), ("PB", "pb"), ("股息率%", "div_yield")):
        v = fund.get(key)
        if v is not None:
            head.append(f"{label}={round(v, 2) if isinstance(v, float) else v}")
    if pack.get("forward_pe"):
        head.append(f"ForwardPE={pack['forward_pe']}")
    lines = ["  ".join(head)]
    if mcap_local and mcap_eodhd and max(mcap_local, mcap_eodhd) / min(mcap_local, mcap_eodhd) > 1.15:
        lines.append(f"⚠ 市值两源分歧：上市地行情源 {_yi(mcap_local)} 亿 vs EODHD {_yi(mcap_eodhd)} 亿"
                     f"（差异多来自股本口径），以上市地源为准；股本口径列入待人工补查")

    # 单位说明：折算过就写明原币与汇率；财报币种不同但折算失败 → 显式预警
    fin_cur = pack.get("fin_currency") or cur
    if pack.get("converted"):
        unit_note = f"单位：亿{cur}（财报原币 {fin_cur}，已按汇率 {pack['fx_rate']:.4f} 折算到上市地货币）"
    elif fin_cur and cur and fin_cur != cur:
        unit_note = (f"⚠ 单位：亿{fin_cur}（财报原币）——注意与市值（{cur}）不同币种，"
                     f"汇率获取失败未折算，估值倍数计算前必须先换算；列入待人工补查")
    else:
        unit_note = f"单位：亿{cur}"

    if pack.get("income_hist"):
        lines.append(f"\n历史利润表（{unit_note}）：")
        lines.append("| 年度 | 收入 | 毛利 | 营业利润 | 净利润 |")
        lines.append("|---|---|---|---|---|")
        for h in pack["income_hist"]:
            lines.append(f"| {h['date'][:4]} | {_yi(h['revenue'])} | {_yi(h['gross_profit'])} "
                         f"| {_yi(h['operating_income'])} | {_yi(h['net_income'])} |")
    else:
        lines.append("\n历史利润表：EODHD 无数据（待人工补查：最新年报/中报）")

    if pack.get("est_trend"):
        lines.append(f"\n卖方一致预期（EODHD Earnings Trend，{unit_note}，EPS 为每股）：")
        lines.append("| 期末 | 收入预估 | EPS预估 | 分析师数 |")
        lines.append("|---|---|---|---|")
        for t in pack["est_trend"]:
            lines.append(f"| {t['period']} | {_yi(t['revenue_avg'])} | {t['eps_avg']} | {t['analysts'] or '?'} |")
    else:
        lines.append("\n卖方一致预期：无（待人工补查：券商研报盈利预测）")

    if pack.get("target_price"):
        lines.append(f"\n华尔街目标价: {pack['target_price']} {cur}  评级分布: {pack.get('rating') or '无'}")
    lines.append("\n分业务收入拆分：EODHD 不提供（待人工补查：年报分部报告）")
    return "\n".join(lines)


def build_report(ticker: str, idea: str = "", framework: str = "",
                 cot_limit: int = 15, note_limit: int = 5, save: bool = True) -> dict:
    """主入口。framework 非空则强制指定（"general" = 直接用通用模板），否则 LLM 路由。

    返回 {ticker, markdown, path, framework, route_reason} 或 {error}。
    """
    model = _model()
    client = make_anthropic_client()

    # 1. 第一步：逻辑校验（不单独落盘，结果并入报告）
    print(f"[REPORT] {ticker} — 第一步：逻辑校验 (vet)")
    vet = vet_stock(ticker, idea=idea, cot_limit=cot_limit, note_limit=note_limit,
                    save=False, with_todo=False)
    if vet.get("error"):
        return {"error": f"vet 失败: {vet['error']}"}
    ticker = vet["ticker"]

    # 2. 框架路由
    frameworks = list_analysis_frameworks()
    if framework:
        if framework == GENERAL_NAME:
            chosen, why = None, "用户指定通用模板"
        else:
            chosen = next((f for f in frameworks if f["name"] == framework), None)
            if chosen is None:
                names = ", ".join(f["name"] for f in frameworks) or "（注册表为空）"
                return {"error": f"框架 '{framework}' 未注册。可选: {names}, general"}
            why = "用户指定"
    else:
        print(f"  第二步：框架路由（{len(frameworks)} 个已注册 + general 保底）...")
        chosen, why = _route(vet["_snapshot"], vet.get("_idea_text", ""), frameworks, model, client)
    fw_title = chosen["title"] if chosen else GENERAL_TITLE
    fw_name = chosen["name"] if chosen else GENERAL_NAME
    fw_body = chosen["body"] if chosen else _general_body()
    print(f"  ✓ 路由 → {fw_title}  {('— ' + why) if why else ''}")

    # 3. 前段：材料与框架分析合成
    msg = FW_SYNTH_TEMPLATE.format(
        fw_body=fw_body, snapshot=vet["_snapshot"],
        idea=vet.get("_idea_text") or "（无）", vet_md=vet["markdown"],
        cots_block=vet["_cots_block"], notes_block=vet["_notes_block"],
        ticker=ticker, fw_title=fw_title,
    )
    print("  第三步：材料与框架分析合成中...")
    try:
        resp = client.messages.create(model=model, max_tokens=8000,
                                      system=inject_review_rules(FW_SYNTH_SYSTEM),
                                      messages=[{"role": "user", "content": msg}])
        front_md = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return {"error": f"框架分析合成失败: {e}"}
    front_md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", front_md).strip()

    # 4. 后段：估值与预期分析（统一，不分框架）
    print("  第四步：拉预测数据（EODHD 历史利润表 + 一致预期）...")
    from .tools.data import fetch_forecast_pack
    pack = fetch_forecast_pack(ticker)
    fin_block = _forecast_block(pack, vet.get("_fund") or {})
    hints = {d["id"]: d["hint"] for d in DIMENSIONS}
    msg = BACK_SYNTH_TEMPLATE.format(
        fin_block=fin_block,
        own_notes=vet.get("_own_notes_block") or "（无）",
        vet_md=vet["markdown"], front_md=front_md,
        cots_block=vet["_cots_block"], ticker=ticker,
        forecast_hint=hints["financial_forecast"],
        valuation_hint=hints["valuation_target"],
        catalysts_hint=hints["catalysts"],
        falsification_hint=hints["falsification"],
        tracking_hint=hints["tracking_metrics"],
    )
    print("  第五步：估值与预期分析合成中...")
    try:
        resp = client.messages.create(model=model, max_tokens=8000,
                                      system=inject_review_rules(BACK_SYNTH_SYSTEM),
                                      messages=[{"role": "user", "content": msg}])
        back_md = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return {"error": f"估值与预期分析合成失败: {e}"}
    back_md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", back_md).strip()

    # 5. 拼全文（各部分降一级标题，挂在三个部分下面）
    # 「待补充」已从源头关掉（with_todo=False）；这里留兜底正则，标题容忍 emoji/装饰前缀
    vet_md = re.sub(r"\n#{1,4}[^\n#]*待补充.*?(?=\n#{1,2} |\Z)", "", vet["markdown"], flags=re.DOTALL)
    name = vet.get("_name") or ""
    doc_title = f"{name} ({ticker}) 研究笔记" if name else f"{ticker} 研究笔记"
    meta = (f"{date.today().isoformat()} · 框架: {fw_title} · 召回 CoT {vet['n_cots']} 条 / "
            f"同业 note {vet['n_notes']} 份 · 预测数据: {'EODHD' if pack else '无(待补查)'} · fa report 生成")
    full_md = "\n\n".join([
        "# 第一部分 逻辑校验（知识库 vet）",
        _demote_headings(vet_md),
        f"# 第二部分 材料与框架分析（{fw_title}）",
        _demote_headings(front_md),
        "# 第三部分 估值与预期分析",
        _demote_headings(back_md),
    ])

    path = None
    if save:
        out_dir = _output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[\\/:*?\"<>|]", "_", ticker)
        path = out_dir / f"report_{safe}_{date.today().isoformat()}.docx"
        from .docx_render import md_to_docx
        md_to_docx(full_md, path, title=doc_title, meta=meta)

    return {
        "ticker": ticker, "markdown": full_md, "path": str(path) if path else None,
        "framework": fw_name, "route_reason": why,
    }


def _demote_headings(md: str) -> str:
    """全部标题降一级（# → ##），让 vet/框架输出挂在「第一/二部分」下。"""
    return re.sub(r"^(#{1,5})\s", r"#\1 ", md, flags=re.MULTILINE)
