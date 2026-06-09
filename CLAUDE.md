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
  agent.py                # Agent 主循环（Predictor + 工具调度，渐进式加载）
  cli.py                  # CLI 入口（fa 各子命令注册）
  config.py               # 配置 + .env 加载（utf-8-sig 抗 BOM）
  framework.py            # 硬框架加载/更新（读写 memory/framework/）
  vet.py                  # 逻辑校验器（fa vet）：用已有 CoT+note 审视一只股票/一个想法
  sectors.py              # 板块清单 + 同义词归一 + 链级主题分类（classify_chains）
  note_extractor.py       # 文档/文本 → 12 维度个股投资逻辑 note
  note_template.py        # 12 维度笔记模板
  review_v2.py            # 基于 12 维 note frontmatter 的结构化复盘
  agents/                 # 子 Agent（各自独立 LLM）
    critic.py             # 评审（锚定客观分 ±0.2）
    recall.py             # 情境记忆召回（LLM 全量判断）
    reflector.py          # 重大失败/成功的根因诊断 → 候选情境笔记
    conflict.py           # ConflictResolver：候选笔记 vs 笔记池（add/skip/replace/branch）
  memory/                 # 记忆引擎
    store.py              # SQLite 持久化（三层记忆底座）
    situations.py         # 情境笔记（Frontmatter + md）
    predictions.py        # 预测注册 + 验证（进化闭环核心）
    performance.py        # 客观分（vs 大盘超额）
    evolution.py          # 进化引擎（规则统计；P3 升级为 GEPA）
  cot/                    # CoT 思维链子系统
    loader.py             # 加载（链级主题 tag；跳过 _archive）
    scorer.py             # 单链对单股符合度（4 维：传导/证伪/历史/时效）
    voter.py              # 多专家联合投票/加权选股
    merger.py             # 跨文档合并迭代（治"摄入越多越乱"）
    stats.py              # 全库统计（fa cot dash 后端）
    local_ops.py          # 不重抽 LLM 的本地重组/重打分/链级编辑
  tools/                  # 工具调用
    data.py               # EODHD + akshare 行情/财务 + 缓存
    sector.py             # 板块成分股发现
  ingest/                 # 外部文档摄入
    base.py               # 摄入入口（按扩展名分发 + archive_raw 原文归档）
    runner.py             # 通用导入分流（CoT vs user note）
    cot_extractor.py      # 研报文本 → CoT 三段式（trigger/COT/signal）
    user_note.py          # 用户论点 4 维度结构化录入
    loaders/              # pdf/docx/pptx/xlsx/text 五 loader
  chat/                   # fa chat 交互（Tier 2）
    repl.py               # REPL 主循环（rich UI + 会话持久化）
    tools.py              # 把 fa 命令包装成 tool use（search/get/list/merge…）
    resolver.py           # ticker 模糊解析 + 主题 tag 归一（resolve_theme_tag）

docs/                     # 项目文档
  ROADMAP.md              # 路线图（Tier 1/2/3 + 不做清单）
  DEV_NOTES.md            # 设计决策日志 + 踩坑记录
  USAGE.md                # 日常使用命令速查

memory/                   # 持久化数据（git 管理，可读、可追溯）
  framework/              # L1 硬框架（人改）
    checklist.md          # 商业模式检查清单
    red-flags.md          # 风险信号库
    valuation.md          # 估值方法论
  sectors.yaml            # 板块/主题闭合词表（classify 只能从这里选；新增主题人工改）
  knowledge/              # L2 软知识（SQLite + 文件双写）
    sectors/              # 板块知识
    patterns/             # 沉淀的模式
    cot/                  # 研报提炼的 CoT
      <sector>/<yyyy-mm>_<source>.md
  theses/                 # 个股论点
    user/                 # 用户写的论点，召回权重 2.0
      <ticker>_<yyyy-mm-dd>.md
  situations/             # L3 情景记忆（Frontmatter + md）
    MEMORY.md             # 索引
    <id>.md               # 单条
  episodic/               # 历史归档
    theses/ scans/ reviews/
  raw/                    # 原始研报归档（ingest 时自动存，软链 OneDrive 双机同步）
    <hash>_<原名>         # fa cot raw <query> 可回溯；ingested_docs.raw_path 记录
  cache/                  # 数据缓存（pickle, 24h TTL，不入 git）
  benchmarks/             # 大盘基准缓存
  agent.db                # SQLite 主库（不软链，各机本地一份）
  _archive*/              # 统一备份/归档/软删除前缀（批量改前的备份都放这；loader 跳过；不入 git）
```

## 命名 & 编码约定

- 所有 md 用 UTF-8 无 BOM
- 文件名小写，连字符分隔（`red-flags.md` 不是 `red_flags.md`）
- ticker 用大写 + 交易所后缀：`2513.HK` / `300750.SHE` / `AAPL.US`。**港股去前导 0**（`3888.HK` 不是 `03888.HK`）、A 股 6 位补零。统一走 `resolver._normalize_ticker`；note 文件名、frontmatter `ticker:`、正文标题三处必须一致
- 主题 tag：前导 ASCII 词与中文之间留**单空格**（`AI 算力` / `AI 大模型与云`，不是 `AI算力`）。tag 是 CoT 召回主轴，拼写不一致会把同一主题拆开、拖累召回
- **主题 tag 是链级的（v4 起）**：每条 CoT 落盘带一行 `**主题**: a、b`，召回按链过滤（loader `_chain_tags`）；frontmatter `tags:` = 各链 tag 的并集（快路径用）。旧文件无 `**主题**` 行则回退文件级，平滑兼容。新摄入由 `sectors.classify_chains` 逐链打 tag；存量补标用 `fa cot retag-chains`
- **主题词表闭合、由用户策展**：classify 只能从 `memory/sectors.yaml` 的 Theme 选，套不上就留空 + 报 `suggested_tags`，**是否新增主题是人的决定**（手动改 yaml），绝不让 LLM 现编
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

完整版见 [docs/ROADMAP.md](docs/ROADMAP.md)。

| Phase | 内容 | 状态 |
|---|---|---|
| 基础 | scan / deep / review / evolve / critique / Critic / Recall / Evolution / Performance | ✅ |
| **P0** | ingest 4 格式 + fa note 4 维度 + memory/knowledge/cot/ + memory/theses/user/ | ✅ |
| **P1** | Reflector + 笔记冲突解决 (add/skip/replace/branch) + sector_scope 硬过滤 | ✅ |
| **P2** | CoT 联合投票选股 + deep 模式 CoT 辅助证据 | ✅ |
| **Tier 1** | CoT 合并迭代 + note 自动结构化 + import 通用入口 | ✅ |
| **Tier 1.5** | CoT 质量自适应数量 + 显化打分子分 + dash 全库统计 + regroup/edit/rescore + docx 文本框抽取 | ✅ |
| **Tier 1.7** | 链级主题 tag（classify_chains + retag-chains 回填）+ 合并可追溯（source_hashes）+ chat 查询修死循环/降噪 + 上传意图询问 | ✅ |
| **Tier 1.8** | 查询体验：tag 模糊解析（resolve_theme_tag）+ 去空格匹配 + list_cot 排序/带 id/不再继承 sector + 链级纠错 edit_cot_chain（改主题/分/正文/删单条，闭合词表守门） | ✅ |
| **Tier 1.9** | 召回反馈闭环：theses.recalled_note_ids 记预测时召回的情境笔记 + note_recall_stats 算每条笔记胜率 + fa evolve 列"僵尸笔记"（召回≥2 次胜率<50%，只提示不自动删） | ✅ |
| **Tier 1.10** | 持久 chain uid 根治删错：每链 `**id**` 落盘 + edit/delete 按 uid 解析（删兄弟链不漂移）+ 两段式删除确认 + 链级删除归档 `_archive/deleted-chains-*` + `fa cot stamp-ids` 回填；顺修 `write_cots_to_file` 丢 主题/证据/来源id；chat 循环上限走 `FA_CHAT_MAX_ITER`（默认 15） | ✅ |
| Tier 2 | **fa chat 体验升级（rich UI + 上下文裁剪/会话持久化 + search/get 召回 + 软删除/合并）✅** ; Web UI / 微信 bot / 多 workspace ⏳ | 🔵 进行中 |
| Tier 3 | Mem-Palace 层级 + GEPA 进化 + CoT 单链回测 + 多模态 | ⏳ |

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
- **批量改 memory/ 前先备份**：cot / theses / situations / knowledge 是 gitignore 的本地数据，无 git 兜底。retag / merge / rescore 等批处理前，先把目标目录复制到 `_archive_<操作>_bak_<YYYYMMDD>/`（`_archive` 前缀 loader 自动跳过，可回滚）。历史遗留的零散备份（`*_bak_*` / `*.bak.local.*`）保留不动，要清理先问我
- **`agent.db` 绝不软链到 OneDrive/网盘**：双机同步 + 同时写会损坏 SQLite。各机本地一份，需要同步走手动 merge（见 DEV_NOTES「五点五」）
- 加新依赖前先在这里登记，再装

## 技术铁律（改相关代码前必看，违反会硬报错或静默坏数据）

- **thinking block 必须原样回传**：DeepSeek v4 思考模式下，assistant content 含 `thinking` block，多轮 tool use 时必须连 `signature` 一起回传，否则 `400 content[].thinking must be passed back`。落盘/裁剪消息时用 `block.model_dump(exclude_none=True)` 保留 thinking / redacted_thinking，只留 text / tool_use 会炸（见 `fa/chat/repl.py::_blocks_to_dicts`）
- **CoT 加载默认排除 `_archive*`**：loader `rglob` 会递归扫到归档 / 备份 / 软删除文件，必须过滤 `_archive` 路径段，否则 `fa cot list` 总数翻倍。凡"要保留但不该被扫描"的东西，一律放 `_archive` 前缀目录
- **chain uid 是持久身份，不可漂移（v5 起）**：每条 CoT 落盘带 `**id**: <6hex>` 行，`_cot_id = <source_hash>_<uid>`。**所有从结构重写链的渲染器（`merger._write_merged_file` / `cot_extractor.save_cot_file` / `local_ops.write_cots_to_file`）必须发 uid；就地改 block 的路径（`edit_chain` / `_rewrite_with_chain_tags`）必须原样保留 `**id**` 行**——漏发=新链无身份；删行=edit/delete 退回位置号，删一条全体偏移、再现连环误删。`edit_chain` 按 uid 解析目标（回退旧位置号），删除两段式（无 `confirm` 只回预览）+ 被删块归档到 `_archive/deleted-chains-*.md`。存量补 uid 用 `fa cot stamp-ids`（自动备份）

## Git 提交规范

- commit 标题用 `feat / fix / chore / refactor / docs` 前缀；body 写"为什么"而非"做了什么"
- 长 message 用 `git commit -F <file>`（PowerShell 会把 `-` 开头行当参数解析）
- 提交后停在本地等我；push 是红线
