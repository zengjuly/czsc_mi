# Hermes 开发指导：czsc_mi

把本文当作唯一执行规范。目标是在空仓 **czsc_mi** 上建成产品：以 **czsc 为缠论内核**，迁入 **misteryanalyze（本地名 stock_analyzer）** 的数据中枢、Mystery 规则、板块指数、扫描与日报/Web，且业务不绑死 czsc 内部对象。

---

## 0. 路径与角色

| 路径 | 角色 | 允许的操作 |
|------|------|------------|
| `/home/ai/ai_runner/stock/czsc_mi` | **唯一工作区**，新产品仓 | 创建、修改、提交全部发生在这里 |
| `/home/ai/ai_runner/stock/czsc` | czsc 源码参考 + 本地可编辑安装 | **只读参考**；不要改它来实现业务 |
| `/home/ai/ai_runner/stock/stock_analyzer` | misteryanalyze 1.22.x 金标与搬迁源 | **只读复制**；不要在旧仓加新功能 |

绝对禁止：
- 在 `czsc/` 里加 Mystery、扶摇、Streamlit、扫描
- 在 `stock_analyzer/` 里继续堆功能
- 把 czsc 或 stock_analyzer 做成 git submodule 当运行时源码树（依赖用 pip/editable install）

Python 环境沿用现有：`/home/ai/ai_runner/venv`（3.12）。文档里的命令默认：

```bash
source /home/ai/ai_runner/venv/bin/activate
cd /home/ai/ai_runner/stock/czsc_mi
```

---

## 1. 任务目标 / 非目标

### 必须达成

1. 同一只股票：个股页 / daily / 扫描 / 板块钻取 **分数一致**（误差 ≤ 1）。
2. 缠论识别只来自 czsc（分型/笔/中枢/多级别），不自研第二套识别。
3. Mystery 规则独立：三振、主升浪、平台、VAP-ATR、形态。
4. 行情本地库为权威；在线源可插拔：`ths_official → tdx_api → tdx_local`。
5. 对外唯一计算入口：`mystery.services.analyze.analyze_one_stock()` → `AnalysisResult`。
6. `MYSTERY_CHAN_ENABLED=0` 时，评分与 stock_analyzer 1.22.30 兼容。
7. 输出详细设计文档，便于后续的agent理解工程上下文及开发
8. 本文方案执行完后，对应的方案移至设计文档，但目标及原则、注意实现在此文档保留。

### 一期不要做

- 不把代码合进 czsc 包
- 不上 `CzscTrader`、不注册大批 czsc signals
- 不把 Streamlit 换成 Vue/FastAPI 主前端
- 不并行维护 easy_tdx / 自研 chanlun 识别
- 不把 HITHINK / Tushare token 写入仓库

---

## 2. 目标目录（必须按此创建）

```
/home/ai/ai_runner/stock/czsc_mi/
├── pyproject.toml
├── README.md
├── config/
│   └── config.yaml
├── mystery/
│   ├── __init__.py
│   ├── core/                 # 零 IO，禁止 import czsc / data 客户端
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── mystery_rules.py
│   │   ├── platform.py
│   │   ├── patterns.py
│   │   ├── resonance.py
│   │   └── scorer.py
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── codes.py          # 代码/周期/复权归一
│   │   ├── market.py         # 多源 + 缓存
│   │   ├── sector.py
│   │   ├── ths.py            # 扶摇
│   │   ├── tdx_api.py
│   │   ├── tdx_local.py
│   │   └── czsc_adapter.py   # 唯一允许 import czsc 的分析适配
│   ├── store/
│   │   ├── __init__.py
│   │   ├── db.py
│   │   ├── schema.sql
│   │   └── migrations/
│   ├── services/
│   │   ├── __init__.py
│   │   ├── analyze.py        # 唯一分析入口
│   │   ├── scan.py
│   │   └── sync.py
│   └── apps/
│       ├── cli.py
│       └── web/              # Streamlit，只调 Service
├── tests/
│   ├── test_core_rules.py
│   ├── test_models.py
│   ├── test_czsc_adapter.py
│   ├── test_unified_score.py
│   └── fixtures/
└── scripts/
    └── verify_unified_analysis.py
```

包名：`mystery`。命令入口：`czsc-mi`。

---

## 3. 依赖与安装

`pyproject.toml` 要求：

- Python `>=3.10`
- `czsc>=1.0.1,<2.0`（优先 editable：本地 `/home/ai/ai_runner/stock/czsc`）
- `pandas>=2.0`、`pyyaml`、`openpyxl`
- optional：`web = ["streamlit"]`

安装：

```bash
pip install -e /home/ai/ai_runner/stock/czsc
pip install -e "/home/ai/ai_runner/stock/czsc_mi[web]"
```

若本地 czsc 未打 wheel，必须 editable，保证 `import czsc; czsc.__version__` 可用。

环境变量（只读，不入库）：

```bash
# 已有生产库则复用，便于金标对比
export MYSTERY_DB_PATH=/home/ai/ai_runner/stock/data/db/mystery_cache.db
export HITHINK_FINANCE_API_KEY=...   # 已在 mystery-web.env，不要复制进仓
export MYSTERY_CHAN_ENABLED=0        # 一期默认关
```

新仓默认库路径：`/home/ai/ai_runner/stock/czsc_mi/data/mystery_cache.db`（可用环境变量覆盖）。

---

## 4. 架构硬约束（违反即返工）

```
apps (CLI/Web/cron)
    → services.analyze / scan / sync
        → core（纯函数 + dataclass）
        → adapters（唯一外部依赖）
            → czsc / 扶摇 / tdx / sqlite
```

1. `mystery/core/**` 禁止 `import czsc`、禁止读网络、禁止读 DB。
2. 只有 `mystery/adapters/czsc_adapter.py` 可以 `import czsc`。
3. 跨层只传 `mystery.core.models` 中的 dataclass，禁止传递 `CZSC`、`BI`、`ZS`、`RawBar` 到 core/web。
4. Web/CLI/scan **禁止**直接调 `MysteryLogic()` 或自己拉日K 算分。
5. 一轮分析锁定一种复权：`adjust="qfq"`，写入 `BarSeries.adjust` 与 `source`。
6. 板块强度禁止成分股抽样；只用 `sector_kline` 真实指数。
7. Token 只走环境变量。

---

## 5. 必须实现的数据模型

写在 `mystery/core/models.py`，字段名保持稳定（Web/Excel/扫描都靠它）：

```python
Bar
BarSeries          # symbol, freq ("1d"|"1w"|"1M"), adjust, bars, source
ChanBi             # direction, sdt, edt, high, low, confirmed
ChanZs             # zg, zd, gg, dd, sdt, edt, n_bi, finished
ChanStructure      # freq, n_fx, bis, zss, last_bi_dir, last_bi_confirmed, in_zs, engine, engine_ver
MarketContext      # index_bars, industry_name, industry_score, industry_up, financial
MysteryBreakdown   # resonance, main_wave, platform, vap_atr, patterns, checklist8
AnalysisResult     # symbol,name,trade_date,price,score,advice,true_resonance,
                   # mystery, chan: dict[freq, ChanStructure], sector, financial,
                   # rule_ver, czsc_ver
```

`AnalysisResult.to_dict()` 必须 JSON 可序列化（datetime 转 iso）。

`rule_ver` 初期固定 `"mystery-1.22.30-compat"`。

---

## 6. 从旧仓搬什么、怎么搬

源根目录：`/home/ai/ai_runner/stock/stock_analyzer`

**复制后必须改 import、去 IO、对齐模型。不要整文件当运行入口。**

| 源文件 | 迁到 | 改造 |
|--------|------|------|
| `analysis/mystery_logic.py` | `core/mystery_rules.py` | 去掉一切数据客户端；入参 DataFrame/`BarSeries` |
| `analysis/adaptive_platform.py` | `core/platform.py` | 同上 |
| `analysis/pattern_recognition.py` | `core/patterns.py` | 同上 |
| `analysis/resonance_analyzer.py` | `core/resonance.py` | `calculate_industry_score_from_sector` 改为吃板块 K 线 DataFrame，不自己查库 |
| `analysis/stock_pipeline.py` | **参考后重写**为 `services/analyze.py` | 不要原样拷；必须产出 `AnalysisResult` |
| `data/ths_client.py` | `adapters/ths.py` | 保持 fuyao 子进程行为；列标准化 |
| `data/tdx_api_client.py` | `adapters/tdx_api.py` | 代码带交易所前缀 |
| `data/tdx_local_client.py` + incremental/gbbq | `adapters/tdx_local.py` | 指数豁免换手率/落后检查 |
| `data/market_data_client.py` | `adapters/market.py` | 统一出口 `BarSeries` |
| `data/kline_resampler.py` | `adapters/market.py` 或 `adapters/resample.py` | 周月默认用这套，配置 `resample_engine: mystery` |
| `data/db_manager.py` + `data_engine.py` | `store/db.py` | schema 可复用现网库 |
| `data/sync_all_market.py` | `services/sync.py` | CLI 调用 |
| `data/run_market_scan.py` | `services/scan.py` | 只调 `analyze_one_stock(include_detail=False)` |
| `web/` | `mystery/apps/web/` | 页面只调 Service；session 只存结果 dict |
| `config/config.yaml` | `config/config.yaml` | 去掉机器绝对路径硬编码，改环境变量 |
| `scripts/verify_unified_analysis.py` | `scripts/` | 改为调新 Service |

不要迁：`simple_demo.py`、过时 mock 主路径、baostock 活动链（代码可留 adapters 兜底但默认不启用）。

---

## 7. 各层实现要点

### 7.1 adapters/codes.py

统一：

- `sh600519` / `600519.SH` / `SH600519` / `sh.600519` → 内部 `600519.SH`
- 给扶摇：`600519.SH`
- 给 tdx-api：`SH600519` 或文档要求的 `SH`+6 位
- 给 tdx 本地：`sh600519`
- 给展示：保留名称表

周期：`日线|daily|1d` → `1d`；周 `1w`；月 `1M`。

### 7.2 adapters/market.py

```text
fetch_bars(symbol, freq, start, end) -> BarSeries
fetch_index(...) -> BarSeries
fetch_stock_list() -> list[{code,name}]
```

顺序：本地库未过期 → ths_official → tdx_api → tdx_local。  
指数：无条件先 tdx_local，跳过换手率与“落后 3 天内”重拉。  
周/月：日 K 重采样（一期不要用两套周期口径）。

### 7.3 adapters/czsc_adapter.py

```python
class CzscAdapter:
    def analyze(self, series: BarSeries) -> ChanStructure
    def analyze_multi(self, daily: BarSeries, freqs: list[str]) -> dict[str, ChanStructure]
```

步骤：
1. `Bar` → `czsc.format_standard_kline` / `RawBar`（tz-naive，拒绝 NaN OHLCV）
2. `CZSC(bars)`
3. 抽取 `bi_list`/`zs_list` 为 `ChanBi`/`ChanZs`
4. 写入 `engine="czsc"`, `engine_ver=czsc.__version__`
5. 读 `CZSC_MIN_BI_LEN` 等环境，不改 czsc 源码

`MYSTERY_CHAN_ENABLED=0` 时 Service **不调用** Adapter。

参考 czsc API：`/home/ai/ai_runner/stock/czsc` 的 README 与 `czsc.utils.plotting.lightweight.plot_czsc`。

### 7.4 services/analyze.py（核心）

伪代码必须遵守：

```python
def analyze_one_stock(symbol: str, include_detail: bool = True) -> AnalysisResult:
    daily = market.fetch_bars(symbol, "1d")
    weekly = market.fetch_bars(symbol, "1w")
    monthly = market.fetch_bars(symbol, "1M")
    ctx = build_market_context(symbol, daily)  # 指数 + 行业分 + 财务
    chan = {}
    if chan_enabled():
        chan = czsc_adapter.analyze_multi(daily, ["1d", "1w"])
    breakdown = rules.run(daily, weekly, monthly, ctx, chan.get("1d"))
    score, advice, true_res = scorer.combine(breakdown, chan)
    return AnalysisResult(...)
```

一期评分：`chan.enabled=false` 时 **只用 Mystery 原公式**，保证金标。  
二期再加：

`S = 0.55 S_mystery + 0.25 S_resonance + 0.20 S_chan`  
`S_chan` 缺省 50。`true_resonance` 布尔口径不改。

### 7.5 store

优先复用 `MYSTERY_DB_PATH` 现网库，避免重同步 5000+ 只股票才能开发。  
新增表（若无则 migration）：

```sql
CREATE TABLE IF NOT EXISTS chan_cache (
  symbol TEXT, freq TEXT, trade_date TEXT, czsc_ver TEXT,
  payload_json TEXT,
  PRIMARY KEY(symbol, freq, trade_date, czsc_ver)
);
```

行情日变化或 `czsc_ver` 变化才失效。

### 7.6 Web

从 stock_analyzer 的 `web/` 搬页面，但：

- 删除页面内拉数/算分
- `st.session_state["stock_analysis"] = result.to_dict()`
- 渲染与计算分离（已有 1.22.13–18 的教训：不要把结果放在 `if st.button` 里）
- 缠论卡片只读 `result["chan"]`
- K 线：有 chan 时尝试 czsc `plot_czsc` HTML；否则沿用旧图

---

## 8. 分阶段交付（按顺序，未完成不进入下一阶段）

### P0：骨架（第 1 天）

- 建包、pyproject、README、models、空 Adapter 接口
- `pytest tests/test_models.py` 通过

### P1：规则迁入 + 金标（关闭缠论）

- 迁 core 规则
- `analyze_one_stock` 可跑单票
- 用现网库对 `sh600519`、`sz000001`、`sh600150` 跑分
- 与 stock_analyzer 的 `analyze_one_stock` / `unified_stock_analysis` 对比，score 差 ≤ 1
- `MYSTERY_CHAN_ENABLED=0`

把旧系统输出存到 `tests/fixtures/gold_*.json`。

### P2：CzscAdapter 只展示

- 日线/周线结构进 `AnalysisResult.chan`
- 个股页展示笔方向、确认、zg/zd
- **不进评分**
- `tests/test_czsc_adapter.py`：用 mock K 线或 fixture，不断网也可测转换逻辑

### P3：入口收口

- CLI：`czsc-mi analyze --stock sh600519`
- CLI：`czsc-mi daily` / `czsc-mi scan` 调同一 Service
- `scripts/verify_unified_analysis.py`：同股三路径评分差 ≤ 1
- Web 三处（个股/板块钻取/前台扫描）全部改新入口

### P4：小权重缠论分（可选，需用户确认后再做）

- 打开开关与权重
- 对比扫描 TopN 漂移，过大则降权

---

## 9. CLI 约定

```bash
czsc-mi analyze --stock sh600519
czsc-mi daily --watchlist
czsc-mi scan --limit 100
czsc-mi sync --period daily --days 365
```

实现：`mystery.apps.cli:main`，click 或 argparse 均可。

---

## 10. 测试与验收

最少用例：

1. `test_core_rules.py`：不 import czsc，给定合成 OHLC，主升浪/平台函数有确定输出。
2. `test_czsc_adapter.py`：BarSeries → ChanStructure 字段齐全；NaN 开高低收应失败或过滤。
3. `test_unified_score.py`：chan 关闭时与 fixture 差 ≤ 1。
4. `verify_unified_analysis.py`：茅台/银行/船舶三只。

验收清单：

- [ ] `cd czsc_mi && pytest -q` 核心测试通过
- [ ] chan 关闭时三入口同分
- [ ] chan 开启时页面有结构、评分默认仍可关
- [ ] 仓库无 API Key、无大型 db
- [ ] 未修改 `/home/ai/ai_runner/stock/czsc` 业务文件
- [ ] 未在 `/home/ai/ai_runner/stock/stock_analyzer` 提交新功能

---

## 11. 配置模板（config/config.yaml）

```yaml
db_path: ${MYSTERY_DB_PATH}
data_source:
  primary: ths_official
  fallback: [tdx_api, tdx_local]
  adjust: qfq
  resample_engine: mystery
chan:
  enabled: false          # 读环境 MYSTERY_CHAN_ENABLED 可覆盖
  freqs: [1d, 1w]
scoring:
  mystery: 0.55
  resonance: 0.25
  chan: 0.20
```

---

## 12. 给 Hermes 的工作方式

1. 先 `ls` 三个目录，确认 czsc_mi 为空或仅有 git 初始化。
2. 读 `stock_analyzer/analysis/stock_pipeline.py`、`web/utils/analysis_service.py`、`data/market_data_client.py`、`DESIGN_DOCUMENT.md` 顶部与 4.47–4.49 节再动手。
3. 每次只提交一个阶段；commit message 用中文或英文均可，但说明阶段号。
4. 遇到扶摇/网络失败：用现网 SQLite 缓存继续开发，不要卡死等 API。
5. 不确定 czsc 1.0 API 时，打开 `/home/ai/ai_runner/stock/czsc/README.md` 与 `docs/examples/01_quick_start.py`，用 `format_standard_kline` + `CZSC`。
6. 不要运行全市场强制 `--force` 同步，除非用户明确要求。
7. 输出结束时列出：已创建文件、如何跑通单票、未完成项。

---

## 13. 一句话任务

在 `/home/ai/ai_runner/stock/czsc_mi` 从零建包；**复制并改造** `stock_analyzer` 的规则与数据层；**依赖但不修改** `czsc`；用 Adapter 出缠论摘要；用唯一 Service 出分。先兼容 1.22.30，再打开缠论展示。

