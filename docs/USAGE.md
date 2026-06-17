# 日常使用速查

写于 2026-05-25。这是给你（Yang）自己看的命令手册，不是给 agent 看的。

## 日常工作流

### 早上开盘前
```bash
fa dash                              # 看仪表盘：活跃论点、待回顾、最近表现
fa review                            # 如有到期论点，自动触发回顾闭环
```

### 看到新研报
```bash
fa ingest "D:\xxx.pdf" --sector "AI"           # 单份
fa import "D:\今日研报" --sector "AI"            # 整个目录
```

研报最好在文件名带 sector 信息，或者每次入时手动 `--sector`。错的 sector 会让 CoT 跑错地方。

### 想到一个观点
```bash
# 单行快录（短文本直接进 core_thesis）
fa note 2513.HK -m "智谱的护城河是 B 端政企关系，不是模型。算力降本后会同质化"

# 个股报告/长文档：抽 canonical_15d_v1 稀疏 note，并归档原文 + 抽文文本
fa note 2513.HK -f my-thesis.md
fa note 2513.HK -f ~/Desktop/某券商深度.pdf -c "重点看现金流和估值假设"

# 模板升级或抽取器改进后，从归档原文重抽最新 note
fa note 2513.HK --reextract
```

note 是 report-level evidence slice：一份报告一份 note，不自动合并、不覆盖旧 note。15 个维度允许缺失；报告没写就留空，不为了填满而编。

文件命名约定（用 `fa import` 时会自动识别 ticker）：
- `2513.HK_xxx.md` ← 文件名前缀
- 或者文件内放 frontmatter：
  ```
  ---
  ticker: 2513.HK
  sector: AI
  ---
  正文...
  ```

### 想分析某只票
```bash
fa deep 2513.HK                      # 全套分析，3-5 分钟
```

输出会自动：
- 召回你之前写的 note
- 召回历史情境笔记
- 注入相关高信号 CoT
- 五维分析 + 预测注册 + 反证条件 + 风险信号
- save_thesis 入库
- 可能 save_pattern 沉淀新发现的模式

### 想校验一个逻辑（vet，不入库、不注册预测）
```bash
# 单股：用全库 CoT+note 审一只票（可带你的想法，文本或研报文件）
fa vet 2513.HK
fa vet 2513.HK -i "智谱的护城河是B端政企关系"
fa vet 2513.HK -i ~/Desktop/我的逻辑.docx

# 纯观点：不带标的，按观点主题召回，校验完顺带映射到库内相关标的
fa vet -i "AI推理需求爆发会让国产算力芯片供不应求"
fa vet -i "..." --tag "AI 算力"          # 可选：主题先过滤（闭合词表，fa sectors 查）

# 批量轻量扫描：清单进、Excel 出（汇总 + 命中明细两个 sheet）
fa vet --batch "2513.HK,2015.HK,GEV.US"   # 内联清单
fa vet --batch 自选股.xlsx                 # 表头识别：代码/ticker 列 + 观点/idea 列
fa vet --batch 名单.txt                    # 每行一个标的，后面可跟观点
```

- 单股/观点输出 markdown 到桌面；批量输出 `vet_batch_<时间>.xlsx` 到桌面
- 批量是轻量版：每股 1 次 LLM 调用，CoT 目录放 prompt 前缀吃 DeepSeek 自动缓存
- 清单里无后缀的输入（公司名/裸代码）走模糊解析，慢且可能错，建议给标准 ticker

### 同一家公司有多份报告，想生成当前综合观点
```bash
fa consolidate 2513.HK               # 多份 report-level notes → company synthesis + conflicts jsonl
fa consolidate 2513.HK --dry-run     # 只看会综合哪些 note，不调用 LLM
fa consolidate 2513.HK --no-save     # 打印综合稿，不落盘
```

- 底层 note 保留在 `memory/theses/user/`，不自动删除/覆盖。
- 综合稿写到 `memory/theses/company/`，冲突/过时信息写到 `memory/theses/conflicts/`。
- 盈利预测、估值、风险若冲突，保留分歧和来源，不直接平均。
- `fa vet` 会优先读取 company synthesis；若有 synthesis，只补最近 5 份 report-level notes，避免多报告重复刷屏。
- `fa chat` 启动时会自动检查最近 30 天新增/更新且 note>=2 的标的，最多自动综合 3 个；可用 `FA_CHAT_AUTO_CONSOLIDATE=0` 关闭。

### 批量筛查研报
```bash
python3 ~/.claude/skills/research-ingest/scripts/research_ingest.py \
  --stock 600519,300750 \
  --start 2026-01-01 \
  --limit 20

python3 ~/.claude/skills/research-ingest/scripts/research_ingest.py \
  --report-type industry \
  --keyword 光模块 \
  --start 2026-01-01 \
  --limit 10
```

- 这个技能现在只做报告筛查，不自动 `fa note/fa ingest/fa consolidate`。
- Wind DB 个股研报没有 PDF/URL 字段，且 `content` 通常为空；脚本会导出 DB 摘要 Markdown，状态标为 `text_from_db_abstract`，不要当原文研报使用。
- 每次输出 `manifest.jsonl`、`run_summary.json`、`report_screen.md` 到桌面时间戳目录。
- 外部源有 `pdf_link` 时，显式加 `--download-pdf` 才下载 PDF；拿到原文后再人工决定是否 `fa note -f` 或 `fa ingest`。

### 想查公司公告（治理/基本面变化）
```bash
fa ann 300750.SHE --focus governance --start 2026-01-01   # 减持/增持/回购/质押/治理/处罚等
fa ann 300750.SHE --focus fundamental --limit 10          # 业绩/订单/产能/并购/重大项目等
fa ann 300750.SHE --keyword 减持 --show-text              # 查关键词并显示正文摘录
```

- 公告来自 Wind DB `financedata.ashareanninf`，有 Wind 链接；部分公告有 HTML 化正文，正文为空时 `--show-text` 会尝试下载 PDF 链接并抽前几页文本。
- 当前只支持 A 股公告；港股/美股公告仍需外部源。
- `fa chat` 中直接问“某公司最近有没有减持/增持/治理公告/业绩预告/订单变化”，会调用公告工具查询。

### 想出一份完整研究笔记（report，Word 输出）
```bash
fa report 2513.HK                          # vet 校验 + 自动路由框架 → report_<ticker>_<date>.docx
fa report 2513.HK -i "我的逻辑..."          # 带自己的想法
fa report GEV.US --framework leopold-bottleneck   # 强制指定框架，跳过路由
fa report 2513.HK --framework general      # 强制用通用 15 维材料/估值模板
fa report --list                           # 看已注册的分析框架
```

- 笔记 = 第一部分逻辑校验（vet）+ 第二部分框架分析，落盘桌面 .docx
- 路由只能选 `memory/framework/frameworks/` 里已存在的框架，把握不大自动回退通用 15 维模板
- **加新框架 = 直接放一个 md 进 frameworks/**（frontmatter 写 name/title/description/applies/avoid），代码零改动；`_` 开头的文件视为草稿不参与路由
- 框架要求但库内没有的数据（期权链/做空比例/机构持仓等）会标「待人工补查」并在文末汇总，不会编数字

### 想要一份正式研究笔记（report → Word）
```bash
fa report 2513.HK                         # 三段式：逻辑校验 + 框架/9维材料分析 + 估值预期
fa report 2513.HK --framework general     # 跳过路由，强制通用 9 维
fa report --list                          # 看已注册的分析框架
```

三段结构：
1. **逻辑校验**——vet 结果（CoT 命中/反逻辑/同业对比）
2. **材料与框架分析**——命中专用框架（leopold/serenity/imacompnerd…）用框架；否则通用 9 维
   （核心论点/业务结构/财务质地/行业地位/护城河/管理层/成长史/风险/竞争优势评级）
3. **估值与预期分析（统一，不分框架）**——盈利预测/估值与目标价/催化剂/反证/跟踪指标/待人工补查清单；
   数据自动拉 EODHD（历史利润表 + 卖方一致预期 + 目标价），缺的（分业务拆分等）进补查清单

新框架往 `memory/framework/frameworks/` 扔 md 文件即可（frontmatter 写 name/title/applies/avoid）。

### 想多股比较选股
```bash
fa cot vote T1 T2 T3 --sector "AI" --min-signal 8 --min-votes 3
```

按你的 CoT 库给每只票打分，出持仓清单 ★/·/✗。

### 对输出不满意，想让它学（点评沉淀）
点评分三类，落点不同：
- **内容错了**（某条逻辑不成立/漏了产业链）→ 改知识库：`fa chat` 里 edit_cot_chain 修链，或 `fa note` 补论点
- **方法不对**（"反逻辑总是太泛""同业对比要带市占"这类每次都该改的）→ 写进
  `memory/framework/review-rules.md` 的 `## 规则` 区，下次 vet/report 合成自动注入
- **路由/召回错了** → 改框架 frontmatter 的 applies/avoid（`memory/framework/frameworks/`）或 sectors.yaml

注意：一次性纠错不进 review-rules（只修当事 CoT/note）；规则攒到 ~30 条记得合并修剪。

### 想检查 agent 学到了什么
```bash
fa notes                             # 看自己写的论点
fa cot list --sector "AI"            # 看 CoT 库
type memory\situations\MEMORY.md     # 看 agent 沉淀的经验笔记
fa sectors                           # 看已分析的板块
```

### 定期维护
```bash
# 摄入 5+ 份新研报后，合并去重
fa cot merge --dry-run               # 先看建议
fa cot merge                         # 真合并

# 跑过 5+ 次 review 后，看 agent 的弱点
fa evolve                            # 偏差分析 + 框架更新建议
```

## 常见问题

### Q: deep 跑了 8 分钟还没出来？
A: DeepSeek 慢的话单股要 5-8 分钟，正常。如果完全没输出，看后台任务文件 `C:\Users\CYBERY~1\AppData\Local\Temp\claude\...`。

### Q: review 提示"无需回顾"
A: 论点的 `review_due` 是建立时间 + 90 天。要立刻测试用 `fa review -d 0`（强制把全部论点当过期）。

### Q: CoT 投票全是"不符合"
A: 大概率是 CoT 的 sector 标签错了。检查 `fa cot list --sector X` 看是不是对应行业的逻辑。

### Q: fa deep 给的预测 70% 是"无法验证"
A: 预测里依赖了非公开/低频数据（如应收账款细节）。这是个已知问题，agent 自己在 situations 笔记里反思过了。下次写 prompt 可强调"预测必须对接季报/月报等公开数据"。

### Q: 想看 agent 用了哪个 LLM
A: `fa status` 显示当前模型，`fa config` 显示完整配置。

### Q: 跨电脑同步
A: 代码在 GitHub（`Cyberyang99/AI-Finance`），但敏感数据不入 git：
- ✅ 同步：framework/、CLAUDE.md、code
- ❌ 不同步：`.env`、`memory/theses/user/`、`memory/knowledge/cot/`、`memory/situations/`、`memory/agent.db`

要跨电脑同步私有数据，自己手动 rsync `memory/` 目录或用 OneDrive 同步。

### Q: agent 沉淀的笔记太多了乱
A: 跑 `fa cot merge` 合并 CoT；情境笔记的合并要等 P3 Mem-Palace 才做。当前如果情境笔记 > 30 条手动归档（移到 `memory/situations/_archive/`）。

## 不要做的事

- ❌ 不要手改 `memory/agent.db`（SQLite 文件，会损坏）
- ❌ 不要手改 `memory/framework/*.md` 之外的 memory 文件（agent 在维护一致性，手改会破坏）
- ❌ 不要在 review 没跑完时另开 fa deep（DB 锁冲突）
- ❌ 不要把私有 ticker 笔记 `git add -f` 然后 push（公开仓库会泄露）

## 关键路径速查

| 我想做什么 | 命令 |
|---|---|
| 看大局 | `fa dash` |
| 摄入资料 | `fa import <dir>` |
| 写观点 | `fa note <ticker> -m "..."` |
| 分析单股 | `fa deep <ticker>` |
| 选股 | `fa cot vote T1 T2 T3` |
| 回顾 | `fa review` |
| 维护 | `fa cot merge` |
