# 开发思路与决策日志

写于 2026-05-25。记录这次冲刺的关键设计决策和踩过的坑，方便未来接手时不重蹈覆辙。

## 一、方法论来源

两份核心 PDF（用户提供，未入仓库）：

| PDF | 来源 | 贡献 |
|---|---|---|
| 《主观投资框架验证与个股决策 Agent》 | 国金证券 高智威 / 张晓冉 (2026-03) | CoT 三段式 `trigger/COT/signal`、单链回测、联合投票、加权选股 |
| 《Harness Engineering：构建自主进化的金融 Agent》 | 国联民生 叶尔乐 (2026-04) | Agent = Model + Harness、Predictor→Critic→Reflector→Evolver 闭环、笔记冲突解决、情境记忆熵管理、GEPA 反思式进化 |

**核心理念**：Harness Engineering = "AI 管理学"。模型固定，靠环境/工具/流程/反馈调出可靠性。

## 二、核心架构决策

### 1. 三层记忆

| 层 | 内容 | 存储 |
|---|---|---|
| L1 硬框架 | 商业模式 / 风险信号 / 估值方法论 | `memory/framework/*.md` 人工编辑 |
| L2 软知识 | 板块知识 / 模式库 / CoT | SQLite + 文件双写 |
| L3 情景记忆 | 个股论点 / 回顾 / 情境笔记 | SQLite + Markdown 导出 |

**为什么三层而不是一层 RAG**：硬框架是认识论基础（不该被 LLM 自动改），软知识是可进化的（agent 改），情景记忆是历史记录（人和 agent 都可读）。混在一层会让 agent 把框架改坏。

### 2. 用户笔记 vs CoT 的权重

| 来源 | 权重 | 注入位置 |
|---|---|---|
| 用户笔记（fa note） | 2.0 | system prompt 最前面，明确"必须对照" |
| 情境笔记（agent 自己沉淀） | 1.0 | LLM 召回，按行业过滤后选 Top-K |
| CoT（研报提炼） | 0.7 | 高信号 (≥8) 注入作为"分析参考" |
| 框架（硬框架 md） | 不可变 | 每次注入 |

**为什么用户笔记最高**：用户的核心诉求是"agent 形成和我一致的思考逻辑"。所以用户输入永远 override agent 自己的判断。

### 3. 行业门限硬过滤

每条情境笔记带 `sector_scope`/`sector_excluded` 字段，召回前先做硬过滤再让 LLM 选 Top-K。

**为什么**：防止"成长股规律"被错误应用到"红利股"。PDF2 §2.2.4 设计。

### 4. CoT 三段式必须行业泛化

`trigger` 字段不允许带具体公司名（如 "DeepSeek V4" / "Apple Vision Pro"），必须是行业层面可复用的现象。

**为什么**：CoT 的价值在跨股票复用。带公司名的 CoT 是新闻摘要，不是逻辑。
**实现**：`fa/ingest/cot_extractor.py` 的 prompt 显式反例引导 LLM。
**实测**：DeepSeek 纪要重抽，trigger 从 "DeepSeek V4 算法创新（mHC、Muon）" → "大模型公司通过算法创新降低训练和推理成本"，跨股投票区分度从 0/6 提升到 3/5。

### 5. Reflector 只在"重大失败/重大成功"时触发

阈值（`fa/agents/reflector.py`）：
- 超额 ≤ -10% 或 ≥ +20%
- 综合分 ≤ 0.4 或 ≥ 0.85

**为什么**：中等表现（0.4-0.7）强行产笔记会噪声泛滥。PDF2 §2.2.3 原则。

### 6. Critic 评分锚定在客观分 ±0.2

LLM 给的分先校验：若偏离客观分 > 0.2，强制拉回。

**为什么**：LLM 自评有美化倾向（PDF2 OpenAI 案例：自评高估偏差）。客观分（vs 大盘超额）是不可争辩的事实。

### 7. ConflictResolver 四决策

候选笔记落盘前必须经过：
- `add`：新情境无重叠 → 直接写
- `skip`：被现有笔记覆盖 → 丢弃
- `replace`：同情境但新笔记更完整 → 替换 + 归档旧的
- `branch`：同情境但条件分支不同 → 在旧笔记追加 "## 例外分支"

**为什么**：直接写盘会让笔记爆炸。PDF2 §2.2.4 熵管理。

### 8. 情境笔记不进 git

`memory/situations/*.md` 在 `.gitignore` 里。
**为什么**：笔记 frontmatter 含 `source_thesis: 2513.HK` 等字段，暴露用户研究标的。
**特例**：可用 `git add -f` 手动同步特别重要的笔记。

## 三、技术踩坑

### Windows 编码问题（系统性）

| 坑 | 现象 | 解决 |
|---|---|---|
| toml 文件 GBK 读 | Windows 默认编码 GBK，中文 config.toml 读崩 | `open(cfg_path, encoding="utf-8")` |
| .env 含 BOM | PowerShell `Set-Content -Encoding utf8` 会写 UTF-8 BOM，污染 .env 首行 key | `open(env_path, encoding="utf-8-sig")` |
| 后台 stdout GBK | `run_in_background` 模式 stdout 默认 GBK，中文输出全乱 | `cli.py` 启动时 `sys.stdout.reconfigure(encoding="utf-8")` |
| PowerShell here-string + `;` | `@"..."@` 在 `;` 后会被解析成命令一部分 | 用 Write 工具或 `-F file` 传递长文本 |
| 长 commit message 含 `-` 开头行 | PowerShell 把 `- xxx` 当成参数 | `git commit -F .commit_msg.txt` 文件方式 |

### DeepSeek 兼容 Anthropic API 配置

```
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=sk-xxx                       # DeepSeek key
```

**坑**：
- DeepSeek 走 `x-api-key` header（SDK 的 `api_key=` 参数），**不是** `Authorization: Bearer`（SDK 的 `auth_token=`）
- 之前代码逻辑是"一旦设了 base_url 就强制 auth_token"，导致 401。已修正。
- Claude Code 会自动注入 `ANTHROPIC_BASE_URL=https://api.anthropic.com` 到 shell。**项目 .env 必须 override**（`fa/config.py` 的 `load_env()` 优先级高于 shell）。
- DeepSeek 模型不存在时会自动 fallback 到 `deepseek-v4-flash`（文档明确）。默认配 flash 避免奇怪行为。

### CoT 加载器 _archive 排除

`fa/cot/loader.py` 的 `list_cot_files()` 默认 `rglob`，会递归扫到 `_archive/` 子目录里的归档文件。`fa cot list` 总数会翻倍。
**修法**：rglob 后过滤掉 `_archive` 路径段。

## 四、为什么这样设计 fa note / fa import 的入口

| 维度 | 选择 | 备选方案 | 为什么这选 |
|---|---|---|---|
| `-m` 单行入口 | LLM 自动拆 4 维度 | 直接进 raw_text 段 | 用户的核心诉求是"方便输入"，结构化对未来召回更有价值 |
| `-m` 失败时降级 | 原文一定保留到 raw_text | 失败就报错让用户重写 | 不损失输入，LLM 拆解只是 bonus |
| frontmatter 约定 | `ticker:` 字段必填 | YAML 完整 schema | 单字段够用，过度约定会让用户写不出来 |
| 文件名 ticker 提取 | 正则匹配前缀 | LLM 推断 | 正则零成本零延迟，文件命名规范是低门槛 |
| `fa import` 分流 | 按扩展名硬路由 | LLM 判断内容类型 | 扩展名 100% 准确，LLM 判断会出错 |

## 五、命名/结构约定的执行情况

- ✅ ticker 大写 + 后缀（`2513.HK` / `600519.SHG`）
- ✅ 日期 `YYYY-MM-DD`
- ✅ md 文件无 BOM
- ✅ Frontmatter 字段统一
- ⚠️ 文件名连字符 vs 下划线还混着用（`red-flags.md` 但 `risk-flags` 也接受），未来统一

## 五点五、双机同步 + 原文归档（2026-05-29 增）

### 双机记忆同步
- **设计**：git 管代码；私有 md 数据（`knowledge/cot`、`theses/user`、`situations`、`episodic`、`raw`）软链到 `OneDrive/AI-Finance-data/memory`，两机共用。
- **Windows**：`scripts/sync_setup.ps1`（junction/symlink）。**macOS**：`scripts/sync_setup.sh`（`ln -s`，`link/status/unlink` 三模式，路径用 `$HOME` 不硬编码）。
- **关键决策：`agent.db` 不软链，各机本地一份。** 理由：SQLite 文件经 OneDrive 双机同步 + 同时写会损坏库。代价：db 内容（ingested 台账 / theses / reviews）不跨机自动同步——但 `fa cot/dash/recall` 全部读文件系统，**功能不受影响**，db 只是去重台账 + 经验记录。换机做了新 ingest/note 想同步 db，需手动 merge（参照本次 5/29 的双向 merge 脚本思路：按 file_hash 去重补 ingested_docs，空表直接拷经验行）。
- **5/29 合并**：Mac 本地 4 CoT + OneDrive 13 CoT 曾分叉，已 additive 合并为 17（备份在 `agent.db.bak.20260529` 和 `memory/_premerge_bak_20260529/`）。

### 原文归档（raw）
- ingest 时把原始研报 `shutil.copy2` 到 `memory/raw/<hash>_<原名>`（`fa/ingest/base.py::archive_raw`），`ingested_docs.raw_path` 记录相对路径。
- **为什么要做**：之前只存 CoT 蒸馏，`source_path` 指向桌面原文；删了桌面文件原文就没了，无法回溯原句/核对引用/重抽。归档后原文随 OneDrive 双机留存。
- 回溯：`fa cot raw <query> [--open]` 按 CoT 的 `source_hash` 在 `raw/` 找回原文。
- **历史数据**：5/29 之前摄入的 17 份 CoT 原文未归档（多数桌面原文已删），`fa cot raw` 会优雅提示「重新 ingest 即可补归档」。

## 五点六、fa chat 体验升级（2026-05-29 增）

围绕四块提升，全部不加新依赖（rich 已装、readline 标准库）：

1. **UI（repl.py）**：rich 渲染——assistant 回复走 Markdown（表格/标题都能渲），工具调用/结果走淡色，LLM 调用期间 `console.status` 转圈。readline 输入历史（↑↓ 调历史，持久化 `~/.fa_chat_history`）。rich/readline 不可用时自动回退纯 print。
2. **问答上下文**：① 上下文裁剪——超 `_MAX_CTX_CHARS` 时按"整轮(user 文本消息为边界)"从最旧裁，保 tool_use/result 配对不破。② 会话持久化——自动落盘 `memory/.chat_sessions/`（gitignore），`/save /load /sessions` 续聊。③ system prompt 注入「记忆概览」（板块/CoT/note 分布）给 LLM 接地。
3. **召回/查询（tools.py 新工具）**：`search_memory`（跨 CoT 正文+note 关键词检索，返回定位 id）、`get_cot`（取某份 CoT 全文）、`get_note`（取某 ticker 笔记全文）、`list_cot` 加 keyword 过滤。**质变：bot 从"只会办事"变成"能读内容答问题"**——问"X 的核心逻辑"会 search→get_cot→综合回答。
4. **删除/修改/合并**：`merge_cot`/`regroup_cot`/`rescore_cot`（包现成函数）、`reclassify_cot`（已有）、`delete_cot`/`delete_note`。**删除=软删除**：移到 `_archive/` 加 `deleted-YYYYMMDD-` 前缀（loader 跳过 `_archive*`，从 list/搜索/投票消失但文件还在，可恢复）——守红线"不删 memory"。

### CoT 打分区分度升级 v3（2026-05-29）
**病根**：signal 是"同一个 LLM 同一次生成顺手自评"→ 必然宽松（实测 ≥7 占 86%、≥8 占 56%，闸门 min_signal=7 形同虚设）；且原三维（传导/历史/时效）只量"可追踪性"，没有一维管"这是逻辑还是一家之言"。

**改动**（只攻打分区分度，存量按用户决定暂不批量重打）：
1. **维度 3→4**：新增 `falsifiability`（可证伪性/具体性）。9-10=有可观测触发+明确反证；1-5=纯价值判断/不可证伪（"管理优秀""护城河强""话语权提升"这类）。
2. **权重重配**（config.toml [cot.score_weights]）：传导 0.35 / **证伪 0.30** / 历史 0.20 / 时效 0.15。证伪给到 0.3 才压得住软论断。
3. **rescore 升级为独立 Critic**：抗通胀锚定（明示目标分布 ~15% 到 8+ / ~50% 在 6-7 / ~35% 在 5 以下）+ 强制每条写 `why_not_higher` 自我质疑 + 专挑一家之言压证伪分。

**验证**（豪迈 dry-run）：分数从全挤 7-9 → 拉开成 2~9。"内部管理文化软壁垒" 8→3(证伪1)、"极致管理护城河" 8→4(证伪2)，"下游上修产能" 7→9(证伪9)。区分度回来了。

**数据兼容**：CoT 子分行 v3 写 `传导·证伪·历史·时效`，loader 同时认 v3/v2 旧格式；`_coerce_signal` 对缺 falsifiability 的旧文件用 history 兜底，不崩不误高。ingest 仍自评(快)，质量靠 `fa cot rescore`(独立 Critic) 保证——这是用户拍板的取舍。
> 待办：用户确认后用新 Critic 批量 rescore 存量 219 条（备份后跑）。

### 主题 tag / ticker 归一化（2026-05-29）
两台机分时摄入导致同一主题被空格拆开（`AI算力` vs `AI 算力`、`AI大模型与云` vs `AI 大模型与云`），直接把 CoT 召回主轴拆成两半。已按"多数派拼写"归一（多数带空格 → 留单空格），归一后 `AI 大模型与云`=104 条、`AI 算力`=95 条。同时把 theses/user 里 3 个带前导 0 的港股 note 文件名 + frontmatter ticker + 标题统一到去零规范（`03888.HK`→`3888.HK` 等），阿里两条 note 归并到 `9988.HK`。规范见 CLAUDE.md 命名约定。归一脚本是一次性的（未保留），备份在 `memory/_normalize_bak_20260529/`。
> 教训：tag 和 ticker 都是召回键，任何"看起来一样但字节不同"的变体都会静默拆分记忆。摄入入口（ingest/note）未来应在写盘前做归一化（tag 去多余空格、ticker 过 _normalize_ticker），从源头堵住。

### ⚠️ 踩坑：thinking block 必须原样回传
DeepSeek v4 思考模式下，assistant content 含 `thinking` block。多轮 tool use 时，**这些块必须连同 signature 原样回传**给 API，否则报 `400 content[].thinking must be passed back`。
- 旧 repl 直接 `messages.append({"content": resp.content})`（原始 block 对象）所以没事。
- 重构时若把 content 转 dict 落盘/裁剪，**务必保留 thinking/redacted_thinking**（用 `block.model_dump(exclude_none=True)` 最稳，见 `_blocks_to_dicts`），只留 text/tool_use 会炸。

## 五点七、CoT 主题 tag 下沉到链级 + 合并可追溯 + 上传询问（2026-06-05）

`fa chat` 用中暴露三个问题，根因都在 CoT 数据模型。

### 1. 主题污染：tag 原本是文档级，应是链级
**病根**：`load_cots(tag=X)` 只看文件 frontmatter 的 `tags`，命中就返回该文件**全部链**，不管单条链讲什么。一份横跨软硬件的报告被打多个 tag，它讲软件的链和讲硬件的链就共享同一组 tag。量化：227 条链里 **123 条（54%）来自挂 >1 tag 的文件**（7 个文件，其中 5 个是单篇——所以主因是 tag 粒度，合并只是放大器）。
**改动**：tag 下沉到**链级**。
- 每条 CoT 落盘加一行 `**主题**: a、b`（`save_cot_file` / `_write_merged_file` 都写）；frontmatter `tags` 改为**各链 tag 的并集**（作文件级快路径过滤的超集，仍有效）。
- `loader._parse_cot_body` 解析出 `_chain_tags` + `_chain_tag_line`；`load_cots` 按链级过滤，**旧文件无 `**主题**` 行则回退文件级**（`file_chain_tagged` 标志切换）——平滑迁移，对存量行为零改变（验证 153/90/34 与改前逐字一致）。
- 生成端：`sectors.classify_chains()`（新）给每条链单独从**闭合词表**选 0-2 主题，复用 `_valid_theme_tag` 守门（延续「AI 教育」事件原则，不让 LLM 现编）。**同一函数同时服务新摄入和回填**。
- 回填：`fa cot retag-chains [--dry-run]`，真跑前备份到 `_archive_retag_bak_YYYYMMDD/`（loader 跳过，可回滚）。回填后 **AI 大模型与云 153→82、AI 算力 90→20**，硬件链不再被误召回。

**坑：flash 对结构化输出方差大**。同一文件两次跑结果可能空/非空。对策：① 输出额度按链数放大 `min(8000, 1200+n*180)` 防截断；② 截断 JSON 用正则 `_salvage_chain_assignments` 逐条捞 `{"i":N,"tags":[...]}`；③ 重试至至少一条链有有效 tag（合法全空的文件——纯讲某公司 IPO/估值——最多试 3 次后接受空）；④ 调用方**防丢**：LLM 全空且文件原有 tag 非空 → 跳过不改。

### 2. 合并不可追溯
**病根**：merged 文件 `source_hash=merged_YYYY-MM-DD`，`fa cot raw` 回不到原始 PDF。
**改动**：`_write_merged_file` 写 frontmatter `source_hashes: [...]`（从各链 `_source_ids` 的 `<hash>_<n>` 取 hash 去重）；`loader` 解析 `_来源 CoT id` 行（整行匹配 + 去尾部斜体下划线——**cot_id 本身含 `_`，不能用 `.+?_` 非贪婪，会截断**）；`fa cot raw` 对 merged 文件逐个列出源报告（旧 merged 无 frontmatter 字段时从正文 `_source_ids` 推算）。

### 3. 上传易误操作
`repl._handle_upload_intent`：消息含文件路径但无意图词（cot/思维链 vs 笔记/note vs both）→ 弹三选一再分派 `_do_ingest_doc`/`_do_add_note`；说了意图就直接干。符合用户"反对静默自动操作"偏好。

> 「AI 教育」纪要的处置（2026-06-05）：内容确是 AI+教育，但**用户决定不把「AI 教育」加进闭合词表**（保持词表精简），故清空其错误的 `AI 大模型与云` 文件级 tag，留空——这份不被任何主题召回，仅靠 sector/关键词可达。体现闭合词表"由用户策展"原则：要不要新主题是人的决定。

## 六、给未来 Claude 的建议

1. **永远不要为"漂亮"重构记忆系统**。三层架构是有意的，不要合并。
2. **永远不要把 Reflector 触发阈值调低**。低阈值会让笔记爆炸，违背 PDF2 设计。
3. **永远不要让 agent 改 framework/*.md**。L1 是人改的认识论基础。`fa evolve --apply` 是例外，但有 LLM 审核 + 人工确认两道关。
4. **永远不要绕过 ConflictResolver 直接写笔记**。会重蹈"摄入越多越乱"覆辙。
5. **新功能优先考虑"用户的输入路径是否方便"**。这是用户的元需求。
6. **DeepSeek 是默认 LLM，但所有 prompt 写成可移植**（不要依赖 DeepSeek 特定能力）。
7. **主题 tag 是链级的，不是文档级**。召回按每条链的 `**主题**` 行（loader `_chain_tags`），旧文件回退文件级。改 CoT 召回/统计时别退回"文件 tags 覆盖全部链"的老模型。主题词表**闭合、由用户策展**：classify 只能从 `sectors.yaml` 选，套不上留空 + 报 `suggested_tags`，是否加新主题是人的决定——绝不让 LLM 现编（「AI 教育」事件教训）。
8. **改动 memory/ 批处理前先备份**（memory/knowledge/cot 是 gitignore 的本地数据，无 git 兜底）。参考 `retag_all_chains` 的 `_archive_retag_bak_*` 模式，备份目录以 `_archive` 开头让 loader 自动跳过。
7. **commit 风格**：标题用 `feat/fix/chore/refactor` 前缀，body 写"为什么"而非"做了什么"。
