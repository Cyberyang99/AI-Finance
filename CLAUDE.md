# AI-Finance / fundamental-agent

基本面研究 Agent。框架驱动 + 记忆进化。CLI 名 `fa`。

## 项目愿景

三个核心能力：
1. **长期记忆**：情境笔记 + 跨股票胜率 + 三层记忆（硬框架/软知识/情景）
2. **自我进化**：`Predictor → Critic → Reflector → Evolver` 四步闭环（Reflector 待建）
3. **方便输入**：PDF/PPT/Excel/Word 研报 + 用户论点，agent 形成与用户一致的思考逻辑

## 目录结构约定

```
fa/                       # 代码
  agent.py                # Agent 主循环（Predictor + 工具调度）
  cli.py                  # CLI 入口
  config.py               # 配置 + .env 加载（utf-8-sig 抗 BOM）
  framework.py            # 框架文件加载（硬框架）
  agents/                 # 子 Agent
    critic.py             # 评审（独立 LLM，锚定客观分 ±0.2）
    recall.py             # 情境记忆召回（LLM 全量判断）
    reflector.py          # [P1 待建] 失败诊断
  memory/                 # 记忆引擎
    store.py              # SQLite 持久化
    situations.py         # 情境笔记（Frontmatter + md）
    predictions.py        # 预测注册 + 验证
    performance.py        # 客观分（vs 大盘超额）
    evolution.py          # 进化引擎（规则统计；P3 升级为 GEPA）
  tools/                  # 工具调用
    data.py               # EODHD + akshare 行情/财务
    sector.py             # 板块成分股
  ingest/                 # [P0 在建] 外部文档摄入
    loaders/              # pdf/docx/xlsx/pptx 四 loader
    cot_extractor.py      # 研报 → CoT 三段式
    user_note.py          # 用户论点结构化录入

memory/                   # 持久化数据（git 管理，可读、可追溯）
  framework/              # L1 硬框架（人改）
    checklist.md          # 商业模式检查清单
    red-flags.md          # 风险信号库
    valuation.md          # 估值方法论
  knowledge/              # L2 软知识（SQLite + 文件双写）
    sectors/              # 板块知识
    patterns/             # 沉淀的模式
    cot/                  # [P0] 研报提炼的 CoT
      <sector>/<yyyy-mm>_<source>.md
  theses/                 # 个股论点
    user/                 # [P0] 用户写的论点，召回权重 2.0
      <ticker>_<yyyy-mm-dd>.md
  situations/             # L3 情景记忆（Frontmatter + md）
    MEMORY.md             # 索引
    <id>.md               # 单条
  episodic/               # 历史归档
    theses/ scans/ reviews/
  raw/                    # [P0] 原始文件归档（可选）
    <hash>_<原名>
  cache/                  # 数据缓存（pickle, 24h TTL，不入 git）
  benchmarks/             # 大盘基准缓存
  agent.db                # SQLite 主库
```

## 命名 & 编码约定

- 所有 md 用 UTF-8 无 BOM
- 文件名小写，连字符分隔（`red-flags.md` 不是 `red_flags.md`）
- ticker 用大写 + 交易所后缀：`2513.HK` / `300750.SHE` / `AAPL.US`
- 日期一律 `YYYY-MM-DD`
- Frontmatter 字段固定：`ticker / sector / source / created_at / confidence / sector_scope / sector_excluded`

## 验证纪律（必须跑的命令）

改完代码后必须跑：
```bash
fa --help                       # 注册没坏
fa status                       # 配置/key 没坏
fa init                         # 框架/目录没坏
```

加新功能/改 storage 后还要跑：
```bash
fa deep 2513.HK                 # 端到端冒烟（DeepSeek 真实调用）
```

## 路线图

| Phase | 内容 | 状态 |
|---|---|---|
| 已完成 | scan / deep / review / evolve / critique / Critic / Recall / Evolution / Performance | ✅ |
| **P0**（当前） | ingest 4 格式 + fa note 4 维度 + memory/knowledge/cot/ + memory/theses/user/ | 🚧 |
| P1 | Reflector + 笔记冲突解决 (add/skip/replace/branch) + sector_scope 硬过滤 | ⏳ |
| P2 | CoT 单链回测 + 联合投票选股 | ⏳ |
| P3 | GEPA 反思式进化 + Mem-Palace 层级 + 多通道入口 | ⏳ |

## 关键配置（不进 git）

`.env` 必须有：
```
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=sk-...                            # DeepSeek key
EODHD_API_KEY=...
```

注意：
- 项目 `.env` 优先级高于 shell 环境变量（覆盖 Claude Code 注入的 `ANTHROPIC_BASE_URL=https://api.anthropic.com`）
- DeepSeek 走 `x-api-key`（SDK 的 `api_key=`），不要用 `auth_token`
- `.env` 写时务必无 BOM（PowerShell `Set-Content -Encoding utf8` 会写 BOM，用 `[System.IO.File]::WriteAllText` + `UTF8Encoding $false`）

## 红线

- 不要改 .env（红线，必须先问）
- 不要 git push（红线）
- 不要删 memory/ 目录任何内容（这是 agent 的"经验"，不可逆）
- 加新依赖前先在这里登记，再装
