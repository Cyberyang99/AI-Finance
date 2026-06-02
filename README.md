# fundamental-agent

基本面研究 Agent —— 把你读过的研报和个股研究沉淀成**可复用的投资知识**，再用它来校验新标的的逻辑。

独立 CLI，调用 Claude 兼容 API（DeepSeek 等），不依赖 Claude Code。

## 两类输入 → 两类知识

| 你喂什么 | 变成什么 | 命令 |
|---|---|---|
| 行业 / 主题研报 (pdf/pptx/docx/txt) | **CoT**：行业层面可复用的投资思维链（含信号强度·证伪维·原文依据） | `fa ingest` |
| 个股纪要 / 你的投资逻辑 | **note**：个股 12 维度研究笔记（带 sector/tags） | `fa note` |

## 核心用法

```bash
fa chat                     # 推荐入口：进去有快捷菜单（1 上传研报 / 2 录入 note / 3 vet / 4 看库）

fa ingest 研报.pdf           # 提炼 CoT（自动分类 GICS 板块 + 主题 tag，原文归档）
fa note 600519.SHG -f 纪要.docx   # 录入个股 note
fa vet 0700.HK -i "你的想法"  # 逻辑校验器：用已有 CoT+note 给你的逻辑打分/补充/写反逻辑/横向对比同业
fa cot dash                 # CoT 全库统计（板块/主题/信号/质量分布）
fa notes <代码> --full       # 看某票全部历史笔记（观点演变）
fa dash                     # 总览：知识层(CoT+note) + 决策层(论点/回顾)
```

`fa vet` 是当前主力——输入陌生个股（+可选想法，想法支持贴 word/txt 文件），它做：① 给你的逻辑逐条打分 ② 补充你漏掉的产业逻辑 ③ 基于 CoT 的证伪条件写反逻辑/风险 ④ 拉同业 note 做横向对比。中性客观，逻辑与已学 CoT 相悖处会标为风险点。输出落盘不入库。

## 安装

```bash
git clone git@github.com:Cyberyang99/AI-Finance.git
cd AI-Finance
pip install -e .
cp .env.example .env        # 填入 API key + base_url（DeepSeek 等）；EODHD_API_KEY 取行情
```

模型在 `config.toml`：`[agent] model` 日常用，`[cot] extract_model` 单独配提取模型。

## 结构

```
fa/
  cli.py              # CLI 入口（所有命令）
  vet.py              # 逻辑校验器（合成层）
  sectors.py          # GICS 板块 + 主题分类（LLM 自动分类）
  agent.py            # 回顾/进化引擎（Critic/Reflector）
  ingest/             # 文档摄入：base + loaders + cot_extractor + user_note
  cot/                # CoT 加载/打分/投票/合并/统计
  chat/               # 自然语言 REPL（repl + tools）
  tools/data.py       # 基本面数据（EODHD + akshare）

memory/               # 持久记忆（cot/raw/situations 软链 OneDrive，双机同步）
  knowledge/cot/      # CoT，按 GICS 板块归类
  theses/user/        # 个股 note（<代码>_<日期>.md，按时间留存看演变）
  raw/                # 研报原文归档（可回溯原句）
  agent.db            # 论点/回顾/模式/摄入台账（sqlite）
  sectors.yaml        # 板块 + 主题分类表（可增删别名）
```

## 进化机制（已搭好，待真实数据启动）

设计闭环：建仓论点 → 持有期 `fa review` 对预测回顾 → Critic/Reflector 反思 → 沉淀情境经验/模式 → `fa evolve` 进化投资框架。

> 现状：知识层（CoT + note）已充实；回顾/进化闭环代码就位但需积累真实回顾（`fa evolve` 偏差分析需 ≥3 次回顾）才会启动。当前主力是知识沉淀 + `fa vet` 逻辑校验。
