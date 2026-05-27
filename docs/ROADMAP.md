# fundamental-agent 路线图

写于 2026-05-25。每完成一个 Tier，回来更新这份文档。

---

## 🆕 v2.1 — 自然语言入口 + 两级分类 + 12 维度模板 (2026-05-26)

这一轮聚焦"降低命令行门槛 + 升级 note 严谨度"，新增 `fa chat` 自然语言入口、CoT/note 两级分类体系（GICS + 主题 tags）、note 12 维度模板（替代旧 4 维度）。

### 新增能力速查

| 能力 | 命令 / 行为 |
|---|---|
| 自然语言对话 | `fa chat` —— LLM tool use 调底层命令，9 个工具，多轮指代消解 |
| ticker 模糊查询 | `fa search 茅台` / `fa search HBM` —— EODHD + akshare 缓存（10931 条） |
| 单文件投喂 | chat 里 `[路径] [描述]` → 抽文 + 自动分类 + 提 CoT + 抽 12 维度 note 一气呵成 |
| 批量交互式投喂 | `fa import <dir> --interactive` —— 逐个 Y/n/c/s/q + 可加评论 |
| 笔记升级 | `fa note <ticker> -f <pdf/docx>` —— 文档抽文 + LLM 抽 12 维度 |
| 笔记编辑 | `fa note <ticker> --edit` —— 用 `$EDITOR` 打开最新 note |
| CoT 跨主题召回 | `fa cot list --tag "AI 算力"` —— Theme 自动转 tag 查询 |
| 板块清单 | `fa sectors` —— GICS 24 + Theme 10，含 CoT 覆盖数 |

### CoT 两级分类（`memory/sectors.yaml`）

- **一级 = GICS 24 industry group**：公司业务定位（如 CapitalGoods / Semiconductors）
- **二级 = 主题 tags（10 个）**：投资视角（AI 算力 / AI 存储 / AI 互联 / 电力能源及设备 / AI 大模型与云 / 机器人 / 太空 / 量子 / 生物医药 / 加密货币）
- 一公司可属于 1 个一级 + 多个主题；写入按一级建目录，召回按 tag 跨板块
- `Theme_AIChip` 覆盖整条算力产业链：芯片 + 板级 + 先进封装 + Foundry + 设备 + 材料 + EDA/IP

### 12 维度笔记模板（`fa/note_template.py`）

| # | 维度 | 类型 |
|---|---|---|
| 1 | core_thesis 核心论点 | 文本 |
| 2 | business_breakdown 业务结构 | 文本 |
| 3 | market_position 行业地位与竞争 | 文本 |
| 4 | moat 护城河 / 竞争优势 | 文本 |
| 5 | management_governance 管理层与治理 | 文本 |
| 6 | financial_quality 财务质地 | 文本 |
| 7 | **financial_forecast 盈利预测** | **JSON 结构化** |
| 8 | **long_term_space 远期空间** | **JSON 结构化** |
| 9 | **valuation_target 估值与目标价** | **JSON 结构化** |
| 10 | **catalysts 催化剂 / 关键时点** | **JSON 结构化** |
| 11 | falsification 反证 / 复盘信号 | 文本 |
| 12 | risks 风险清单 | 文本 |

JSON 字段同时写到 frontmatter（机器可读）和 markdown body（人可读）。

### 三类 note 来源

| source | 文件名 | 触发 |
|---|---|---|
| `user` / `user_file` / `user_doc` | `<ticker>_<date>.md` | `fa note -m` / `-f` 命令行 |
| `llm_ingest` | `<ticker>_<date>.md` | `fa chat` 投喂个股研报时自动产出 |
| `llm_deep` | `<ticker>_<date>_deep.md` | `fa deep <ticker>` 跑完自动产出 |

note 自动从该 ticker 已有的 CoT 文件继承 sector + tags（`inherit_sector_tags()`）。

---

## 已完成 (P0 + P1 + P2 + Tier 1)

| 阶段 | 内容 | 落地命令 |
|---|---|---|
| **P0** | 外部资料摄入 (PDF/DOCX/XLSX/PPTX) → CoT 三段式 | `fa ingest` |
| **P0** | 用户论点录入 4 维度 (核心论点/护城河/反证/时间+仓位) | `fa note` / `fa notes` |
| **P0** | 用户论点优先注入 deep 模式 prompt | `fa deep` |
| **P1** | Predictor → Critic → Reflector → Evolver 四 agent 闭环 | `fa review` / `fa reflect` |
| **P1** | 笔记冲突解决 add/skip/replace/branch | 自动（ConflictResolver） |
| **P1** | 行业门限硬过滤 sector_scope/excluded | 自动（recall 前） |
| **P2** | CoT 联合投票选股 | `fa cot list/score/vote` |
| **P2** | deep 模式拉高信号 CoT 作为辅助证据 | 自动注入 |
| **Tier 1** | CoT 跨文档合并迭代（避免摄入越多越乱） | `fa cot merge [--dry-run]` |
| **Tier 1** | fa note -m 自动 LLM 拆 4 维度 | `fa note <ticker> -m "..."` |
| **Tier 1** | 通用入口 fa import 按扩展名自动分流 | `fa import <dir>` |

## 当前能力分类（用法速查）

### 输入
```bash
fa import <dir> --sector X          # 一行解决一个目录的研报+笔记
fa note <ticker> -m "一句话"         # 单行快录，LLM 自动拆维度
fa note <ticker> -f file.md         # 文件导入
fa ingest <file> --sector X         # 单份研报 → CoT
fa ingest <dir> --batch             # 批量研报
```

### 分析
```bash
fa deep <ticker>                    # 单股深度，注入：用户笔记 + 情境笔记 + CoT
fa cot list [--sector X]            # 看 CoT 库
fa cot score <ticker>               # 用 CoT 给单股打分
fa cot vote T1 T2 T3                # 多股投票出持仓清单
```

### 进化
```bash
fa review [-d 0]                    # 触发回顾 (-d 0 强制触发，正常 90 天后自动)
fa reflect <ticker>                 # 单股强制反思
fa cot merge                        # 同 sector CoT 合并去重，建议摄入 5+ 份后跑
fa evolve                           # 偏差分析 + 框架更新建议
```

### 元
```bash
fa status / dash / sectors / notes  # 系统状态
fa init                             # 初始化（已运行过则跳过）
fa config                           # 看配置
```

---

## Tier 2 — 体验升级（建议先用 1-2 周再决定优先级）

> 设计原则：基于真实使用时遇到的痛点决定优先级，不要凭推断做。

### 候选 A: fa chat 对话式接入
**痛点**：每次都要敲完整命令，没法连续追问。
**例子**：
```
> AI 板块还有哪些标的符合我对智谱的判断逻辑？
> 智谱跟商汤比，财务结构差异哪里最大？
> 帮我把这些预测做成可量化的指标
```
**工作量**：1 天。基于已有 agent.py 主循环，加一个 chat 子命令保持对话状态。

### 候选 B: Streamlit Web UI
**痛点**：CLI 输入大段文字、看 dashboard 不方便。
**功能**：
- 拖文件直接 `fa import`
- 文本框写 `fa note`
- 表格化看 dashboard、CoT 列表、笔记列表
- 一键 `fa deep` / `fa review`

**工作量**：1-2 天。挂在已有 CLI 命令上。

### 候选 C: 微信/飞书 bot
**痛点**：碎片化时间想到什么没法立刻入库。
**功能**：
- 私聊机器人：发文字 → 自动判断是研究 / 笔记 / 命令
- 文件上传：直接 ingest
- 定时推送：每天的 dashboard 摘要

**工作量**：2-3 天。要在云端跑 webhook（不在本地）。

### 候选 D: 多仓库/多策略支持
**痛点**：现在所有论点放一起，没法区分"我自己的"vs"测试的"vs"客户的"。
**功能**：`fa --workspace personal` / `fa -w client-a` 多套数据隔离。
**工作量**：1 天。改 storage 加 workspace 前缀。

### 候选 E: 资料导入定时器
**痛点**：每天需要主动跑 `fa import`。
**功能**：监控某个目录，新文件自动 ingest。
**工作量**：半天。文件系统 watcher + 定时任务。

**Tier 2 决策时机**：用 1-2 周日常使用后，看哪个最常想要。

---

## Tier 3 — 战略级（等数据攒够再做）

| 内容 | 何时做 |
|---|---|
| **Mem-Palace 层级记忆**（wing/room/drawer + 时序知识图谱） | 情境笔记 > 100 条时 |
| **GEPA 反思式进化**（Evolver 读完整轨迹自动改 system prompt） | 跑过 50+ 次 review，有足够"失败/成功"案例时 |
| **CoT 单链回测**（PDF1 后半段：每条 CoT 月频选股算 IR） | 拉历史月度财务+股价数据后；适合做某个板块的 alpha 因子 |
| **多模态摄入**（图表识别） | 文本闭环已稳定使用 3-6 个月后 |
| **跨 Sector CoT 关联**（不同板块的相似逻辑） | Mem-Palace 之后 |

---

## 明确不做的（避免过度工程）

| ❌ | 理由 |
|---|---|
| **RL/LoRA 微调** | 投资数据稀疏（每年几十次预测验证），权重微调成本高、收益低 |
| **自动交易接口** | 红线，agent 是研究工具不是交易系统 |
| **过早多模态** | 文本闭环没跑顺前不碰图表/音频 |
| **真正的 RAG 嵌入库** | 笔记 < 100 条时 LLM 全量判断比嵌入准；超过再考虑 |
| **多模型路由**（GPT/Claude/Gemini 切换） | DeepSeek 性价比够，多模型徒增复杂度 |
| **预测的自动跟踪推送** | 数据源不稳定，自动化只会产生大量"无法验证"噪声 |

---

## 未来 Claude 接手时的快速上手清单

1. 读 `CLAUDE.md` 了解项目约定 + 红线
2. 读这份 `docs/ROADMAP.md` 看路线
3. 读 `docs/DEV_NOTES.md` 看历史决策和坑
4. 跑 `fa status` 看当前数据状态
5. 跑 `fa dash` 看仪表盘
6. 看 `git log --oneline` 知道最近改了什么
7. 看 `memory/situations/MEMORY.md` 知道 agent 已经沉淀了什么经验

如果用户说"继续之前的开发"：
- 默认认为是 Tier 2 起步
- 先问用户最近 1-2 周用得最难受的是什么，按那个排优先级
- 不要凭推断直接做某个 Tier 2 候选

---

## 🛠 TODO（v2.1 之后待做 — 按优先级）

### P1：review 严谨化 ✅ (2026-05-27 落地)

12 维度 note 的结构化预测现在可以对照实际数据自动复盘。

**已完成**：
- ✅ `fa/review_v2.py` — 读 12d frontmatter 四个 JSON 字段
- ✅ `compare_financial` — EODHD 年报 totalRevenue / netIncome / 派生 net_margin 对照，±15% 容差
- ✅ `compare_valuation` — 当前股价 × SharesOutstanding 算市值，对 base/bull/bear 三档定位
- ✅ `check_catalysts` — 解析 2026Q3 / 2026H1 / 2026-2027 等 window 字符串，标 已到期 / 未到期 / 无窗口
- ✅ `_attribute_errors` — LLM 归因（overall_bias + items + next_steps）
- ✅ 报告输出到 `memory/reviews/<ticker>_<date>_v2.md`
- ✅ CLI: `fa review2 <ticker> [--no-llm]`

**已知简化（v2 待跟进）**：
- 财务对照只到年度 totalRevenue / netIncome 层，未拆 by_segment（需要 akshare A 股分业务，再加一步）
- 催化剂只做窗口期检查，未判"事件是否真发生"（需要拉新闻或 LLM 联网，下个版本）
- 季度对照未做（fetch_fundamentals 只暴露年度 series）

### P1.5：复盘补 financial_quality + falsification + risks ✅ (2026-05-27 落地)

review_v2 在量化 4 维度之外，加入 3 个文本维度的 LLM 对照，复盘从"看数字"升级到"查反证"。

**已完成**：
- ✅ `check_financial_quality` — 用户写的财务质地文本 vs EODHD 实际指标（净利率/毛利率/ROE/OCF/FCF/股息率），输出"吻合/偏离/未提及/不可判断"
- ✅ `check_conditions_llm` — 通用条件检查函数，falsification 和 risks 共用，逐条标"已触发/未触发/不可判断" + evidence
- ✅ `extract_section` — 从 note body 抽 Section 6/11/12 纯文本
- ✅ 报告新增三段（财务质地对照 / 反证检查 / 风险检查 + 表格/列表格式 + 触发图标）
- ✅ `--no-llm` flag 跳过所有 LLM 调用

**实测局限（v2 暴露）**：
- 反证 / 风险里需要"分业务季度增速"、"管理层动态"、"行业市占率变化"等数据时，EODHD 不暴露 → LLM 全标"不可判断"
- 含义：纯财务可判断的条件（如毛利率下降）能闭环，其余仍需外部数据源

### P1.6：定性维度的"软复盘"（待做）

12 维度里剩 5 个纯定性维度（# 1 core_thesis, 2 business_breakdown, 3 market_position, 4 moat, 5 management_governance）还没复盘逻辑。需要外部数据源支持：

- [ ] 数据源调研：EODHD News API 是否够用 / akshare 公告流稳定性
- [ ] 写 `fetch_recent_news(ticker, days=90)` 拉最近 N 天公告/新闻
- [ ] 给每个定性维度 LLM 一个 prompt："读最新 X 条公告，判断该 thesis 是否仍成立 / 部分动摇 / 已被证伪"
- [ ] 报告增加"定性维度复盘"段

预计 4-6 小时，但前置依赖：先评估数据源质量。建议用现版本 2-3 周积累 review 经验后再决策。

### P2：行业特化模板

12 维度通用模板对所有行业能填，但部分维度颗粒度对某些行业不够贴：

- [ ] 创新药：把 `business_breakdown` 拆为 `pipeline`（在研管线，分阶段：临床前/I/II/III/上市）
- [ ] 银行：把 `financial_quality` 拆为 `nim / npl_ratio / coverage_ratio / capital_adequacy`
- [ ] 大模型公司：把 `financial_forecast` 强调 `arr / token_volume / token_price / compute_cost`
- [ ] 实现方式：`memory/sectors.yaml` 里给每个 sector 配 `template_override`，extract_12d 时读

预计 2 小时（一次只做一个行业）。

### P3：数据迁移工具

旧 4 维度 note 还有少量历史数据，应该升级到 12 维度：

- [ ] `fa migrate notes` 命令：扫 `memory/theses/user/`，遇到 `template_version != 12d_v1` 的文件，LLM 重新跑 extract_12d
- [ ] 同样的，旧 CoT 文件 frontmatter 没 `tags` 字段的，重跑 classify_doc 补 tags

预计 1 小时。

### P4：交互体验小改进 ✅ (2026-05-27 落地)

- ✅ `fa chat` 加 `/confirm on|off` 切换 yolo / 逐步确认模式（默认 off = yolo）
- ✅ `fa chat` 加 `reclassify_cot(query, new_sector, new_tags)` 工具，核心逻辑放在 `fa/cot/local_ops.reclassify_file`，sector 变更自动搬目录
- ✅ `fa note --append`：同日 note 已存在时按时间戳追加段落（不重抽 12 维度），fallback 到正常新建
- ✅ chat 长任务 Ctrl-C 优雅打断：工具执行中捕获 KeyboardInterrupt 不终止 session；输入提示符按一次只取消输入，连按两次或 /quit 才退出

### P5：可视化（最远期）

- [ ] Streamlit 本地 web：
  - sector / tag 树状导航
  - note 渲染（含 JSON 字段可视化为图表）
  - review 报告对比视图（预测 vs 实际）
  - 仪表盘：胜率、超额收益、活跃论点
- [ ] 触发条件：fa chat 用了 2-4 周后，明确知道哪些 view 真的会被反复看

预计 1-2 周。不要急。

---

## v2.1 设计决策的脚注

- **为什么 GICS 24 而不是 GICS 11**：11 一级太粗（"工业"什么都装），163 子行业又太细；24 个 industry group 是投研常用粒度。
- **为什么 tags 设计成多选而不是单选**：豪迈这种"业务是机械、增量来自 AI 数据中心电力"的票，单选 tag 必丢信息。
- **为什么 ticker resolver 用 EODHD + akshare 双源**：EODHD 不识别中文名（"茅台"），akshare 不覆盖美股；双源互补。
- **为什么 12 维度的 4 个量化字段用 JSON 而不是自由文本**：review 时要机器解析对比实际数据。如果是自由文本就只能靠 LLM 再读一遍。
- **为什么 ingest_doc 双产出而不是合并**：CoT 是"行业逻辑库"，note 是"个股论点库"，定位不同。CoT 跨股票复用，note 单股票绑定。混在一起会让两个用途都做不好。
- **为什么 deep note 单独存 `_deep.md`**：deep 是 agent 自动产出，user note 是用户手写。两者并存便于对照"我想的"和"agent 想的"。
