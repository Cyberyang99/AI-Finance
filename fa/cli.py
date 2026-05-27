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

    # review2 (12d 结构化复盘)
    pr2 = sub.add_parser("review2", help="基于 12d note frontmatter 的结构化复盘 (单股)")
    pr2.add_argument("ticker", help="股票代码")
    pr2.add_argument("--no-llm", action="store_true", help="跳过 LLM 归因（省 API 费）")

    # critique
    pc = sub.add_parser("critique", help="查看某只股票最近一次 Critic 评审")
    pc.add_argument("ticker", help="股票代码")
    pc.add_argument("--rerun", action="store_true", help="重新触发 Critic 评审（消耗 API）")

    # reflect (P1) — 手动触发反思
    prf = sub.add_parser("reflect", help="对某只股票最近一次回顾跑 Reflector 反思（产出情境笔记）")
    prf.add_argument("ticker", help="股票代码")
    prf.add_argument("--force", action="store_true",
                     help="强制反思，绕过 should_reflect 阈值检查")

    # import (P0 + P1 通用入口)
    pim = sub.add_parser("import", help="通用入口：按扩展名自动分流，研报走 ingest / .md走 note")
    pim.add_argument("path", help="文件或目录路径")
    pim.add_argument("--sector", help="所属板块（默认应用到所有文件）")
    pim.add_argument("--ticker", help="股票代码（默认应用到所有文件）")
    pim.add_argument("--no-cot", action="store_true", help="研报跳过 LLM 提炼 CoT")
    pim.add_argument("--no-structure", action="store_true", help="用户笔记跳过 LLM 拆 4 维度")
    pim.add_argument("--force", action="store_true", help="强制重抽（覆盖已有 CoT）")
    pim.add_argument("--dry-run", action="store_true", help="只扫描，不实际入库")
    pim.add_argument("-i", "--interactive", action="store_true",
                     help="逐个确认每个文件 [Y/n/c=加评论/s=skip/q=quit]")

    # ingest (P0)
    pi = sub.add_parser("ingest", help="摄入外部文档 (PDF/DOCX/XLSX/PPTX) → 提炼 CoT")
    pi.add_argument("path", help="文件路径，或 --batch 时为目录")
    pi.add_argument("--ticker", help="绑定个股 (例: 2513.HK)")
    pi.add_argument("--sector", help="绑定板块 (例: AI/半导体)；不传则 LLM 自动分类")
    pi.add_argument("--batch", action="store_true", help="批量摄入目录下所有支持格式的文件")
    pi.add_argument("--no-cot", action="store_true", help="只抽文，不调用 LLM 提炼 CoT")
    pi.add_argument("--force", action="store_true", help="强制重新提炼 CoT (覆盖已有)")
    pi.add_argument("--no-classify", action="store_true",
                    help="跳过 LLM 自动分类（即使没传 --sector 也用 uncategorized）")
    pi.add_argument("-c", "--comment", help="一句话评论/角度提示，会注入 CoT prompt")
    pi.add_argument("--cot-count", type=int, help="强制本份报告抽 N 条 CoT（覆盖质量自适应）")
    pi.add_argument("--cot-min", type=int, help="最少多少条 CoT")
    pi.add_argument("--cot-max", type=int, help="最多多少条 CoT")

    # note (P0)
    pn = sub.add_parser("note", help="录入用户论点 (4 维度: 论点/护城河/反证/时间+仓位)")
    pn.add_argument("ticker", help="股票代码")
    pn.add_argument("-m", "--message", help="一句话快录 (默认 LLM 自动拆 4 维度)")
    pn.add_argument("-f", "--file", help="从文件导入 (支持 md/txt 或 pdf/pptx/docx/xlsx)")
    pn.add_argument("-c", "--comment", help="一句话评论/角度提示 (上传外部研报时主观加权)")
    pn.add_argument("--sector", help="所属板块（可选，辅助召回过滤）")
    pn.add_argument("--no-structure", action="store_true",
                    help="跳过 LLM 拆解，原文直接进 raw_text 段")
    pn.add_argument("--edit", action="store_true",
                    help="打开 $EDITOR 编辑该 ticker 最新一条笔记")
    pn.add_argument("--append", action="store_true",
                    help="同日 note 已存在时追加到末尾而非覆盖（不重抽 12 维度，原文 + comment 直接附加）")

    # notes
    pln = sub.add_parser("notes", help="列出用户论点")
    pln.add_argument("ticker", nargs="?", help="可选：只列某只票")

    # cot (P2) — CoT 选股工具
    pcot = sub.add_parser("cot", help="CoT 工具：list / score / vote")
    cotsub = pcot.add_subparsers(dest="cot_cmd")

    pcot_l = cotsub.add_parser("list", help="列出已有 CoT")
    pcot_l.add_argument("--sector", help="按板块过滤（标准 sector id，如 CapitalGoods）")
    pcot_l.add_argument("--tag", help="按主题 tag 过滤（跨板块召回，如 'AI 主题'）")
    pcot_l.add_argument("--min-signal", type=int, default=0, help="只列信号 >= N 的 CoT")
    pcot_l.add_argument("--group-by", choices=["sector", "tag", "signal", "source", "quality"],
                        help="按维度聚合输出（替代逐条列表）")
    pcot_l.add_argument("--limit", type=int, default=200, help="逐条模式下最多列多少条")

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

    pcot_m = cotsub.add_parser("merge", help="同 sector 内 CoT LLM 聚类合并去重")
    pcot_m.add_argument("--sector", help="只合并某 sector (不指定则全部 sector 逐个跑)")
    pcot_m.add_argument("--dry-run", action="store_true", help="只看建议，不归档不写盘")

    pcot_d = cotsub.add_parser("dash", help="CoT 全库统计：总数 / 板块 / tag / 信号 / 质量 分布")
    pcot_d.add_argument("--sector", help="只看某 sector")
    pcot_d.add_argument("--tag", help="只看某 tag")
    pcot_d.add_argument("--min-signal", type=int, default=0)

    pcot_e = cotsub.add_parser("edit", help="按 cot_id/source 前缀定位文件并用 $EDITOR 打开")
    pcot_e.add_argument("query", help="cot_id 前缀 / source 文件名片段 / sector 名")

    pcot_rg = cotsub.add_parser("regroup", help="单文件内重新分组合并（不重抽 CoT）")
    pcot_rg.add_argument("query", help="cot_id 前缀 / source 文件名片段")
    pcot_rg.add_argument("--dry-run", action="store_true", help="只预览不写盘")

    pcot_rs = cotsub.add_parser("rescore", help="单文件仅重新打分（不重抽 CoT）")
    pcot_rs.add_argument("query", help="cot_id 前缀 / source 文件名片段")
    pcot_rs.add_argument("--dry-run", action="store_true")

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

    # chat - 自然语言入口
    pch = sub.add_parser("chat", help="自然语言对话模式 (推荐入口)")
    pch.add_argument("--model", help="覆盖 config.toml 的 model")

    # search - ticker 模糊查询
    psr = sub.add_parser("search", help="ticker 模糊查询：公司名/拼音/代码 → 标准 ticker")
    psr.add_argument("query", help="查询词，例如：茅台、moutai、600519、Apple")
    psr.add_argument("--refresh", action="store_true",
                     help="强制刷新本地 akshare 缓存（默认 14 天过期）")
    psr.add_argument("-n", "--limit", type=int, default=5, help="返回候选数 (默认 5)")

    args = parser.parse_args()

    if args.cmd == "scan":
        _cmd_scan(args)
    elif args.cmd == "deep":
        _cmd_deep(args)
    elif args.cmd == "review":
        _cmd_review(args)
    elif args.cmd == "evolve":
        _cmd_evolve(args)
    elif args.cmd == "review2":
        _cmd_review2(args)
    elif args.cmd == "critique":
        _cmd_critique(args)
    elif args.cmd == "reflect":
        _cmd_reflect(args)
    elif args.cmd == "import":
        _cmd_import(args)
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
    elif args.cmd == "chat":
        _cmd_chat(args)
    elif args.cmd == "search":
        _cmd_search(args)
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


def _cmd_review2(args):
    from .review_v2 import do_review_v2
    do_review_v2(args.ticker, skip_llm=args.no_llm)


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


def _cmd_import(args):
    """通用入口：扫文件 → 按扩展名分流到 research/user_note."""
    from pathlib import Path
    from .ingest.runner import (
        scan_dir, classify_file, detect_ticker_from_filename,
        detect_ticker_from_frontmatter, USER_NOTE_EXT,
    )
    from .ingest.user_note import save_user_note, auto_structure, DIMENSIONS

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"[IMPORT] 路径不存在: {target}")
        return

    if target.is_file():
        files = [target]
    else:
        files = scan_dir(target, recursive=True)

    if not files:
        print(f"[IMPORT] 无可识别文件: {target}")
        return

    # 按类型分桶
    research = []
    user_notes = []
    skipped = []
    for f in files:
        c = classify_file(f)
        if c == "research":
            research.append(f)
        elif c == "user_note":
            user_notes.append(f)
        else:
            skipped.append(f)

    print(f"[IMPORT] 扫到 {len(files)} 个文件:")
    print(f"  研报: {len(research)} | 用户笔记: {len(user_notes)} | 跳过: {len(skipped)}")

    if args.dry_run:
        print(f"\n[DRY RUN] 预览各文件:")
        for f in research:
            print(f"  research → {f.name}")
        for f in user_notes:
            t = detect_ticker_from_filename(f.name)
            if not t:
                try:
                    t = detect_ticker_from_frontmatter(f.read_text(encoding="utf-8-sig"))
                except Exception:
                    t = None
            tag = t or "无 ticker → 会跳过"
            print(f"  user_note ({tag}) → {f.name}")
        return

    interactive = getattr(args, "interactive", False)

    # 1. 先处理用户笔记 (无需 API，且优先入库可影响后续 CoT 召回)
    if user_notes:
        print(f"\n--- 用户笔记 ({len(user_notes)} 份) ---")
    for f in user_notes:
        _import_user_note(f, sector=args.sector, ticker_override=args.ticker,
                          use_llm=not args.no_structure)

    # 2. 再批量摄入研报
    if research:
        print(f"\n--- 研报 ({len(research)} 份) ---")

    quit_flag = False
    for idx, f in enumerate(research, 1):
        if quit_flag:
            break

        comment = ""
        if interactive:
            print(f"\n[{idx}/{len(research)}] {f.name}")
            print(f"  sector={args.sector or '(未指定)'}  size={f.stat().st_size // 1024} KB")
            while True:
                choice = input("  [Y]导入 / [n]跳过 / [c]加评论后导入 / [s]跳过本批剩余 / [q]退出: ").strip().lower()
                if choice in ("", "y", "yes"):
                    break
                if choice in ("n", "no"):
                    print(f"  ⏭  跳过 {f.name}")
                    break
                if choice in ("c", "comment"):
                    comment = input("  评论(一句话): ").strip()
                    if comment:
                        print(f"  ✏  评论已记录: {comment}")
                    break
                if choice == "s":
                    print(f"  ⏭⏭  跳过本批剩余 {len(research) - idx + 1} 份")
                    quit_flag = True
                    break
                if choice == "q":
                    print(f"  🛑 退出 import")
                    quit_flag = True
                    break
                print(f"  无效输入，请输入 Y/n/c/s/q")
            if choice in ("n", "no"):
                continue
            if quit_flag:
                break

        _ingest_one(f, args.ticker, args.sector,
                    with_cot=not args.no_cot, force=args.force,
                    user_comment=comment)

    print(f"\n[IMPORT] 完成")


def _import_user_note(fpath, sector=None, ticker_override=None, use_llm=True):
    """单个 .md/.txt 文件作为用户论点入库."""
    from pathlib import Path
    from .ingest.runner import detect_ticker_from_filename, detect_ticker_from_frontmatter
    from .ingest.user_note import save_user_note, auto_structure, DIMENSIONS

    try:
        raw = Path(fpath).read_text(encoding="utf-8-sig")
    except Exception as e:
        print(f"  ✗ {fpath.name}: 读文件失败 - {e}")
        return

    # 找 ticker：优先 override，否则 frontmatter，否则文件名
    ticker = ticker_override
    if not ticker:
        ticker = detect_ticker_from_frontmatter(raw)
    if not ticker:
        ticker = detect_ticker_from_filename(fpath.name)
    if not ticker:
        print(f"  ⚠ {fpath.name}: 找不到 ticker（文件名前缀、frontmatter、--ticker 都没指定），跳过")
        return

    print(f"  [user_note] {fpath.name} → {ticker}")
    structured = {k: "" for k, _ in DIMENSIONS}
    if use_llm and raw.strip():
        extracted = auto_structure(ticker, raw)
        if extracted:
            structured.update(extracted)
            filled = [k for k, v in extracted.items() if v]
            print(f"    LLM 填充: {', '.join(filled) if filled else '无'}")

    try:
        path = save_user_note(
            ticker=ticker, **structured,
            raw_text=raw, sector=sector,
        )
        print(f"    ✓ 已保存 → {path.name}")
    except ValueError as e:
        print(f"    ✗ {e}")


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
        _ingest_one(
            f, args.ticker, args.sector,
            with_cot=not args.no_cot,
            force=getattr(args, "force", False),
            user_comment=getattr(args, "comment", "") or "",
            cot_count=getattr(args, "cot_count", None),
            cot_min=getattr(args, "cot_min", None),
            cot_max=getattr(args, "cot_max", None),
            auto_classify=not getattr(args, "no_classify", False),
        )


def _ingest_one(fpath, ticker, sector, with_cot=True, force=False, user_comment="",
                cot_count=None, cot_min=None, cot_max=None, auto_classify=True):
    """摄入单文件。

    user_comment: 注入 CoT prompt 并写 frontmatter
    cot_count/cot_min/cot_max: 数量约束（覆盖 LLM 质量自适应）
    auto_classify: 用户未传 sector 时，是否调 LLM 自动分类（默认开）
    """
    from .ingest import ingest_file
    from .ingest.cot_extractor import extract_cot, save_cot_file

    print(f"\n[INGEST] {fpath.name}")
    try:
        doc = ingest_file(fpath)
    except Exception as e:
        print(f"  ✗ 抽文失败: {e}")
        return

    print(f"  ✓ 抽文成功: {len(doc['text'])} 字 / {doc['pages']} 页 / hash={doc['hash']}")
    if user_comment:
        print(f"  ✏  用户评论: {user_comment}")

    # 去重逻辑：已有 CoT 才跳过；之前 --no-cot 进过的允许补 CoT；--force 强制覆盖
    existing = [r for r in store.list_ingested(limit=10000) if r["file_hash"] == doc["hash"]]
    if existing and existing[0].get("cot_count", 0) > 0 and with_cot and not force:
        print(f"  ⚠ 已摄入并提炼过 {existing[0]['cot_count']} 条 CoT，跳过 (用 --force 强制重抽)")
        return
    if existing and existing[0].get("cot_file") and force:
        from pathlib import Path as _P
        old_cot = _P(__file__).resolve().parent.parent / "memory" / existing[0]["cot_file"]
        if old_cot.exists():
            old_cot.unlink()
            print(f"  ↺ --force: 删除旧 CoT 文件 {old_cot.name}")

    # 自动分类：用户没指定 --sector 时，调 LLM 从 sectors.yaml 挑
    final_sector = sector
    auto_tags = []
    if not final_sector and auto_classify and with_cot:
        from .sectors import classify_doc, display_sector
        print(f"  [LLM] 自动分类中...")
        cls = classify_doc(doc["filename"], doc["text"], user_comment=user_comment)
        final_sector = cls["sector_id"]
        auto_tags = cls.get("tags") or []
        print(f"  ✓ 分类: {display_sector(final_sector)} "
              f"(置信度 {cls.get('confidence', '?')}) tags={auto_tags or '(无)'}")
        if cls.get("reasoning"):
            print(f"    理由: {cls['reasoning'][:120]}")

    cot_count_out = 0
    cot_file_rel = None
    if with_cot:
        print(f"  [LLM] 提炼 CoT 中{'（围绕用户评论角度）' if user_comment else ''}...")
        result = extract_cot(
            doc["text"], user_comment=user_comment,
            min_cots=cot_min, max_cots=cot_max, force_count=cot_count,
        )
        cots = result["cots"]
        cot_count_out = len(cots)
        quality_rating = result.get("quality_rating", 0)
        quality_reason = result.get("quality_reason", "")
        if quality_rating > 0:
            print(f"  ★ 研报质量: {'⭐' * quality_rating} ({quality_rating}/5) {quality_reason}")
        if cot_count_out > 0:
            cot_path = save_cot_file(
                cots, ticker, final_sector, doc["filename"], doc["hash"],
                user_comment=user_comment, tags=auto_tags,
                quality_rating=quality_rating, quality_reason=quality_reason,
            )
            cot_file_rel = str(cot_path.relative_to(cot_path.parents[3]))
            print(f"  ✓ 提炼 {cot_count_out} 条 CoT → {cot_file_rel}")
            for i, c in enumerate(cots[:3], 1):
                detail = f"(传导{c.get('transmission', '?')}·历史{c.get('history', '?')}·时效{c.get('recency', '?')})"
                print(f"    {i}. [{c['signal']}/10] {c['trigger']}  {detail}")
            if cot_count_out > 3:
                print(f"    ... 还有 {cot_count_out - 3} 条")
        else:
            print(f"  ⚠ 未能提炼出 CoT")

    store.save_ingested_doc(
        source_path=doc["path"], filename=doc["filename"],
        file_type=doc["ext"], file_hash=doc["hash"],
        ticker=ticker, sector=final_sector, pages=doc["pages"],
        cot_count=cot_count_out, cot_file=cot_file_rel,
    )


def _cmd_note(args):
    """用户论点录入 — 统一走 12 维度模板.

    输入方式：
      -m "一句话"               → 短文本，存到 core_thesis
      -f <md/txt>               → 文本文件，LLM 抽 12 维度
      -f <pdf/pptx/docx/xlsx>   → 文档抽文 + LLM 抽 12 维度
      (无 -m 无 -f)             → 交互式 prompt 一句话

    --edit            打开 $EDITOR 编辑该 ticker 最新笔记
    --no-structure    跳过 LLM，单行直接进 core_thesis
    -c/--comment      用户角度提示
    --sector          手工指定 sector（不指定则自动继承 CoT）
    """
    from pathlib import Path
    from .ingest import (
        ingest_file, SUPPORTED_EXT, save_note_12d, inherit_sector_tags,
    )
    from .note_extractor import extract_12d
    from .note_template import empty_payload, filled_dims

    ticker = args.ticker.upper()

    # --edit 分支
    if getattr(args, "edit", False):
        _open_latest_note_in_editor(ticker)
        return

    use_llm = not getattr(args, "no_structure", False)
    user_comment = (args.comment or "").strip() if getattr(args, "comment", None) else ""
    append_mode = bool(getattr(args, "append", False))

    # 1) 获取 raw_text + source_doc
    raw_text = ""
    source_doc = ""
    source = "user"

    if args.file:
        p = Path(args.file).expanduser().resolve()
        if not p.exists():
            print(f"[NOTE] 文件不存在: {p}")
            return
        ext = p.suffix.lower()
        if ext in {".md", ".txt", ""}:
            try:
                raw_text = p.read_text(encoding="utf-8-sig")
            except Exception as e:
                print(f"[NOTE] 读取失败: {e}")
                return
            print(f"[NOTE] 文本文件: {p.name} ({len(raw_text)} 字)")
            source_doc = p.name
            source = "user_file"
        elif ext in SUPPORTED_EXT:
            print(f"[NOTE] 抽文中: {p.name}")
            try:
                doc = ingest_file(p)
            except Exception as e:
                print(f"[NOTE] 抽文失败: {e}")
                return
            raw_text = doc["text"]
            source_doc = doc["filename"]
            source = "user_doc"
            print(f"[NOTE] 抽文成功: {len(raw_text)} 字 / {doc['pages']} 页")
        else:
            print(f"[NOTE] 不支持的文件类型: {ext}")
            return
    elif args.message:
        raw_text = args.message
    else:
        # 交互式：让用户输入一句话核心论点
        print("\n=== fa note 交互录入 ===\n请输入核心论点（一句话；空回车取消）:")
        try:
            raw_text = input("> ").strip()
        except EOFError:
            raw_text = ""
        if not raw_text and not user_comment:
            print("[NOTE] 取消")
            return

    # 1.5) --append 模式：当日有 note 就直接追加文本，不重抽 12 维度
    if append_mode:
        from .ingest import append_to_today_note
        appended = append_to_today_note(ticker, raw_text=raw_text, user_comment=user_comment)
        if appended:
            print(f"[NOTE] ✓ 追加到 → {appended}")
            return
        else:
            print(f"[NOTE] --append 指定但当日无 note，回退到正常新建流程")

    # 2) 抽 12 维度
    payload = empty_payload()
    short_text = len(raw_text) < 300
    if use_llm and raw_text and not short_text:
        print(f"[NOTE] LLM 抽 12 维度{'（围绕评论角度）' if user_comment else ''}...")
        payload = extract_12d(ticker, raw_text, user_comment)
        filled = filled_dims(payload)
        if filled:
            print(f"[NOTE] LLM 填了 {len(filled)}/12: {', '.join(filled[:6])}{'...' if len(filled) > 6 else ''}")
        else:
            print(f"[NOTE] LLM 没抽出维度（资料过简或 LLM 抽风）")

    # 短文本兜底：直接把 raw_text 当 core_thesis
    if short_text and raw_text and not payload.get("core_thesis"):
        payload["core_thesis"] = raw_text.strip()
        print(f"[NOTE] 短文本兜底：→ core_thesis")

    # 3) 继承 sector/tags
    inherited_sector, inherited_tags = inherit_sector_tags(ticker)
    final_sector = args.sector or inherited_sector
    final_tags = inherited_tags  # tags 总是继承（手动改请直接编辑文件）
    if inherited_sector and not args.sector:
        print(f"[NOTE] 继承 CoT 的归类: sector={inherited_sector}, tags={inherited_tags or '(无)'}")

    # 4) 保存
    if not filled_dims(payload) and not user_comment:
        print("[NOTE] 12 维度全空且无 comment，已取消")
        return

    try:
        path = save_note_12d(
            ticker=ticker,
            payload=payload,
            sector=final_sector,
            tags=final_tags,
            user_comment=user_comment,
            source_doc=source_doc,
            source=source,
        )
        print(f"[NOTE] ✓ 已保存 → {path}")
    except ValueError as e:
        print(f"[NOTE] {e}")


def _open_latest_note_in_editor(ticker: str):
    """打开 $EDITOR (或默认 nano/vi) 编辑该 ticker 最新笔记。"""
    import os
    import subprocess
    from .ingest.user_note import load_user_notes

    notes = load_user_notes(ticker)
    if not notes:
        print(f"[NOTE-EDIT] {ticker} 还没有笔记，先用 fa note {ticker} -m '...' 录入一条")
        return

    latest = notes[0]
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    print(f"[NOTE-EDIT] 打开 {latest['path']} ({editor})")
    try:
        subprocess.run([editor, latest["path"]])
        print(f"[NOTE-EDIT] ✓ 编辑完成")
    except FileNotFoundError:
        print(f"[NOTE-EDIT] 找不到编辑器 '{editor}'，请设置环境变量 EDITOR")
    except Exception as e:
        print(f"[NOTE-EDIT] 调用编辑器失败: {e}")


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
        # Theme → tag 自动转向：用户给 --sector "光模块" 实际意思是 --tag "AI 互联"
        sector_arg = args.sector
        tag_arg = getattr(args, "tag", None)
        if sector_arg:
            from .sectors import resolve_alias, get_sector
            resolved = resolve_alias(sector_arg)
            if resolved:
                info = get_sector(resolved)
                if info and info.get("parent") == "Theme":
                    if not tag_arg:
                        tag_arg = info["name_cn"]
                        print(f"[COT] '{sector_arg}' 是主题 → 按 tag='{tag_arg}' 跨板块召回")
                    sector_arg = None
                else:
                    sector_arg = resolved
        cots = load_cots(sector=sector_arg, min_signal=args.min_signal, tag=tag_arg)
        if not cots:
            filters = f"sector={sector_arg}, min_signal={args.min_signal}"
            if tag_arg:
                filters += f", tag={tag_arg}"
            print(f"[COT] 无符合条件的 CoT ({filters})")
            return

        group_by = getattr(args, "group_by", None)
        if group_by:
            from collections import Counter
            print(f"\n=== CoT 聚合 by {group_by} ({len(cots)} 条) ===\n")
            if group_by == "sector":
                ctr = Counter(c.get("_sector") or "uncategorized" for c in cots)
            elif group_by == "tag":
                ctr = Counter()
                for c in cots:
                    for t in c.get("_tags", []):
                        ctr[t] += 1
                    if not c.get("_tags"):
                        ctr["(无 tag)"] += 1
            elif group_by == "signal":
                from .cot.stats import _signal_bucket
                ctr = Counter(_signal_bucket(c.get("signal", "5")) for c in cots)
            elif group_by == "source":
                ctr = Counter(c.get("_source", "?") for c in cots)
            elif group_by == "quality":
                ctr = Counter(c.get("_quality_rating", 0) or "未评级" for c in cots)
            max_n = max(ctr.values(), default=1)
            for key, n in ctr.most_common():
                bar = "▰" * max(1, int(n / max_n * 20))
                label = f"⭐ × {key}" if group_by == "quality" and isinstance(key, int) else str(key)
                print(f"  {label:<32} {n:4d}  {bar}")
            return

        limit = getattr(args, "limit", 200) or 200
        print(f"\n=== CoT 列表 ({len(cots)} 条{'，截到 ' + str(limit) if len(cots) > limit else ''}) ===\n")
        for c in cots[:limit]:
            sub = ""
            if "transmission" in c and "history" in c and "recency" in c:
                sub = f" _(传导{c['transmission']}·历史{c['history']}·时效{c['recency']})_"
            print(f"  [{c['signal']}/10]{sub} {c['trigger']}")
            print(f"    {c['COT'][:140]}")
            print(f"    src={c['_source']} | sector={c['_sector']} | id={c['_cot_id']}")
            print()
        return

    if args.cot_cmd == "dash":
        from .cot import compute_stats, render_dashboard
        sector_arg = args.sector
        tag_arg = getattr(args, "tag", None)
        if sector_arg:
            from .sectors import resolve_alias
            sector_arg = resolve_alias(sector_arg) or sector_arg
        stats = compute_stats(sector=sector_arg, tag=tag_arg, min_signal=args.min_signal)
        if stats["total_cots"] == 0:
            print(f"[COT-DASH] 全库无 CoT (filters: sector={sector_arg}, tag={tag_arg}, min_signal={args.min_signal})")
            return
        print(render_dashboard(stats))
        return

    if args.cot_cmd == "edit":
        _cmd_cot_edit(args.query)
        return

    if args.cot_cmd == "regroup":
        _cmd_cot_regroup(args.query, dry_run=args.dry_run)
        return

    if args.cot_cmd == "rescore":
        _cmd_cot_rescore(args.query, dry_run=args.dry_run)
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

    if args.cot_cmd == "merge":
        from .cot.merger import merge_sector, list_sectors_with_cots

        if args.sector:
            sectors = [args.sector]
        else:
            sectors = [s for s, _ in list_sectors_with_cots()]
            if not sectors:
                print("[COT-MERGE] 没有任何 sector 有 CoT")
                return
            print(f"[COT-MERGE] 全库扫到 {len(sectors)} 个 sector，逐个合并")

        for sector in sectors:
            print(f"\n{'='*60}")
            report = merge_sector(sector, dry_run=args.dry_run)
            if "skipped" in report:
                print(f"  [{sector}] {report['skipped']}")
                continue
            if "error" in report:
                print(f"  [{sector}] ✗ {report['error']}")
                continue

            print(f"  [{sector}] {report['input_count']} → {report['output_count']} 条 "
                  f"(合并 {report['merged_groups']} 组, 缩减 {report['reduction_pct']}%)")

            if args.dry_run:
                print(f"  [DRY RUN] 预览：")
                for p in report.get("preview", []):
                    tag = f" (合并自 {p['merged_from']} 条)" if p["merged_from"] > 1 else ""
                    print(f"    [{p['signal']}/10] {p['trigger']}{tag}")
            else:
                print(f"  归档旧文件: {len(report.get('archived_files', []))} 份")
                print(f"  新合并文件: {report.get('new_file', '?')}")

        if args.dry_run:
            print(f"\n[COT-MERGE] DRY-RUN 完成，未写盘。去掉 --dry-run 真正合并。")
        return


def _cmd_cot_edit(query: str):
    """按 cot_id/source/sector 前缀定位文件并用 $EDITOR 打开。"""
    import os
    import subprocess
    from .cot.local_ops import find_cot_file

    fp = find_cot_file(query)
    if not fp:
        print(f"[COT-EDIT] 没找到匹配 '{query}' 的 CoT 文件")
        return
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "notepad"
    print(f"[COT-EDIT] 打开 {fp} ({editor})")
    try:
        subprocess.run([editor, str(fp)])
        print(f"[COT-EDIT] ✓ 编辑完成")
    except FileNotFoundError:
        print(f"[COT-EDIT] 找不到编辑器 '{editor}'，请设置环境变量 EDITOR")
    except Exception as e:
        print(f"[COT-EDIT] 调用编辑器失败: {e}")


def _cmd_cot_regroup(query: str, dry_run: bool = False):
    """单文件内 CoT 本地重组（合并去重），不重新调 LLM 抽取。"""
    from .cot.local_ops import find_cot_file, regroup_file

    fp = find_cot_file(query)
    if not fp:
        print(f"[COT-REGROUP] 没找到匹配 '{query}' 的 CoT 文件")
        return
    print(f"[COT-REGROUP] 目标文件: {fp}")
    report = regroup_file(fp, dry_run=dry_run)
    if "skipped" in report:
        print(f"  ⚠ {report['skipped']}")
        return
    if "error" in report:
        print(f"  ✗ {report['error']}")
        return
    print(f"  {report['input_count']} → {report['output_count']} 条 "
          f"(合并 {report['merged_groups']} 组，缩减 {report['reduction_pct']}%)")
    if dry_run:
        print(f"  [DRY RUN] 预览前 10 条:")
        for p in report.get("preview", [])[:10]:
            tag = f" (合并自 {p['merged_from']} 条)" if p["merged_from"] > 1 else ""
            print(f"    [{p['signal']}/10] {p['trigger']}{tag}")
        print(f"\n  去掉 --dry-run 真正写盘。")
    else:
        print(f"  ↺ 原文件备份: {report.get('backup')}")
        print(f"  ✓ 新文件: {report.get('new_file')}")


def _cmd_cot_rescore(query: str, dry_run: bool = False):
    """单文件重新打分（仅改 signal/子分，不动 trigger/COT 内容）。"""
    from .cot.local_ops import find_cot_file, rescore_file

    fp = find_cot_file(query)
    if not fp:
        print(f"[COT-RESCORE] 没找到匹配 '{query}' 的 CoT 文件")
        return
    print(f"[COT-RESCORE] 目标文件: {fp}")
    report = rescore_file(fp, dry_run=dry_run)
    if "skipped" in report:
        print(f"  ⚠ {report['skipped']}")
        return
    if "error" in report:
        print(f"  ✗ {report['error']}")
        return
    print(f"  共 {report['count']} 条，{report['updated']} 条 signal 有变动")
    for d in report.get("diffs", [])[:10]:
        print(f"    [{d['old_signal']} → {d['new_signal']}] {d['trigger']}")
    if dry_run:
        print(f"\n  去掉 --dry-run 真正写盘。")
    else:
        print(f"  ↺ 备份: {report.get('backup')}")


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
    """列出板块清单。

    分两部分：
      1. CoT 主板块（GICS 24 + 主题 7）—— 来自 memory/sectors.yaml
      2. 旧的 scan 用预设板块（PRESET_SECTORS）—— 给 fa scan 用，独立于 CoT 分类
    """
    from .sectors import list_sectors as list_cot_sectors, COT_DIR

    print("=== CoT 主板块清单（来自 memory/sectors.yaml）===\n")
    cot_secs = list_cot_sectors()
    # 按 parent 分组
    by_parent: dict[str, list] = {}
    for s in cot_secs:
        by_parent.setdefault(s["parent"], []).append(s)
    for parent in ["Energy", "Materials", "Industrials", "ConsumerDiscretionary",
                   "ConsumerStaples", "HealthCare", "Financials",
                   "InformationTechnology", "CommunicationServices",
                   "Utilities", "RealEstate", "Theme"]:
        if parent not in by_parent:
            continue
        label = "🎯 投资主题" if parent == "Theme" else parent
        print(f"  [{label}]")
        for s in by_parent[parent]:
            cnt = 0
            sub = COT_DIR / s["id"]
            if sub.exists():
                cnt = len(list(sub.glob("*.md")))
            cnt_str = f"({cnt} 份 CoT)" if cnt else ""
            print(f"    {s['id']:<28} {s['name_cn']:<20} {cnt_str}")
        print()

    # 旧 scan 用的板块（独立）
    print("=== fa scan 用板块（PRESET_SECTORS，独立于 CoT 分类）===\n")
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

def _cmd_chat(args):
    """启动自然语言 REPL."""
    from .chat.repl import run_repl
    run_repl(model=getattr(args, "model", None))


def _cmd_search(args):
    """ticker 模糊查询：公司名/拼音/代码 → 标准 ticker."""
    from .chat.resolver import resolve
    res = resolve(args.query, limit=args.limit, refresh=getattr(args, "refresh", False))
    if not res:
        print(f"[SEARCH] 无匹配: {args.query}")
        return
    print(f"\n=== 匹配 '{args.query}' ({len(res)} 条) ===\n")
    for i, r in enumerate(res, 1):
        print(f"  {i}. {r['ticker']:14} {r['name']:30} ({r.get('country', '')})  [src={r['source']}]")


if __name__ == "__main__":
    main()
