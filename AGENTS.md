# Hermes 开发指导：czsc_mi

把本文当作唯一执行规范。目标是在空仓 **czsc_mi** 上建成产品：以 **czsc 为缠论内核**，迁入 **misteryanalyze（本地名 stock_analyzer）** 的数据中枢、Mystery 规则、板块指数、扫描与日报/Web，且业务不绑死 czsc 内部对象。

---

## 0. 路径与角色

| 路径 | 角色 | 允许的操作 |
|------|------|------------|
| `/home/ai/ai_runner/stock/czsc_mi` | **唯一工作区**，新产品仓 | 创建、修改、提交全部发生在这里 |
| `/home/ai/ai_runner/stock/czsc` | czsc 源码参考 + 本地可编辑安装 | **只读参考**；不要改它来实现业务 |
| `/home/ai/ai_runner/stock/stock_analyzer` | misteryanalyze 1.22.x 金标与搬迁源 | **只读复制**；不要在旧仓加新功能 |

本文是后续工作的唯一执行规范：目标、规范、约束。
工程现状与实现细节见 DESIGN_DOCUMENT.md，本文不写设计方案。

## 1. 目标

1. 以 czsc 为唯一缠论识别内核；Mystery 规则（三振、主升浪、平台、VAP-ATR、形态）独立存在，二者在结果层组合，不在内核层缠死。
2. 同一标的在个股分析、daily、扫描、板块钻取中结论一致：综合评分误差 ≤ 1。
3. 对外只承认一个计算入口：`mystery.services.analyze.analyze_one_stock()` → `AnalysisResult`。
4. `MYSTERY_CHAN_ENABLED=0` 时，评分口径与 misteryanalyze 1.22.30 兼容（金标三只分差 ≤ 1）。
5. 本地行情库为分析权威数据；在线源可替换、可降级，分析不得绑定单一商业 API。
6. 产品能力覆盖 misteryanalyze 已有的投研闭环：数据更新、规则分析、扫描分类、日报/报告、Web 查看；缠论以结构摘要和可选展示/加分为限。
7. 本仓库是唯一可写产品仓。czsc 与 misteryanalyze 只作依赖或对照，不在其中实现本产品业务。

## 2. 规范

### 2.1 仓库与依赖

- 可写范围：本仓库。禁止修改 czsc 源码与 misteryanalyze/stock_analyzer 来完成本产品功能。
- 包名 `mystery`，CLI 入口 `czsc-mi`。
- 依赖以本仓库 `pyproject.toml` 为准；文档与 extras（web/chan/dev）必须与之一致。
- 密钥与 Token 只来自环境变量或主机注入文件，禁止写入仓库、日志、fixture、提交信息。

### 2.2 分层

- `mystery/core/**`：纯规则与纯数据。禁止 `import czsc`，禁止网络，禁止读写数据库。
- 只有 `mystery/adapters/czsc_adapter.py` 允许 `import czsc`。
- Web、CLI、scan、daily、报表生成器禁止自行拉 K 线后调用规则类出分，必须走 `analyze_one_stock`。
- 跨层只传递 `mystery.core.models` 中的 dataclass（或其 `to_dict()`）。禁止向 core / web / 报表传递 `CZSC`、`BI`、`ZS`、`RawBar` 实例。

### 2.3 数据与复权

- 一轮分析只使用一种复权，默认并锁定 `adjust="qfq"`，写入 `BarSeries.adjust` 与 `source`。
- 禁止将不同源、不同复权基准的 K 线拼进同一次结构识别或评分。
- 板块强度只使用 `sector_kline` 真实板块指数；禁止用成分股抽样回退。
- 行业或板块缺失时标为未知或空，不得用假数据填充分数。
- 默认活跃行情链以当前代码与配置为准；未接通的源不得在接口或文档中写成已可用。

### 2.4 模型与版本

- 跨层契约以 `mystery/core/models.py` 为准：`Bar`、`BarSeries`、`ChanBi`、`ChanZs`、`ChanStructure`、`MarketContext`、`MysteryBreakdown`、`AnalysisResult`。
- `AnalysisResult.to_dict()` 必须 JSON 可序列化。
- `rule_ver` 在兼容旧口径期间固定为 `mystery-1.22.30-compat`。变更评分公式或规则语义必须同时变更 `rule_ver`，并重出金标。

### 2.5 评分

- chan 关闭：沿用 misteryanalyze 1.22.30 综合分口径，不得悄悄改权重。
- chan 开启：允许在综合分中纳入缠论分；年线滤网未通过时综合分必须为 0。
- `true_resonance` 的布尔口径不因缠论开关改变。
- 生产默认 `MYSTERY_CHAN_ENABLED=0`。开启混合分不得破坏关闭状态下的金标。
- 买卖点、背驰等未纳入契约的缠论解释，不得进入默认综合分。

### 2.6 缠论

- 识别只允许来自 czsc。禁止自研分型/笔/中枢识别，禁止用其他库的 chanlun 结果替代 `CzscAdapter`。
- 对外只提供 `ChanStructure` 摘要，不暴露 czsc 内部对象。
- czsc 不可用时：结构可空，分析链路不得崩溃；chan 关闭时的评分仍须可用。

### 2.7 一致性与测试

- 金标标的：`sh600519`、`sz000001`、`sh600150`。chan 关闭时与对照系统分差 ≤ 1。
- 离线测试（不打行情、不依赖生产库）必须可独立通过。
- 依赖实盘行情或生产 DB 的测试必须显式标记，默认不跑。
- 扫描、日报、Web 钻取若输出评分，必须与 `analyze_one_stock` 同一路径，禁止第二套计分。

### 2.8 配置与路径

- 运行路径、数据库路径用环境变量（如 `MYSTERY_DB_PATH`）或配置文件，禁止把单机绝对路径写进业务代码。
- 本机约定可记录在文档，不作为代码默认值扩散。

## 3. 约束

### 3.1 禁止

- 把本产品代码合入 czsc 包，或把 czsc / misteryanalyze 当作本仓源码树改。
- 引入 CzscTrader、批量注册 czsc 信号、重写主前端为 Vue/FastAPI（除非规范本身被明确修订）。
- 并行维护第二套缠论识别。
- 在页面、扫描脚本、Excel 生成器中复制规则并改出另一套分数。
- 默认打开缠论混合分却仍声称与 1.22.30 金标一致。
- 全市场强制重同步、清空生产库，除非任务明确要求。
- 提交 API Key、大型数据库文件、无意义的全量行情快照。
- 用成分股抽样计算板块强度。
- 为对齐页面效果而破坏分层或唯一入口。

### 3.2 范围边界

- 本规范不规定模块如何实现、不排期、不指定补齐顺序。
- 能力是否“已具备”以代码与 DESIGN_DOCUMENT.md 为准；未写进模型与入口的行为，不得当作对外契约。
- 与 misteryanalyze 对齐的是分析口径和必要产品能力，不是目录结构或页面一一复制。

### 3.3 变更

- 改分层、改唯一入口、改评分口径、改复权锁定、改板块强度定义，视为规范变更，必须先改本文再改代码。
- 仅增加展示字段、缓存、源适配、报表排版，且不改变 `AnalysisResult` 计分语义的，不算规范变更。

