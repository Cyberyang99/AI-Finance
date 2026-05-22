# fundamental-agent

基本面研究 Agent — 框架驱动 + 记忆进化。

独立 CLI 程序，调用 Claude API 做基本面分析，不依赖 Claude Code。

## 安装

```bash
git clone git@github.com:xxx/fundamental-agent.git
cd fundamental-agent
pip install -e .
cp .env.example .env  # 编辑填入 ANTHROPIC_API_KEY
fa init               # 初始化框架文件
```

## 用法

```bash
fa scan "固态电池"              # 板块横向扫描
fa deep 300750.SHE             # 个股深度分析
fa review                      # 定期回顾
fa sectors                     # 列出已知板块
```

## 结构

```
fa/                # 代码
  agent.py         # Agent 核心（Claude API + 工具）
  cli.py           # CLI 入口
  framework.py     # 投资框架管理
  memory.py        # 结构化记忆
  tools/
    data.py        # 基本面数据获取 (EODHD + akshare)
    sector.py      # 板块成分股发现

memory/            # 持久记忆（git 管理）
  framework/       # 可进化投资框架
  theses/          # 个股投资论点
  scans/           # 扫描存档
  reviews/         # 回顾记录
  learnings/       # 经验教训
```

## 进化机制

- `framework/` 下的检查清单随使用迭代
- `theses/` 记录每只股票的分析历史
- `fa review` 定期回顾 → 标记偏差 → 建议框架调整
- 整个 memory/ 目录用 git 版本控制，进化历史可追溯
