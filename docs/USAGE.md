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
# 单行快录（LLM 自动拆 4 维度）
fa note 2513.HK -m "智谱的护城河是 B 端政企关系，不是模型。算力降本后会同质化"

# 长一点写成文件
fa note 2513.HK -f my-thesis.md
```

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

### 想多股比较选股
```bash
fa cot vote T1 T2 T3 --sector "AI" --min-signal 8 --min-votes 3
```

按你的 CoT 库给每只票打分，出持仓清单 ★/·/✗。

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
