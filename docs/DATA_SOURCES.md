# 数据源盘点

写于 2026-06-17。用途：记录本机 Wind DB/技能数据源的实际可用边界，后续不要重复全库扫描。

## Wind DB 实际开放库

| 库 | 表数 | 用途 |
|---|---:|---|
| `financedata` | 166 | 行情、估值、财务、公告、基金、债券、期权、指数 |
| `reportdata` | 1 | 研报索引和摘要 |
| `clsnews` | 1 | 财联社新闻标题和正文 |

其他可见 schema 只有 `information_schema/performance_schema`。

## 研报库结论

`reportdata.report_info` 有约 192 万条，日期范围 `2020-01-01` 到 `2026-06-16`。

字段：`id / infopubldate / organization / researchers / infotitle / abstract / content / constituent / industry / stocks / classification / keywords / concept / other`。

结论：
- 没有 PDF、URL、附件路径字段。
- `content` 字段注释是“研报全文”，但 2020/2024/2025/2026 最新样本抽查均为空；智谱 2026 年以来 27 篇公司研报也全为空。
- 可用方式是“发现研报、看摘要、做覆盖统计、摘要级低保真导出”，不要把它当原文库。
- PDF 下载仍依赖东方财富/券商网页等外部源；Wind DB 命中不等于能下载 PDF。

## 公告库结论

公告在 `financedata`，与研报不同，通常至少有 Wind 链接；部分历史公告有 HTML 正文，近年 A 股公告常见形态是 `N_INFO_FTXT` 为空但 `N_INFO_ANNLINK_NEW` 指向 PDF。

| 表 | 估计行数 | 作用 |
|---|---:|---|
| `ashareanninf` | 754 万 | A 股公告 |
| `bondanninf` | 557 万 | 债券公告 |
| `fundanninf` | 694 万 | 基金公告 |
| `ashareanncolumn` | 75 | A 股公告栏目 |
| `fundanncolumn` | 110 | 基金公告栏目 |

`ashareanninf` 关键字段：
- `S_INFO_WINDCODE`：A 股 Wind 代码
- `ANN_DT`：公告日期，范围 `1991-06-10` 到 `2026-06-17`
- `N_INFO_TITLE`：标题
- `N_INFO_FCODE`：公告栏目代码
- `N_INFO_FTXT`：HTML 化正文，`longtext`；不是每条都有，近年定期报告/债券公告常为空
- `N_INFO_ANNLINK / N_INFO_WINDLINK / N_INFO_ANNLINK_NEW`：Wind 公告链接

FA 入口：
```bash
fa ann 300750.SHE --focus governance --start 2026-01-01
fa ann 300750.SHE --keyword 减持 --show-text
fa ann 600519.SHG --focus fundamental --limit 10
```

`fa chat` 已有 `list_announcements` 工具。用户问减持、增持、回购、质押、董监高变动、关联交易、处罚诉讼、业绩预告、订单、产能、并购、重大项目等，应优先查公告。

`--show-text` 会先用 DB 正文；若正文为空且链接是 PDF，会下载 Wind PDF 并用 PyMuPDF 抽前几页文本作为摘录。

当前限制：
- 只接入 A 股 `ashareanninf`。
- 港股/美股公告未在当前 DB 中找到对应表，仍需外部源。
- 公告只做查询和正文/PDF 摘录；暂不自动写入 note，避免把临时公告噪声污染长期 thesis。

## 基本面核心表

A 股：
- 基础信息：`asharedescription`
- 行情：`ashareeodprices`，`1990-12-19` 到 `2026-06-16`
- 估值/市值：`ashareeodderivativeindicator`、`asharevaluationindicator`
- 三表：`asharebalancesheet`、`ashareincome`、`asharecashflow`
- 财务指标：`asharefinancialindicator`，173 字段
- 盈利预测：`ashareconsensusdata`、`ashareconsensusrollingdata`、`ashareearningest`
- 评级/目标价：`asharestockrating`
- 资金流：`asharemoneyflow`
- 两融：`asharemargintrade`
- 分红：`asharedividend`
- 股东户数/流通股东：`ashareholdernumber`、`asharefloatholder`
- 分业务：`asharesalessegment`
- 业绩预告/快报：`ashareprofitnotice`、`ashareprofitexpress`
- IPO：`ashareipo`

港股：
- 基础信息：`hksharedescription`
- 行情：`hkshareeodprices`
- 估值/衍生指标：`hkshareeodderivativeindex`
- 财务衍生指标：`hksharefinancialderivative`
- 盈利预测：`hkprofitforecast`

基金：
- 基础信息：`chinamutualfunddescription`
- 净值：`chinamutualfundnav`
- 股票持仓：`chinamutualfundstockportfolio`
- 资产配置：`chinamutualfundassetportfolio`
- 公告：`fundanninf`

债券/期权/指数：
- 债券基础/EOD/评级/估值：`cbonddescription`、`cbondeodprices`、`cbondrating`、`cbondissuerrating`、`ccbondvaluation`
- 期权基础/EOD：`chinaoptiondescription`、`chinaoptioneodprices`
- 指数行情/成分/权重/估值：`aindex*`、`aswsindex*`

新闻：
- `clsnews.news_info`：约 135 万条，字段 `id / ctime / title / content`。

## 使用取舍

- 研报下载技能仍保留：外部源有 PDF 时继续用于批量下载和 `fa note/fa ingest`；Wind DB 无 PDF 时只能导出摘要 Markdown。
- 公告适合补三表之外的信息：治理变化、减持增持、回购、质押、处罚诉讼、重大合同、订单、产能、募投、并购、业绩预告。
- `fa vet/report` 里不要默认塞全量公告。更合理的顺序是：用户问到具体标的/治理/基本面变化时，chat 先查公告；确认重要后再手动 `fa note` 固化成长期 note。
