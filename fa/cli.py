"""CLI v2 — fa 命令.

用法:
  fa scan "固态电池"                # 板块横向扫描
  fa scan "半导体" -l 10            # 限制数量
  fa deep 300750.SHE                # 个股深度分析
  fa review                         # 回顾到期论点 (90天)
  fa review -d 180                  # 回顾180天前的
  fa evolve                         # 进化分析 + 框架建议
  fa dash                           # 仪表盘
  fa sectors                        # 已知板块
  fa status                         # 系统状态
  fa init                           # 初始化
"""

# ── SSL 修复 (必须在所有 import 之前) ──
import os as _os
try:
    import certifi as _certifi
    _os.environ["SSL_CERT_FILE"] = _certifi.where()
except ImportError:
    pass

import argparse
import sys
from pathlib import Path

# ── Windows 后台运行时 stdout/stderr 默认 GBK，强制 utf-8 ──
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from .config import load_config
from .tools.sector import find_sector_peers, list_sectors
from .memory import MemoryStore, PerformanceTracker


store = MemoryStore()
performance = PerformanceTracker(store)


def main():
    parser = argparse.ArgumentParser(prog="fa", description="基本面研究Agent v2 — 三层记忆 + 预测追踪 + 自主进化")
    sub = parser.add_subparsers(dest="cmd")

    # scan
    ps = sub.add_parser("scan", help="板块横向扫描")
    ps.add_argument("topic", help="板块/主题名称")
    ps.add_argument("-l", "--limit", type=int, default=10)
    ps.add_argument("-o", "--output", help="输出路径")
    ps.add_argument("--tickers", nargs="*", help="指定成分股")

    # deep
    pd = sub.add_parser("deep", help="个股深度分析")
    pd.add_argument("ticker", help="股票代码")

    # review
    pr = sub.add_parser("review", help="定期回顾")
    pr.add_argument("-d", "--days", type=int, default=90)
    pr.add_argument("--no-critic", action="store_true", help="跳过 Critic LLM 评审（省 API 费）")
    pr.add_argument("--no-reflector", action="store_true",
                    help="跳过 Reflector 反思（不沉淀新情境笔记）")

    # evolve
    pe = sub.add_parser("evolve", help="进化分析")
    pe.add_argument("--apply", type=int, help="执行指定编号的框架更新建议")

    # critique
    pc = sub.add_parser("critique", help="查看某只股票最近一次 Critic 评审")
    pc.add_argument("ticker", help="股票代码")
    pc.add_argument("--rerun", action="store_true", help="重新触发 Critic 评审（消耗 API）")

    # reflect (P1) — 手动触发反思
    prf = sub.add_parser("reflect", help="对某只股票最近一次回顾跑 Reflector 反思（产出情境笔记）")
    prf.add_argument("ticker", help="股票代码")
    prf.add_argument("--force", action="store_true",
                     help="强制反思，绕过 should_reflect 阈值检查")

    # ingest (P0)
    pi = sub.add_parser("ingest", help="摄入外部文档 (PDF/DOCX/XLSX/PPTX) → 提炼 CoT")
    pi.add_argument("path", help="文件路径，或 --batch 时为目录")
    pi.add_argument("--ticker", help="绑定个股 (例: 2513.HK)")
    pi.add_argument("--sector", help="绑定板块 (例: AI/半导体)")
    pi.add_argument("--batch", action="store_true", help="批量摄入目录下所有支持格式的文件")
    pi.add_argument("--no-cot", action="store_true", help="只抽文，不调用 LLM 提炼 CoT")

    # note (P0)
    pn = sub.add_parser("note", help="录入用户论点 (4 维度: 论点/护城河/反证/时间+仓位)")
    pn.add_argument("ticker", help="股票代码")
    pn.add_argument("-m", "--message", help="一句话快录")
    pn.add_argument("-f", "--file", help="从 md 文件导入")
    pn.add_argument("--sector", help="所属板块（可选，辅助召回过滤）")

    # notes
    pln = sub.add_parser("notes", help="列出用户论点")
    pln.add_argument("ticker", nargs="?", help="可选：只列某只票")

    # cot (P2) — CoT 选股工具
    pcot = sub.add_parser("cot", help="CoT 工具：list / score / vote")
    cotsub = pcot.add_subparsers(dest="cot_cmd")

    pcot_l = cotsub.add_parser("list", help="列出已有 CoT")
    pcot_l.add_argument("--sector", help="按板块过滤")
    pcot_l.add_argument("--min-signal", type=int, default=0, help="只列信号 >= N 的 CoT")

    pcot_s = cotsub.add_parser("score", help="用相关 CoT 对单只股票打分")
    pcot_s.add_argument("ticker", help="股票代码")
    pcot_s.add_argument("--sector", help="覆盖股票自身的 sector，强制用某板块的 CoT")
    pcot_s.add_argument("--min-signal", type=int, default=7, help="只用信号 >= N 的 CoT (默认 7)")
    pcot_s.add_argument("--limit", type=int, default=15, help="最多用多少条 CoT (默认 15，省 API)")

    pcot_v = cotsub.add_parser("vote", help="多股票联合投票 → 出持仓清单")
    pcot_v.add_argument("tickers", nargs="+", help="股票代码列表")
    pcot_v.add_argument("--sector", help="用哪个板块的 CoT 投票")
    pcot_v.add_argument("--min-signal", type=int, default=7)
    pcot_v.add_argument("--min-votes", type=int, default=3, help="进入持仓的最低票数")

    # dashboard
    sub.add_parser("dash", help="仪表盘")

    # sectors
    sub.add_parser("sectors", help="已知板块")

    # status
    sub.add_parser("status", help="系统状态")

    # init
    sub.add_parser("init", help="初始化项目")

    # config
    sub.add_parser("config", help="当前配置")

    args = parser.parse_args()

    if args.cmd == "scan":
        _cmd_scan(args)
    elif args.cmd == "deep":
        _cmd_deep(args)
    elif args.cmd == "review":
        _cmd_review(args)
    elif args.cmd == "evolve":
        _cmd_evolve(args)
    elif args.cmd == "critique":
        _cmd_critique(args)
    elif args.cmd == "reflect":
        _cmd_reflect(args)
    elif args.cmd == "ingest":
        _cmd_ingest(args)
    elif args.cmd == "note":
        _cmd_note(args)
    elif args.cmd == "notes":
        _cmd_notes(args)
    elif args.cmd == "cot":
        _cmd_cot(args)
    elif args.cmd == "dash":
        _cmd_dash()
    elif args.cmd == "sectors":
        _cmd_sectors()
    elif args.cmd == "status":
        _cmd_status()
    elif args.cmd == "init":
        _cmd_init()
    elif args.cmd == "config":
        _cmd_config()
    else:
        parser.print_help()


def _cmd_scan(args):
    from .agent import do_scan
    tickers = args.tickers or find_sector_peers(args.topic)
    if tickers:
        tickers = tickers[:args.limit]
        print(f"[SCAN] {args.topic} — {len(tickers)} 只")
    else:
        print(f"[SCAN] {args.topic} — Agent 将自行搜索成分股")
    do_scan(args.topic, tickers, args.output)


def _cmd_deep(args):
    from .agent import do_deep
    print(f"[DEEP] {args.ticker}")
    do_deep(args.ticker)


def _cmd_review(args):
    from .agent import do_review
    do_review(args.days, with_critic=not args.no_critic,
              with_reflector=not args.no_reflector)


def _cmd_evolve(args):
    from .agent import do_evolve
    do_evolve(apply_index=args.apply)


def _cmd_critique(args):
    """查看/重跑 Critic 评审。"""
    import json as _json
    ticker = args.ticker

    if args.rerun:
        # 重新触发评审：拉数据 + 验证预测 + 评估 + Critic
        from .agent import critic, performance, predictions, store as _store
        from .tools.data import fetch_fundamentals

        thesis = _store.get_thesis(ticker)
        if not thesis:
            print(f"[CRITIQUE] {ticker} 无活跃论点")
            return
        if not thesis.get("baseline_price"):
            print(f"[CRITIQUE] {ticker} 缺少基线，请先 store.backfill_baseline()")
            return

        data = fetch_fundamentals(ticker, with_benchmarks=False)
        results = predictions.verify(ticker, thesis["id"], data) if data else []
        correct_n = sum(1 for r in results if r["result"] == "正确")
        partial_n = sum(1 for r in results if r["result"] == "部分正确")
        total_n = len(results)
        subjective = round((correct_n + 0.5 * partial_n) / total_n, 3) if total_n > 0 else None

        perf = performance.evaluate(ticker, subjective_score=subjective)
        if not perf or "error" in perf:
            print(f"[CRITIQUE] 无法评估: {perf.get('error') if perf else '未知'}")
            return

        print(f"[CRITIQUE] 调用 Critic LLM... ({ticker})")
        critic_out = critic.critique(thesis, perf, results, current_fundamentals=data)
        performance.attach_critic(perf["performance_id"], critic_out)
        _render_critique(ticker, perf, critic_out)
        return

    # 默认：读最近一次评审
    rows = performance.get_history(ticker)
    if not rows:
        print(f"[CRITIQUE] {ticker} 无评审记录。使用 --rerun 触发首次评审。")
        return
    latest = rows[0]
    critic_out = {
        "critic_score": latest.get("critic_score"),
        "raw_llm_score": latest.get("raw_llm_score"),
        "final_score": latest.get("final_score"),
        "what_worked": latest.get("what_worked") or "",
        "what_failed": latest.get("what_failed") or "",
        "improvement_hints": _json.loads(latest.get("improvement_hints") or "[]"),
        "critique": latest.get("critique") or "",
        "anchor_adjusted": False,
    }
    _render_critique(ticker, latest, critic_out)


def _render_critique(ticker: str, perf: dict, critic_out: dict):
    print(f"\n=== {ticker} Critic 评审 ===")
    print(f"  评估日:    {perf.get('checkpoint_date')}")
    print(f"  持仓:      {perf.get('days_held')} 天")
    print(f"  股票收益:  {perf.get('stock_return'):+.2f}%")
    print(f"  基准收益:  {perf.get('index_return'):+.2f}%")
    print(f"  超额收益:  {perf.get('excess_return'):+.2f}%")
    print(f"")
    print(f"  客观分:    {perf.get('objective_score')}")
    print(f"  Critic 分: {critic_out.get('critic_score')} "
          f"(原始 LLM: {critic_out.get('raw_llm_score')})")
    print(f"  最终分:    {critic_out.get('final_score')}")
    print(f"")
    if critic_out.get("what_worked"):
        print(f"  ✓ 哪里对了: {critic_out['what_worked']}")
    if critic_out.get("what_failed"):
        print(f"  ✗ 哪里错了: {critic_out['what_failed']}")
    if critic_out.get("improvement_hints"):
        print(f"\n  改进建议:")
        for h in critic_out["improvement_hints"]:
            print(f"    - {h}")
    if critic_out.get("critique"):
        print(f"\n  完整评审:\n  {critic_out['critique']}")


def _cmd_reflect(args):
    """对某只股票最近一次回顾跑 Reflector，独立于 fa review."""
    import json as _json
    from .agent import (reflector, performance, store as _store, situations,
                        _apply_reflection)
    from .tools.data import fetch_fundamentals

    ticker = args.ticker
    thesis = _store.get_thesis(ticker)
    if not thesis:
        print(f"[REFLECT] {ticker} 无活跃论点")
        return

    # 拉最近一次 performance 记录
    history = performance.get_history(ticker)
    if not history:
        print(f"[REFLECT] {ticker} 无 performance 记录，先跑 fa review")
        return
    latest = history[0]
    perf = dict(latest)

    # critic_out 从 latest 重建
    critic_out = {
        "critic_score": latest.get("critic_score"),
        "raw_llm_score": latest.get("raw_llm_score"),
        "final_score": latest.get("final_score"),
        "what_worked": latest.get("what_worked") or "",
        "what_failed": latest.get("what_failed") or "",
        "improvement_hints": _json.loads(latest.get("improvement_hints") or "[]"),
        "critique": latest.get("critique") or "",
    }

    if not args.force:
        should, why = reflector.should_reflect(perf, critic_out)
        if not should:
            print(f"[REFLECT] 跳过 ({why})。用 --force 强制反思")
            return

    # 重新拉 prediction_results
    reviews = _store.get_reviews(ticker)
    pred_results = []
    if reviews:
        try:
            pred_results = _json.loads(reviews[0].get("prediction_results") or "[]")
        except Exception:
            pred_results = []

    data = fetch_fundamentals(ticker, with_benchmarks=False)

    print(f"[REFLECT] 调用 Reflector LLM... ({ticker})")
    reflection = reflector.reflect(thesis, perf, pred_results, critic_out,
                                   current_fundamentals=data)
    diag = reflection.get("diagnosis", {})
    cands = reflection.get("candidate_notes", [])

    if reflection.get("error"):
        print(f"[REFLECT] {reflection['error']}")
        return

    if diag.get("root_cause"):
        print(f"\n=== {ticker} Reflector 诊断 ===")
        print(f"  root_cause:   {diag['root_cause']}")
        print(f"  pattern_type: {diag.get('pattern_type', '?')}")
        if diag.get("generalization_reason"):
            print(f"  泛化说明:     {diag['generalization_reason']}")

    if not cands:
        print(f"\n[REFLECT] 无可泛化经验，未产出笔记")
        return

    print(f"\n[REFLECT] 产出 {len(cands)} 条候选笔记，进入冲突仲裁...")
    stats = _apply_reflection(cands, ticker, excess=perf.get("excess_return"))
    print(f"[REFLECT] 笔记落盘: add={stats['add']} skip={stats['skip']} "
          f"replace={stats['replace']} branch={stats['branch']}")


def _cmd_ingest(args):
    """摄入外部文档 → 抽文 → (可选) LLM 提炼 CoT → 入库."""
    from pathlib import Path
    from .ingest import ingest_file, SUPPORTED_EXT, extract_cot
    from .ingest.cot_extractor import save_cot_file

    target = Path(args.path).expanduser().resolve()

    if args.batch:
        if not target.is_dir():
            print(f"[INGEST] --batch 要求目录，得到文件: {target}")
            return
        files = sorted([p for p in target.rglob("*") if p.suffix.lower() in SUPPORTED_EXT])
        print(f"[INGEST] 扫到 {len(files)} 个文件: {target}")
    else:
        if not target.exists():
            print(f"[INGEST] 文件不存在: {target}")
            return
        files = [target]

    for f in files:
        _ingest_one(f, args.ticker, args.sector, with_cot=not args.no_cot)


def _ingest_one(fpath, ticker, sector, with_cot=True):
    """摄入单文件。"""
    from .ingest import ingest_file
    from .ingest.cot_extractor import extract_cot, save_cot_file

    print(f"\n[INGEST] {fpath.name}")
    try:
        doc = ingest_file(fpath)
    except Exception as e:
        print(f"  ✗ 抽文失败: {e}")
        return

    print(f"  ✓ 抽文成功: {len(doc['text'])} 字 / {doc['pages']} 页 / hash={doc['hash']}")

    # 去重逻辑：已有 CoT 才跳过；之前 --no-cot 进过的允许补 CoT
    existing = [r for r in store.list_ingested(limit=10000) if r["file_hash"] == doc["hash"]]
    if existing and existing[0].get("cot_count", 0) > 0 and with_cot:
        print(f"  ⚠ 已摄入并提炼过 {existing[0]['cot_count']} 条 CoT，跳过")
        return

    cot_count = 0
    cot_file_rel = None
    if with_cot:
        print(f"  [LLM] 提炼 CoT 中...")
        cots = extract_cot(doc["text"])
        cot_count = len(cots)
        if cot_count > 0:
            cot_path = save_cot_file(cots, ticker, sector, doc["filename"], doc["hash"])
            cot_file_rel = str(cot_path.relative_to(cot_path.parents[3]))  # AI-Finance/memory/...
            print(f"  ✓ 提炼 {cot_count} 条 CoT → {cot_file_rel}")
            # 展示前 3 条
            for i, c in enumerate(cots[:3], 1):
                print(f"    {i}. [{c['signal']}/10] {c['trigger']}")
            if cot_count > 3:
                print(f"    ... 还有 {cot_count - 3} 条")
        else:
            print(f"  ⚠ 未能提炼出 CoT")

    store.save_ingested_doc(
        source_path=doc["path"], filename=doc["filename"],
        file_type=doc["ext"], file_hash=doc["hash"],
        ticker=ticker, sector=sector, pages=doc["pages"],
        cot_count=cot_count, cot_file=cot_file_rel,
    )


def _cmd_note(args):
    """用户论点录入（4 维度结构化 + 自由文本兜底）."""
    from pathlib import Path
    from .ingest.user_note import save_user_note, interactive_prompt, DIMENSIONS

    ticker = args.ticker.upper()
    raw_text = ""
    structured = {k: "" for k, _ in DIMENSIONS}

    if args.file:
        p = Path(args.file).expanduser().resolve()
        if not p.exists():
            print(f"[NOTE] 文件不存在: {p}")
            return
        raw_text = p.read_text(encoding="utf-8-sig")  # 兼容 PowerShell 写的 BOM
        print(f"[NOTE] 从文件读入: {p.name} ({len(raw_text)} 字)")
    elif args.message:
        raw_text = args.message
    else:
        # 交互
        structured = interactive_prompt()
        if not any(structured.values()):
            print("[NOTE] 所有维度都为空，已取消")
            return

    try:
        path = save_user_note(
            ticker=ticker,
            **structured,
            raw_text=raw_text,
            sector=args.sector,
        )
        print(f"[NOTE] ✓ 已保存 → {path}")
    except ValueError as e:
        print(f"[NOTE] {e}")


def _cmd_notes(args):
    """列出用户论点."""
    from .ingest.user_note import load_user_notes

    notes = load_user_notes(args.ticker)
    if not notes:
        print(f"[NOTES] 无记录{f' (ticker={args.ticker})' if args.ticker else ''}")
        return

    print(f"\n=== 用户论点 ({len(notes)} 条) ===\n")
    for n in notes:
        # 第一行 frontmatter 之后的标题/摘要
        body = n["content"].split("---", 2)[-1].strip()
        summary = body.split("\n")[0:6]
        print(f"[{n['created_at']}] {n['ticker']}")
        print(f"  {n['path']}")
        for line in summary:
            if line.strip():
                print(f"  {line[:100]}")
        print()


def _cmd_cot(args):
    """fa cot list / score / vote 子命令分发."""
    if not args.cot_cmd:
        print("用法: fa cot {list|score|vote} ...\n输入 fa cot --help 查看子命令")
        return

    from .cot import load_cots, CotScorer, vote, weighted_vote, score_all_cots
    from .tools.data import fetch_fundamentals

    if args.cot_cmd == "list":
        cots = load_cots(sector=args.sector, min_signal=args.min_signal)
        if not cots:
            print(f"[COT] 无符合条件的 CoT (sector={args.sector}, min_signal={args.min_signal})")
            return
        print(f"\n=== CoT 列表 ({len(cots)} 条) ===\n")
        for c in cots:
            print(f"  [{c['signal']}/10] {c['trigger']}")
            print(f"    {c['COT'][:140]}")
            print(f"    src={c['_source']} | sector={c['_sector']} | id={c['_cot_id']}")
            print()
        return

    if args.cot_cmd == "score":
        ticker = args.ticker.upper()
        print(f"[COT-SCORE] {ticker}")
        data = fetch_fundamentals(ticker)
        if not data:
            print(f"  ✗ 无法获取 {ticker} 数据")
            return
        sector = args.sector or data.get("sector")
        print(f"  行业: {sector or '未知'}")

        cots = load_cots(sector=args.sector, min_signal=args.min_signal)
        if not cots:
            # fallback: 拉全部 CoT
            cots = load_cots(min_signal=args.min_signal)
            print(f"  [INFO] 板块 {sector} 无 CoT，回退到全库 ({len(cots)} 条)")
        if not cots:
            print(f"  ✗ 无可用 CoT，请先 fa ingest 研报")
            return

        # 限制条数省 API
        if len(cots) > args.limit:
            cots = sorted(cots, key=lambda c: -int(c.get("signal", "5")))[:args.limit]
            print(f"  [LIMIT] 取信号最高的 {args.limit} 条")

        scorer = CotScorer()
        def cb(i, n, s):
            print(f"  [{i}/{n}] {s['match']:5s} ({s['confidence']:3d}%) {s['_trigger'][:50]}")
        scores = score_all_cots(cots, data, scorer=scorer, progress_callback=cb)

        # 输出汇总
        print(f"\n--- 投票（等权） ---")
        v = vote(scores, min_votes=3, min_confidence=60)
        print(f"  得票: {v['votes']}/{v['total_cots']} → 决策: {v['decision']}")
        for voter in v["voters"]:
            print(f"    ✓ [{voter['match']}] {voter['trigger']}")

        print(f"\n--- 加权综合 ---")
        wv = weighted_vote(scores)
        print(f"  综合分: {wv['total_score']} (阈值 {wv['min_score']}) → 决策: {wv['decision']}")
        for ctr in wv["contributors"][:5]:
            print(f"    [{ctr['signal']}/10|{ctr['match']}|{ctr['confidence']}%] {ctr['trigger']}")
        return

    if args.cot_cmd == "vote":
        cots = load_cots(sector=args.sector, min_signal=args.min_signal)
        if not cots:
            print(f"[COT-VOTE] 无符合条件的 CoT")
            return
        print(f"[COT-VOTE] 用 {len(cots)} 条 CoT 投票，共 {len(args.tickers)} 只股票")

        scorer = CotScorer()
        results = []
        for ticker in args.tickers:
            t = ticker.upper()
            print(f"\n--- {t} ---")
            data = fetch_fundamentals(t)
            if not data:
                print(f"  ✗ 无数据，跳过")
                continue
            def cb(i, n, s):
                marker = "✓" if s["match"] == "完全符合" else ("△" if s["match"] == "较符合" else "·")
                print(f"  {marker} [{i}/{n}] {s['match']:5s} {s['_trigger'][:40]}")
            scores = score_all_cots(cots, data, scorer=scorer, progress_callback=cb)
            v = vote(scores, min_votes=args.min_votes)
            wv = weighted_vote(scores)
            print(f"  得票 {v['votes']}/{v['total_cots']} | 加权 {wv['total_score']} | {v['decision']}")
            results.append({"ticker": t, "votes": v["votes"],
                            "score": wv["total_score"], "decision": v["decision"]})

        # 汇总持仓清单
        results.sort(key=lambda r: (-r["votes"], -r["score"]))
        print(f"\n{'='*60}")
        print(f"持仓清单（按得票排序）:")
        for r in results:
            mark = "★" if r["decision"] == "纳入持仓" else ("·" if r["decision"] == "观察" else "✗")
            print(f"  {mark} {r['ticker']:12s} votes={r['votes']:2d} score={r['score']:.2f} {r['decision']}")
        return


def _cmd_dash():
    dash = store.dashboard()
    perf = performance.summary()

    print("\n=== 基本面研究Agent 仪表盘 ===\n")
    print(f"  活跃论点:     {dash['active_theses']} 只")
    print(f"  待回顾:       {dash['reviews_due']} 只")
    print(f"  板块知识:     {dash['sectors_known']} 个")
    print(f"  沉淀模式:     {dash['patterns_found']} 个")
    print(f"  最近回顾:     {dash['last_review']}")

    print("\n--- 主观评分（预测验证）---")
    print(f"  预测准确率:   {dash['prediction_accuracy']}")

    print("\n--- 客观评分（vs 大盘超额）---")
    if perf["total"] == 0:
        print(f"  尚无评估记录。运行 fa review 触发首次评估。")
    else:
        win_rate = perf["win_rate"]
        avg_ex = perf["avg_excess"]
        print(f"  评估论点:     {perf['total']} 只 (其中 {perf['wins']} 只跑赢)")
        print(f"  客观胜率:     {win_rate}%")
        print(f"  平均超额:     {avg_ex:+.2f}%")
        print(f"  平均客观分:   {perf['avg_objective_score']}")
        print(f"  最佳:         {perf['best']['ticker']} ({perf['best']['excess']:+.2f}%)")
        print(f"  最差:         {perf['worst']['ticker']} ({perf['worst']['excess']:+.2f}%)")
    print()


def _cmd_sectors():
    print("已知板块 (预设):")
    for s in list_sectors():
        peers = find_sector_peers(s)
        print(f"  {s:12s} ({len(peers)} 只)")

    stored = store.list_sectors()
    if stored:
        print("\n已分析板块 (含知识库):")
        for s in stored:
            print(f"  {s['sector']:12s} (最近扫描: {s['last_scan_at'][:10]})")


def _cmd_status():
    dash = store.dashboard()
    cfg = load_config()
    print("\n=== 系统状态 ===\n")
    print(f"  模型:         {cfg.get('agent', {}).get('model', '?')}")
    print(f"  API Key:       {'已设置' if 'ANTHROPIC_API_KEY' in _os.environ else '⚠ 未设置'}")
    print(f"  EODHD Key:     {'已设置' if 'EODHD_API_KEY' in _os.environ else '⚠ 未设置'}")
    print(f"  数据库:        {store.db_path}")
    print(f"  活跃论点:      {dash['active_theses']}")
    print(f"  预测准确率:    {dash['prediction_accuracy']}")
    print(f"  最近回顾:      {dash['last_review']}")


def _cmd_init():
    import shutil

    project_dir = Path(__file__).resolve().parent.parent

    # 创建目录
    for d in ["framework", "knowledge/sectors", "knowledge/patterns",
              "episodic/theses", "episodic/scans", "episodic/reviews",
              "cache", "benchmarks"]:
        (project_dir / "memory" / d).mkdir(parents=True, exist_ok=True)

    # config
    cfg = project_dir / "config.toml"
    if not cfg.exists():
        src = project_dir / "config.toml.example"
        if src.exists():
            shutil.copy(src, cfg)
            print("[INIT] 已创建 config.toml")

    # .env
    env = project_dir / ".env"
    if not env.exists():
        print("[INIT] 请创建 .env: cp .env.example .env")

    # 初始框架
    _write_initial_framework()
    print(f"[INIT] 初始化完成 → {project_dir}")


def _write_initial_framework():
    framework_dir = Path(__file__).resolve().parent.parent / "memory" / "framework"
    framework_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "checklist.md": CHECKLIST_V2,
        "red-flags.md": REDFLAGS_V2,
        "valuation.md": VALUATION_V2,
    }
    for name, content in files.items():
        path = framework_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            print(f"[INIT] 框架: {name}")


def _cmd_config():
    cfg = load_config()
    import json
    print(json.dumps(cfg, ensure_ascii=False, indent=2, default=str))


# ── 框架内容 v2 ──
CHECKLIST_V2 = """# 商业模式质量检查清单

> 每次分析必须覆盖以下维度。不是打分，是逐项写出判断依据。
> 每个维度的核心问题：为什么这家公司能持续赚到超额利润？

## 1. 护城河
- 供给侧：专利/牌照/特有资源/规模效应？
- 需求侧：转换成本/网络效应/品牌溢价/习惯？
- 行业结构：集中度、新进入者壁垒、替代品威胁
- **关键问：如果竞争对手明天拿到同样的资源，这家公司能撑多久？**

## 2. 盈利质量
- 毛利率趋势及变化原因
- OCF/NI 长期 < 0.8 需警惕（赚的是纸面利润）
- 收入确认政策有无激进迹象
- **关键问：利润中多少是可持续的，多少是一次性的？**

## 3. 增长驱动
- 量价拆分：卖更多 vs 卖更贵？
- TAM和渗透率：天花板在哪？
- 增长资本效率：每赚1元利润需多少资本投入？
- **关键问：增长是否需要大量资本？资本从哪来？**

## 4. 周期敏感性
- 行业处于什么周期位置？
- 对宏观/政策/利率的敏感度
- 历史上周期振幅和持续时间
- **关键问：如果宏观恶化30%，收入和利润会跌多少？**

## 5. 管理层信号
- 资本配置纪律（并购历史、回购时机、分红政策）
- 内部人持股/增减持
- 股东态度（沟通质量、一致行动人）
- **关键问：管理层用股东的钱是在创造价值还是在摧毁价值？**
"""

REDFLAGS_V2 = """# 风险信号库

> 发现任一信号必须在结论中明确讨论。
> 不意味"一票否决"，但必须解释为什么在此案例中不致命。

## 财务危险信号
- [ ] 净资产为负
- [ ] OCF 连续3年为负
- [ ] 应收账款增速持续 > 营收增速
- [ ] 存货周转天数持续上升
- [ ] 负债率 > 80% 且连续亏损
- [ ] 商誉/总资产 > 30%
- [ ] 关联交易占比异常
- [ ] 分红率 > 100% 或借钱分红

## 业务危险信号
- [ ] 核心产品/技术被替代（结构性死亡，非周期）
- [ ] 大客户依赖 > 50%
- [ ] 管理层大量减持
- [ ] 审计师频繁更换
- [ ] 被监管调查/做空狙击

## 估值危险信号
- [ ] 乐观假设下估值也不合理
- [ ] 市场共识过于乐观
"""

VALUATION_V2 = """# 估值方法论

> 估值是最后一步。先确定"好公司"，再考虑"什么价格划算"。

## 核心原则
- 估值告诉你"市场相信什么"，不是"股票值多少"
- 安全边际来自基本面认知深度，不是 PE 低
- 同样PE：好公司便宜，烂公司贵。别跨质量比PE

## 工具优先级
1. **反推法（最实用）**：当前市值隐含什么增速？这个假设合理吗？
2. **历史对比**：当前PE/PB在5-10年分位数
3. **行业横向**：PE在行业中何处？基本面支撑吗？
4. **DCF（谨慎）**：能预测未来5-10年现金流才用，否则别用

## 底线问题
如果基本面判断正确，3年后的合理市值应该多少？
如果基本面判断错误，最大下跌空间是多少？
"""

if __name__ == "__main__":
    main()
