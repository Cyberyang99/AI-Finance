"""CLI 入口 — fa 命令."""

# ── 必须在所有 import 之前执行，修复 macOS Python 3.14 SSL 证书问题 ──
import os as _os
# 系统可能设了 SSL_CERT_FILE 但指向不存在的路径（Homebrew Python 常见问题）
# 直接无条件覆盖为 certifi 的证书包
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


def main():
    parser = argparse.ArgumentParser(
        prog="fa",
        description="基本面研究Agent — 框架驱动 + 记忆进化",
    )
    sub = parser.add_subparsers(dest="cmd")

    # scan
    ps = sub.add_parser("scan", help="板块横向扫描")
    ps.add_argument("topic", help="板块/主题名称")
    ps.add_argument("-l", "--limit", type=int, default=10)
    ps.add_argument("-o", "--output", help="输出路径")
    ps.add_argument("--tickers", nargs="*", help="手动指定成分股 (覆盖预设)")

    # deep
    pd = sub.add_parser("deep", help="个股深度分析")
    pd.add_argument("ticker", help="股票代码, 如 BABA.US / 300750.SHE / 0700.HK")
    pd.add_argument("--interactive", action="store_true", help="交互模式 (暂未实现)")

    # review
    pr = sub.add_parser("review", help="定期回顾")
    pr.add_argument("-d", "--days", type=int, default=90, help="回顾阈值天数")

    # sectors
    sub.add_parser("sectors", help="列出已知板块")

    # init
    sub.add_parser("init", help="初始化项目 (首次运行)")

    # config
    sub.add_parser("config", help="显示当前配置")

    args = parser.parse_args()

    if args.cmd == "scan":
        _cmd_scan(args)
    elif args.cmd == "deep":
        _cmd_deep(args)
    elif args.cmd == "review":
        _cmd_review(args)
    elif args.cmd == "sectors":
        _cmd_sectors()
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
        print(f"[SCAN] {args.topic}: {len(tickers)} 只成分股")
    else:
        print(f"[SCAN] {args.topic}: 无预设成分股，Agent 将自行搜索")

    do_scan(args.topic, tickers, args.output)


def _cmd_deep(args):
    from .agent import do_deep
    do_deep(args.ticker)


def _cmd_review(args):
    from .agent import do_review
    do_review(args.days)


def _cmd_sectors():
    print("已知板块:")
    for s in list_sectors():
        peers = find_sector_peers(s)
        print(f"  {s:12s} ({len(peers)} 只)")
    print("\n提示: 使用 'fa scan <板块名>' 扫描，或直接指定 tickers")


def _cmd_init():
    """初始化项目目录和配置文件."""
    import shutil

    project_dir = Path(__file__).resolve().parent.parent

    # 创建 memory 目录结构
    for d in ["framework", "theses", "scans", "reviews", "learnings"]:
        (project_dir / "memory" / d).mkdir(parents=True, exist_ok=True)

    # 复制 config.toml
    cfg = project_dir / "config.toml"
    if not cfg.exists():
        src = project_dir / "config.toml.example"
        if src.exists():
            shutil.copy(src, cfg)
            print(f"[INIT] 已创建 config.toml，请编辑配置")

    # 检查 .env
    env = project_dir / ".env"
    if not env.exists():
        print("[INIT] 请创建 .env 文件并设置 ANTHROPIC_API_KEY")
        print(f"  参考: {project_dir / '.env.example'}")

    # 写入初始框架文件
    init_framework_files()

    print("[INIT] 初始化完成")
    print(f"  项目目录: {project_dir}")
    print("  下一步: 编辑 .env 和 config.toml")


def init_framework_files():
    """写入初始框架文件."""
    framework_dir = Path(__file__).resolve().parent.parent / "memory" / "framework"
    framework_dir.mkdir(parents=True, exist_ok=True)

    # 只在文件不存在时创建，避免覆盖用户修改
    files = {
        "checklist.md": _DEFAULT_CHECKLIST,
        "red-flags.md": _DEFAULT_REDFLAGS,
        "valuation.md": _DEFAULT_VALUATION,
    }
    for name, content in files.items():
        path = framework_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            print(f"[INIT] 已创建框架文件: {name}")


def _cmd_config():
    cfg = load_config()
    if cfg:
        import json
        print(json.dumps(cfg, ensure_ascii=False, indent=2, default=str))
    else:
        print("无配置，请运行 'fa init'")


# ── 默认框架内容 ──

_DEFAULT_CHECKLIST = """# 商业模式质量检查清单

> 每次分析必须覆盖以下维度。不是打分，是逐项写出判断依据。

## 1. 护城河
- 供给侧优势？(专利、牌照、特有资源、规模效应)
- 需求侧锁定？(转换成本、网络效应、品牌溢价、习惯)
- 行业结构？(集中度高/分散、新进入者壁垒、替代品威胁)

## 2. 盈利质量
- 毛利率趋势？↑/↓/→，变化原因是什么？
- 现金流 vs 会计利润的差距？OCF/NI 长期 < 0.8 警惕
- 收入确认政策？有没有提前确认收入的嫌疑？

## 3. 增长驱动
- 量价拆分：增长来自卖更多 vs 卖更贵？
- 增长天花板？TAM 多大，渗透率在哪一阶段？
- 增长需要的资本？每赚1元利润要投入多少资本？

## 4. 周期敏感性
- 行业处于周期的什么位置？
- 公司对宏观/政策/利率的敏感度？
- 历史上周期的振幅和持续时间？

## 5. 管理层信号
- 资本配置纪律？(并购历史、回购时机、分红政策)
- 内部控制人持股/增减持？
- 对股东的态度？(沟通质量、一致行动人)
"""

_DEFAULT_REDFLAGS = """# 风险信号库

> 发现任一信号，必须在结论中明确讨论。
> 这并不意味着"一票否决"，但必须解释为什么这个信号在当前案例中不致命。

## 财务危险信号

- [ ] 净资产为负 — 资不抵债，严重
- [ ] OCF 连续 3 年为负 — 赚的是纸面利润
- [ ] 应收账款增速持续 > 营收增速 — 收入质量恶化
- [ ] 存货周转天数持续上升 — 产品滞销 or 渠道压货
- [ ] 资产负债率 > 80% 且连续亏损 — 债务危机风险
- [ ] 商誉/总资产 > 30% — 并购驱动增长，减值炸弹
- [ ] 关联交易占比异常高 — 利润可能被转移
- [ ] 分红率 > 100% 或借钱分红 — 不可持续

## 业务危险信号

- [ ] 核心产品/技术被替代 — 不是周期问题，是结构性死亡
- [ ] 大客户依赖 > 50% — 单一客户倒了公司就倒了
- [ ] 管理层大量减持 — 内部人知行不一
- [ ] 审计师频繁更换 — 可能在掩盖问题
- [ ] 被监管调查/做空机构狙击 — 需要额外谨慎

## 估值危险信号

- [ ] 即使乐观假设下估值也不合理 — 没有安全边际
- [ ] 市场共识过于乐观 — 预期差风险大
"""

_DEFAULT_VALUATION = """# 估值方法论

> 估值是最后一步，不是第一步。
> 先确定"这是一家好公司"，再考虑"什么价格买入划算"。

## 核心原则

- 估值告诉你"市场现在相信什么故事"，不是"股票值多少钱"
- 安全边际来自对基本面的认知深度，不是来自 PE 低
- 同一个 PE：好公司便宜，烂公司贵。不要用 PE 做跨质量比较

## 估值工具箱

### 1. 历史对比
- 当前 PE/PB/PS 在过去 5-10 年的分位数
- 如果当前便宜：是基本面恶化还是市场错杀？
- 如果当前贵：高估值隐含的增速假设是多少？

### 2. 行业横向对比
- PE 在行业中处于什么位置？
- 高/低估值是否有基本面支撑？

### 3. 反推法（最实用）
- 当前市值隐含了什么增速？
- 这个增速假设合理吗？需要什么条件？
- 如果增速假设被证伪，估值会跌到哪？

### 4. 绝对估值（谨慎使用）
- DCF 的价值在"思考过程"而非"输出数字"
- DCF 的前提是能大致预测未来 5-10 年现金流，做不到就别用
"""


if __name__ == "__main__":
    main()
