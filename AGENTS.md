# Hermes 开发指导：czsc_mi

把本文当作唯一执行规范。

产品仓是 **czsc_mi**：以 **czsc 为唯一缠论识别内核**，迁入 Mystery 趋势交易论的数据中枢、规则、板块指数、扫描与日报/Web。业务只消费 `ChanStructure` 等本仓模型，不绑死 czsc 内部对象。

工程现状与实现细节见 `DESIGN_DOCUMENT.md`。本文只规定目标、规范、约束，不写方案、不排期、不列版本号。

---

## 0. 路径与角色

| 路径 | 角色 | 允许的操作 |
|------|------|------------|
| `/home/ai/ai_runner/stock/czsc_mi` | **唯一工作区**，本产品仓 | 创建、修改、提交全部发生在这里 |
| `/home/ai/ai_runner/stock/czsc` | czsc 源码参考 | **只读**；禁止改它实现本产品功能 |
| `/home/ai/ai_runner/stock/stock_analyzer` | misteryanalyze 1.22.x 金标与搬迁源 | **只读复制**；禁止在旧仓加本产品功能 |

本机绝对路径只约束 Agent 工作区，不得写进 `mystery/`、`config/` 业务代码。

---

## 1. 目标

1. czsc 是唯一缠论识别内核。Mystery 规则（三振、主升浪、平台、VAP-ATR、形态）独立存在。二者只在结果层组合，不在内核层缠死。
2. 同一标的在个股分析、daily、扫描、板块钻取中结论一致：综合评分误差 ≤ 1。
3. 对外只承认一个计算入口：`mystery.services.analyze.analyze_one_stock()` → `AnalysisResult`。
4. **混合分关闭**时（生产默认 `MYSTERY_CHAN_SCORE=0`，或结构关闭），综合分与 misteryanalyze 1.22.30 兼容，金标三只分差 ≤ 1。`MYSTERY_CHAN_ENABLED` 只控制结构展示，默认开，单独开关不得改变综合分。
5. 本地行情库是分析权威数据。在线源可替换、可降级。分析不得绑定单一商业 API。
6. 产品覆盖投研闭环：数据更新、规则分析、扫描分类、日报/报告、Web 查看。缠论以结构摘要和可选展示为限，**加分默认关**。
7. 本仓库是唯一可写产品仓。czsc 与 misteryanalyze 只作依赖或对照。

---

## 2. 规范

### 2.1 仓库与依赖

- 可写范围：本仓库。禁止修改 czsc 源码与 misteryanalyze/stock_analyzer 来完成本产品功能。
- 包名 `mystery`，CLI 入口 `czsc-mi`。
- 依赖以 `pyproject.toml` 为准；文档与 extras（`web` / `chan` / `dev`）必须与之一致。
- 密钥与 Token 只来自环境变量或主机注入文件，禁止写入仓库、日志、fixture、提交信息。

### 2.2 分层

- `mystery/core/**`：纯规则与纯数据。禁止 `import czsc`，禁止网络，禁止读写数据库。
- 只有 `mystery/adapters/czsc_adapter.py` 允许 `import czsc`。
- Web、CLI、scan、daily、报表生成器禁止自行拉 K 线后调用规则类出分，必须走 `analyze_one_stock`。
- 跨层只传递 `mystery.core.models` 中的 dataclass（或其 `to_dict()`）。禁止向 core / web / 报表传递 `CZSC`、`BI`、`ZS`、`RawBar` 实例。

### 2.3 数据与复权

- 一轮分析只使用一种复权，默认并锁定 `adjust="qfq"`，写入 `BarSeries.adjust` 与 `source`。
- 禁止将不同源、不同复权基准的 K 线拼进同一次结构识别或评分。
- 同一轮 `analyze_one_stock` 内，周线、月线必须由**本轮已取日 K** `resample_bars` 得到；禁止为缠论或规则再 `fetch_bars('1w'/'1M')` 另读一遍日 K。
- 板块强度只使用 `sector_kline` 真实板块指数；禁止用成分股抽样回退。
- 行业或板块缺失时标为未知或空，不得用假数据填充分数。
- 默认活跃行情链以当前代码与配置为准；未接通的源不得在接口或文档中写成已可用。

换手与股本：

- DuckDB / fuyao 日 K 的 `turnover` 是成交额，禁止写入 `stock_kline_data.turn`。
- 集合竞价 `auction_turnover_pct` 禁止当作全日换手。
- `turn` 只允许两类来源：① 官方或 legacy 原值；② `volume（股）/ float_shares × 100`，且 `0 < turn < 80`。tdx 本地量为「手」时必须先 ×100 再除。越界丢弃，保持空。
- 空 `turn` 禁止用 0 或假数填。写入必须保护已有值（`COALESCE` / 等价逻辑）：禁止用空值或 `calc_float` 覆盖 `official` / `legacy`。
- 无有效近 20 日均换手必须 `chip_low_unknown`，不得判 `chip_low=True`，展示不得写成「非低位吸筹」。
- 流通股本低频更新（初始化 / 周对账 / 公司行动）。禁止每个交易日全市场打竞价刷股本。
- 禁止用当前股本回填快照日 `as_of` 之前的历史空 `turn`。近端短洞若做 ffill，必须标记 `turn_source=ffill`，不得标成官方。

缓存：

- 写该票 K 线后必须失效其 `analysis_cache`。
- `analysis_cache` 与 `chan_cache` 都不得改变计分语义；键必须含复权、`rule_ver`、结构/混合分开关、czsc 版本及日 K 指纹（或等价失效条件）。开关或 K 线变化必须 miss。

### 2.4 模型与版本

- 跨层契约以 `mystery/core/models.py` 为准：`Bar`、`BarSeries`、`ChanBi`、`ChanZs`、`ChanStructure`、`MarketContext`、`MysteryBreakdown`、`AnalysisResult`。
- `AnalysisResult.to_dict()` 必须 JSON 可序列化。
- `rule_ver` 在兼容旧口径期间固定为 `mystery-1.22.30-compat`。变更评分公式或规则语义必须同时变更 `rule_ver`，并重出金标。
- 改列、加表必须走 `mystery/store/migrations/*.sql`，由 `_init_db` 记账执行。禁止只改 `schema.sql`，禁止只在 `_init_db` 里手写 ALTER。

### 2.5 评分

两个独立开关，禁止再写成一个「缠论开关」：

| 开关 | 配置 | 含义 | 生产默认 |
|------|------|------|----------|
| `MYSTERY_CHAN_ENABLED` | `chan.enabled` | 结构展示（图、摘要、`result.chan`） | 开 |
| `MYSTERY_CHAN_SCORE` | `chan.score` | 综合分是否纳入缠论分 | 关 |

- 混合分关（默认）：综合分 = Mystery 1.22.30 原公式，不得改权重。年线滤网未过时，**默认综合分仍走 Mystery 原值**，禁止为了「加速扫描」把默认分打成 0 或跳过 `run_mystery`。
- 混合分开且结构开：允许 `S = 0.55×Mystery + 0.25×共振 + 0.20×缠论`。此时年线滤网未过，混合分必须为 0（防止 `0.2×S_chan` 把否决股拉成正分）。
- 只开结构、不开混合分：综合分与关结构相同，金标路径不变。
- `true_resonance` 的布尔口径不因上述开关改变。
- 买卖点、背驰等未纳入契约的缠论解释，不得进入默认综合分。要进分必须先改本节并升 `rule_ver`。
- README、DESIGN、AGENTS、`config.yaml`、启动脚本中的开关含义必须一致。

### 2.6 缠论

- 识别只允许来自 czsc。禁止自研分型/笔/中枢识别，禁止用其他库的 chanlun 结果替代 `CzscAdapter`。
- 对外只提供 `ChanStructure` 摘要，不暴露 czsc 内部对象。
- 主图默认中枢盒（plotly 自绘：K + 中枢矩形 + 笔）；czsc 官方 lightweight 仅作校验 / 降级对照。
- czsc 不可用时：结构可空，分析链路不得崩溃；混合分关闭时的评分仍须可用。

### 2.7 一致性与测试

- 金标标的：`sh600519`、`sz000001`、`sh600150`。混合分关闭时与对照系统分差 ≤ 1。
- 离线测试（不打行情、不依赖生产库）必须可独立通过。
- 依赖实盘行情或生产 DB 的测试必须显式标记，默认不跑。
- 扫描、日报、Web 钻取若输出评分，必须与 `analyze_one_stock` 同一路径，禁止第二套计分。

### 2.8 配置与路径

- 运行路径、数据库路径用环境变量（如 `MYSTERY_DB_PATH`）或配置文件，禁止把单机绝对路径写进业务代码。
- 本机约定可记录在文档与 `scripts/`，不作为 `mystery/` 代码默认值扩散。

### 2.9 展示规范

- 有证券代码必有证券名称：任何给人看的列表 / 标题 / 报表都同时带代码与名称，名称为空显示「未知」。名称来自 `result.name` / 证券表 / watchlist，不为补名称再分析。
- 自选管理是子视图（`session_state["subview"]`），不进主导航；进入子页禁止对全部自选跑分析。

### 2.10 写库职责

| 角色 | 允许写 |
|------|--------|
| `sync` / 每日管线 | K 线、财务、板块、当日空 `turn` 派生、失效 `analysis_cache` |
| `sync-shares` | 仅股本快照（低频） |
| `czsc-mi scan`（含 18:00 cron） | `scan_jobs` / `scan_results`；同类型只留最新一份 |
| Web | 默认只读 job |

- 页面触发的扫描与 cron 必须互斥（flock）；冲突时拒绝写入，不排队双写。
- `--force` / `no_persist` 可不加锁，不得作为生产双写的借口。
- analyze / Web 禁止为算换手或股本在热路径打竞价 HTTP。

---

## 3. 约束

### 3.1 禁止

- 把本产品代码合入 czsc 包，或把 czsc / misteryanalyze 当作本仓源码树改。
- 引入 CzscTrader、批量注册 czsc 信号、重写主前端为 Vue/FastAPI（除非规范本身被明确修订）。
- 并行维护第二套缠论识别。
- 在页面、扫描脚本、Excel 生成器中复制规则并改出另一套分数。
- 默认打开缠论混合分却仍声称与 1.22.30 金标一致。
- 用成交额列或竞价换手冒充 `turn`。
- 无有效换手时把 `chip_low_unknown` 显示或判成低位吸筹。
- 混合分关闭时，用年线预筛把综合分改成 0 或跳过 Mystery 规则。
- Web 与 `czsc-mi scan` cron 同时写 `scan_jobs`（须互斥；`--force` / `no_persist` 除外）。
- 跳过 migration 框架直接改生产表结构。
- 全市场强制重同步、清空生产库，除非任务明确要求。
- 提交 API Key、大型数据库文件、无意义的全量行情快照。
- 用成分股抽样计算板块强度。
- 为对齐页面效果而破坏分层或唯一入口。

### 3.2 范围边界

- 本规范不规定模块如何实现、不排期、不指定补齐顺序。
- 能力是否「已具备」以代码与 `DESIGN_DOCUMENT.md` 为准；未写进模型与入口的行为，不得当作对外契约。
- 与 misteryanalyze 对齐的是分析口径和必要产品能力，不是目录结构或页面一一复制。
- 覆盖率数字、cron 时刻、批大小、回填窗口属于设计与运营，不写入本文。

### 3.3 变更

必须先改本文再改代码：

- 分层、唯一入口、评分口径、复权锁定、板块强度定义；
- 两个缠论开关的含义或生产默认；
- `turn` / `chip_low` 的判定语义；
- 扫描写库的互斥与职责。

不算规范变更（不改 `AnalysisResult` 计分语义即可直接改代码）：

- 展示字段、报表排版、源适配；
- 分析缓存与缠论缓存的实现；
- 换手派生、覆盖率观测、股本低频刷新与除权触发。
