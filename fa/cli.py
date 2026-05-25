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

    # evolve
    pe = sub.add_parser("evolve", help="进化分析")
    pe.add_argument("--apply", type=int, help="执行指定编号的框架更新建议")

    # critique
    pc = sub.add_parser("critique", help="查看某只股票最近一次 Critic 评审")
    pc.add_argument("ticker", help="股票代码")
    pc.add_argument("--rerun", action="store_true", help="重新触发 Critic 评审（消耗 API）")

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
    do_review(args.days, with_critic=not args.no_critic)


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
