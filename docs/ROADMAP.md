# fundamental-agent 路线图

写于 2026-05-25。每完成一个 Tier，回来更新这份文档。

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
