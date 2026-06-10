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

### 9. 不上向量库 / RAG —— 何时才加 embedding（2026-06-08）

记忆检索**不用 embedding / 向量库**，靠两条结构化路径：
- CoT：`tag` 闭合词表（`sectors.yaml`）精确过滤 → 候选（`cot/loader.load_cots`）
- 情境笔记：把 `MEMORY.md` 索引全量喂 LLM，让 LLM 选 Top-K（`agents/recall.py`）

存储是「文件存正文 + SQLite 存元数据」，**不是向量库**：`agent.db` 只有 `ingested_docs/patterns/reviews/theses/performance/sector_knowledge` 等元数据表，**无 CoT 正文表**（227 条链全在 `memory/knowledge/cot/*.md`）。

**为什么不用向量**（`recall.py` docstring + PDF2 §2.2.3 实证）：笔记 < 100 条时「LLM 全量判断比 embedding 准」；向量是「内容多到 LLM 看不过来」才需要的压缩+近似检索妥协。当前规模（227 链 / 8 tag）下——① 候选能全量喂 LLM，让 LLM 当裁判比 cosine 聪明；② tag 召回**可追溯**（知道凭哪个 tag 召回），向量只给说不清的相似度分，投研要可追溯；③ 精确过滤几乎无假阳性、文件+SQLite 零运维。

**局限**：纯 tag 过滤的召回率吃标注质量——相关链没打所查 tag 就漏（情境笔记走 LLM 全量索引已规避；CoT 纯 tag 路理论上会漏跨主题语义关联）。

**何时才加（升级触发，现在没到）**：链/笔记到**几百上千条**、且明显感到「写过相关的、但 tag 没对上被漏」。届时——
- **加一路 embedding 做补充召回**（与 tag 过滤并联、二次扩召回），**不是替换** tag、不推翻现架构；
- 本地 `FAISS` 或 `sqlite-vec` 即可，**不需要独立向量数据库**（Chroma/Milvus/pgvector）；
- 闭合词表 + LLM 判断仍是主路，embedding 只做「tag 漏了兜底」。

### 10. chain 身份用持久 uid，不用位置号（2026-06-09）

**触发**：`fa chat` 里用 `edit_cot_chain` 删一条重复链，连环误删了 merged 文件里 3 条高分链。根因——旧 `_cot_id = source_hash_<位置序号 i>`，删/加任一链则其后全部 id 偏移一位：list 时记下的 id 删一条后就指向隔壁链（删错），agent target 不到就反复换 id 重试（撞工具循环上限）。**误删 + 撞上限是同一个根。**

**决策**：每条 CoT 落盘带 `**id**: <6hex>` 持久 uid，`_cot_id = source_hash_<uid>`，显示号 `## CoT N` 退化为纯装饰。`edit_chain` 按 uid 解析目标（回退旧位置号兼容存量），删兄弟链时其余 uid 不变。配套：①链级删除归档到 `_archive/deleted-chains-YYYYMMDD.md`（此前链级删除根本不备份，工具描述"可恢复"是假的）②删除两段式——不带 `confirm` 只回预览不删 ③`fa cot stamp-ids` 存量回填（自动备份）。

**顺手修的既存 bug**：`write_cots_to_file`（rescore/reclassify 重写盘）只写 header/信号/推理链，静默丢 `**主题**`/`**原文依据**`/`来源 CoT id`——已补全为完整 block。

**为什么不上自增主键/数据库托管 id**：CoT 是人可读 md，id 必须随文件走、可眼检、可手改（"文件即 UI"）。随机 6hex 落在正文里最省事，碰撞概率可忽略且按文件查重。详见技术铁律。

### 11. chat 工具循环上限可配（2026-06-09）

`run_repl` 上限从写死 8 改为读 `FA_CHAT_MAX_ITER`（默认 15）。根因（位置号导致的重试风暴）已由 #10 消除，15 足够；保留 env 旋钮给长任务兜底。

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

## 五点八、CoT 查询体验修复 + 链级纠错（2026-06-05）

用户高频查询 CoT，体验差且无法方便纠错。两个查询根因 bug + 一个能力缺口。

### 查询
- **Bug：`list_cot` 静默继承 `last_sector`**（`_do_list_cot`）。只给 tag 的查询被上次留下的 sector（如 Utilities）过滤 → 空 → LLM 反复换拼写硬试 → 撞循环上限。**修**：给了 tag 就完全不碰 sector，且不再读 `state["last_sector"]` 做过滤（消除"静默状态"脆弱点）。
- **Bug：tag 匹配空格敏感**（`load_cots` `target in t.lower()`）。`AI大模型与云`(无空格)=0、`AI 大模型与云`=82。**修**：去空格子串匹配（`_tag_hit`），所有调用方受益。
- **新增 `sectors.resolve_theme_tag(q)`**：模糊词 → 规范 name_cn（精确→去空格双向子串→唯一命中；零/多→候选）。`_do_list_cot` 用它解析 tag，解析不到**返回"现有主题"提示**而非空转。
- list_cot 加 `sort=asc/desc`（"看最低分"一步到位）、每行带 `id`（供纠错点名）、查询结果默认**不折叠**（`_QUIET_TOOLS` 只剩 find_ticker；查询结果就是用户要的东西）。

### 链级纠错（`local_ops.edit_chain` + chat `edit_cot_chain`）
按 `cot_id`（`<hash>_<n>`，与 `load_cots` 链序号一致）改/删**单条**链：`set_tags`（过 `_valid_theme_tag` 闭合词表守门，越界拒绝）/`set_signal`/`set_trigger`/`set_cot`/`delete`。块手术：定位第 n 个 `## CoT` 块做正则替换；**删除后重排 `## CoT K —` 链号 + 重算 frontmatter tags 并集 + `cot_count`**；delete 回显被删全文（留 chat 记录里可恢复）。chat NL：用户"把这条主题改成X/这条删掉"→ LLM 从 list/search 拿 id 调工具。
> 坑：删链后其后链的 `_cot_id` 序号前移（位置型 id 固有）；一次改一条、改完重新 list。单条编辑不做逐次备份（块级、点名、回显即安全网）。

## 五点九、召回反馈闭环：给情境笔记记胜率（2026-06-08）

**问题**：`RecallAgent` 召回情境笔记后从不复盘——哪条真帮上了判断、哪条只是反复被召回却毫无贡献，无从知晓。笔记库只进不出，慢慢积出"僵尸笔记"：白占召回名额、白烧 token、还稀释有用笔记。这是 PDF2「情境记忆熵管理」缺的那一环。

**做法**（挂在已有 deep→thesis→review 闭环上，不新建流程）：
- `theses` 表加 `recalled_note_ids TEXT`（JSON）。召回发生在**预测时**，所以 id 挂论点，不挂 review。
- `do_deep` 跑完、论点落盘后调 `store.set_thesis_recall(ticker, ids)`；id 仅取**情境笔记**（`_recall_for_deep` 第 2 段那批），用户论点/CoT 不计入（不在 situations 库、不可同口径归档）。
- `store.note_recall_stats()` 算账：每条笔记聚合它所在论点的预测命中（`correct/可验证总数`，排除"无法验证/无法判定"），出 `recall_count` / `hit_rate`。
- `fa evolve` 加一段"僵尸笔记识别"：召回≥2 次但胜率<50% → 列出来提示复核/archive。

**两个设计选择**：
- **只提示、不自动删**。守红线（不动 memory），也合 [[fa-ux-fragility-preference]]（反对静默改状态）。归档仍是人调 `situations.archive`。
- **存量 0 影响**。改动前的论点无 `recalled_note_ids`，迁移加的是 nullable 列；从下一次 `fa deep` 起才累积，跑过 deep + review 后 evolve 才出胜率表。
> 边界：`set_thesis_recall` 空列表不写——避免重跑 deep（save_thesis 是 UPSERT）时把历史归因覆盖掉。

## 五点十、进化闭环冻结 + 点评沉淀替代（2026-06-10）

**审察结论**：项目两条腿——腿 A 知识合成（ingest→CoT/note→召回→vet/report），腿 B 预测进化（论点→回顾→Critic→Reflector→Evolver）。实际使用只走腿 A；腿 B 数据为零（论点 1 / 预测验证 0/4 / 情境笔记 2），而 Mem-Palace（情境笔记>100）和 GEPA（50+ review）的解锁数据只能由腿 B 生产 → 事实死亡。根因与"不做清单"拒绝 RL/LoRA 同一个：**市场反馈太稀疏**（一年几十个验证点、周期数月、归因噪声大），单人校准不出概率。四步愿景的第三步（盈利预测 Excel）、第四步（估值/赔率）用户同日取消，逻辑自洽。

**替代方案：点评沉淀（review-rules）**。用户对每份 vet/report 输出的点评是**密集、即时、归因清晰**的反馈——正是腿 B 缺的燃料。机制：
- 点评分三类、落点不同：内容错 → 改 CoT/note（已有 edit_cot_chain）；方法不对 → `memory/framework/review-rules.md`；路由/召回错 → 框架 applies/avoid、sectors.yaml（已有）
- `framework.load_review_rules()` 只取 `## 规则` 之后正文，空/「（暂无」占位返回 ""；`inject_review_rules(system)` 拼到 vet_stock / vet_idea / vet_batch / report 四个合成 system 尾部
- batch 在循环外算一次 system，不打破 DeepSeek 前缀缓存
- **人审入库**：区分「一次性纠错」（不进规则）和「长期偏好」（才进），归类由人确认，合 [[fa-ux-fragility-preference]]
- 天花板：规则 ~30 条要合并修剪（同 CoT merger 思路）；`fa feedback` 自动化（点评→分类→提议→确认）等规则攒到十几条、确认有效后再做（不过早造工具）

**连带降级**：`fa deep` 保留当快速五维分析（预测注册尾巴无人回顾）；验证纪律冒烟从 `fa deep` 换成 `fa vet --no-save`。五点九的僵尸笔记功能随腿 B 一起冻结。唯一活着的 Tier 3 触发器：embedding 补充召回（见 二.9，链数 813 已过阈值，等"写过却没召回"的体感）。

### ⚠️ 踩坑：港股中概的双币种 + EODHD 港股数据陷阱（2026-06-10）

report 估值段被用户抓出数据错误，根因三个，全在 2513.HK 上实测坐实：
1. **财报 CNY、交易 HKD**：EODHD `General.CurrencyCode` 是交易货币（HKD），财务报表另有
   `Income_Statement.currency_symbol`（CNY）。直接拿 CurrencyCode 标利润表 = 全表标错币种，
   混币算 PS/PE。修复：`fetch_forecast_pack` 读两个币种字段，不同则按 EODHD FOREX 实时汇率
   （`fetch_fx_rate`，24h 缓存）**统一折算到上市地货币**并在数据块注明原币+汇率；折算失败保留原币+显式 ⚠。
2. **EODHD 港股市值/目标价不可靠**：Highlights.MarketCapitalization（5858亿）vs 东财（5065亿）
   vs 实时价×EODHD股本（2514亿）三者打架，分歧在股本口径；所谓 WallStreetTargetPrice（1136.9）
   实为昨收价（1136.0）。修复：市值**以上市地行情源为准**（fund，港股=东财），两源分歧>15% 在
   数据块显式预警；无评级分布（StrongBuy 等计数）的目标价直接丢弃。
3. **裸大数进提示词**：快照原来打 `市值=506477750240`，LLM 易读错量级。修复：snapshot 统一
   折成 `亿+币种`，浮点圆整 2 位。

### ⚠️ 踩坑：靠正则事后剥 LLM 输出的章节不可靠（2026-06-10）

report 嵌入 vet 结果时要删「## 待补充」尾节，最初用正则 `\n#{1,3} 待补充` 事后剥除——结果 LLM 输出成 `## 📌 待补充`（自己加了 emoji 装饰），正则没命中，整节漏进了交付的 Word。教训：**LLM 输出的标题/格式有装饰变异，事后正则匹配防不住；正确做法是从提示词源头不让它生成**（`SYNTH_TEMPLATE` 参数化 `{todo_section}`，`vet_stock(with_todo=False)` 时该节指令整体不进 prompt，并加一句「不要自行添加结构之外的章节」）。兜底正则保留但放宽为 `#{1,4}[^\n#]*待补充`（容忍装饰前缀），属第二道防线，不再是主依赖。

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
