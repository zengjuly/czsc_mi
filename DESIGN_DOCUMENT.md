# czsc_mi 设计文档（DESIGN_DOCUMENT）

> 目标与原则、注意实现保留在 CLAUDE.md（唯一执行规范）；本文件记录落地后的工程上下文，供后续 agent 快速理解。

## 1. 概述

以 **czsc**（缠中说禅技术分析工具 v1.0.1，Rust/PyO3）为缠论识别内核，迁入
**misteryanalyze / stock_analyzer 1.22.30** 的 Mystery 规则（三振共振、主升浪、
平台、VAP-ATR、形态）、数据中枢、板块指数、扫描与日报/Web 的新产品。

- 包名：`mystery`；命令入口：`czsc-mi`；Python 3.12（venv：/home/ai/ai_runner/venv）
- 工作区：/home/ai/ai_runner/stock/czsc_mi（唯一可写仓，远端 zengjuly/czsc_mi，SSH 443）
- 只读参考：`/home/ai/ai_runner/stock/czsc`（czsc 源码）、`/home/ai/ai_runner/stock/stock_analyzer`（搬迁源）
- 生产库（只读复用）：`MYSTERY_DB_PATH=/home/ai/ai_runner/stock/data/db/mystery_cache.db`

## 2. 架构（硬约束）

```
apps (CLI/Web)
    → services.analyze / scan / sync     （唯一计算入口 analyze_one_stock → AnalysisResult）
        → core                            （纯函数 + dataclass，零 IO，禁止 import czsc）
        → adapters                        （唯一外部依赖：czsc / 扶摇 / tdx / sqlite）
```

- `mystery/core/**` 禁止 `import czsc`、禁止读网络、禁止读 DB（测试断言过）。
- 只有 `mystery/adapters/czsc_adapter.py` 可以 `import czsc`。
- 跨层只传 `mystery.core.models` 的 dataclass；禁止传递 CZSC/BI/ZS/RawBar 到 core/web。
- 一轮分析锁定一种复权 `adjust="qfq"`，写入 `BarSeries.adjust` 与 `source`。
- 板块强度只用 `sector_kline` 真实指数，禁止成分股抽样。
- Token 只走环境变量/配置注入（不入库）。

## 3. 数据模型（core/models.py）

`Bar`(dt/open/high/low/close/volume/amount/turnover/pct_chg) ·
`BarSeries`(symbol/freq/adjust/bars/source) · `ChanBi` · `ChanZs` ·
`ChanStructure`(freq/n_fx/bis/zss/last_bi_dir/last_bi_confirmed/in_zs/engine/engine_ver) ·
`MarketContext` · `MysteryBreakdown`(signal/resonance/main_wave/platform/vap_atr/patterns/checklist8) ·
`AnalysisResult`(symbol/name/trade_date/price/score/advice/true_resonance/mystery/chan/sector/financial/rule_ver/czsc_ver)

- `to_dict()` 保证 JSON 可序列化（datetime→iso，NaN→None）。
- `rule_ver` 固定 `"mystery-1.22.30-compat"`。

## 4. 数据流（services/analyze.py）

```
analyze_one_stock(symbol):
  daily  = market.fetch_bars(symbol, "1d")      # DB未过期→ths→tdx_local
  weekly = market.fetch_bars(symbol, "1w")      # 日K重采样（W-FRI, keep_latest）
  monthly= market.fetch_bars(symbol, "1M")      # 日K重采样（ME, min 10根）
  ctx    = build_market_context()               # 上证指数 + 主行业分 + 财务
  chan   = {}                                   # MYSTERY_CHAN_ENABLED=1 时
  bd     = run_rules(daily_df, weekly, monthly, ctx, chan)   # 指标加工→规则
  score, advice, true_res = scorer.combine(bd, chan)         # P1/P2 恒用 Mystery 原公式
```

- 指标加工：`core/indicators.enrich_indicators`（复刻 main.py `_calculate_all_indicators`
  全链：均线/排列/MACD/RSI/量比/换手率/量价/OBV）。
- 评分 = 共振评分×0.6 + 主升浪信号40×0.4（`comprehensive_signal_analysis`），
  与旧仓 1.22.30 完全一致。

## 5. 数据源与新鲜度（adapters/market.py）

优先级：**本地库(SQLite) 未过期 → ths_official → tdx_local**（tdx_api 预留）。

- 新鲜度参照：在线交易日历（akshare，TTL 600s，盘中 15:30 前回退昨日）
  → 本地 MarketDB 最新日 → .day 文件最新日。
- DB 过期 → ths_official：本地 MarketDB(DuckDB) 秒读 → 若 MarketDB 也滞后
  （新鲜度检查）→ fuyao.py 子进程在线拉最新（HITHINK key 走环境变量）。
- 指数：DB 优先（允许 3 天滞后，通达信未同步属正常）→ ths → tdx_local。
- 周/月：统一日K重采样（`resample_engine: mystery`），口径与旧仓 kline_resampler
  一致（W-FRI/ME、agg first/max/min/last/sum/sum/sum、min_bars 周3月10、最新周期豁免）。
- 换手率：ths 数据无换手率列（None，与旧仓一致）；tdx .day 无 → 从 SQLite 按日期补齐+ffill。

### 5.1 关键坑（2026-08-27 实测）

- **MarketDB（/home/ai/ai_runner/stock/Financial-API/data/market.duckdb）滞后于在线**
  （本地 08-21 vs fuyao 在线 08-26/27）：必须做新鲜度检查，否则用旧行情冒充当日。
- **fuyao 子进程有限流**：连续调用会空返回；ths 空 → tdx_local 兜底（.day 原始价，
  与 qfq 有细微差异，金标价格断言可能瞬时失败，重跑即可）。
- **DB 代码格式 `sh.600519`** vs 内部 `600519.SH`：统一走 `adapters/codes.db_code_of()`。
- **upsert_kline 用 COALESCE 保护 turn**：ths 数据 turn=None，直接覆盖会清掉
  baostock 同步的历史换手率（INSERT OR REPLACE 教训）。
- **旧系统实际"同花顺优先"**：fetch_daily 主源链 ths_official 第一，增量合并最后兜底；
  金标三只票全部来自 fuyao（DB 过期时）。

## 6. 缠论适配（adapters/czsc_adapter.py，P2）

- czsc 1.0.1（PyPI，作者 zengbin93；本地 czsc/ 为同源 Rust 版，无 wheel 需编译，
  直接用 PyPI 轮子）。API：`format_standard_kline(df, freq)`（列 symbol/dt/open/high/
  low/close/**vol**/amount）→ `CZSC(bars, min_bi_len)` → `bi_list/zs_list/fx_list`。
- BI：`direction`（中文"向上/向下"）、sdt/edt（datetime）、high/low；
  ZS：zg/zd/gg/dd/sdt/edt/is_valid/bis；`c.finished_bis` 判最后一笔是否确认。
- 只进 `AnalysisResult.chan`（freq→ChanStructure），**P4 起参与评分**：
  `S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan`；S_chan 缺省 50，
  有 1d 结构时按最新笔方向 ±10、中枢内 +5；**年线滤网未通过 → 混合分强制 0**
  （一票否决语义）。
- chan_cache：`store.chan_cache` 表（symbol/freq/trade_date/czsc_ver PK），
  行情日或 czsc 版本变化才失效。

## 7. 入口（P3）

- CLI：`czsc-mi analyze --stock sh600519 [--quick]`（输出 JSON）/
  `daily --limit 50 [--min-score]` / `scan --limit 100 [--min-score]` /
  `sync --period daily --days 365 [--symbols ...] [--limit]`
- Web：`streamlit run mystery/apps/web/app.py`（个股/扫描/板块钻取三视图，
  只调 Service；session 只存结果 dict；渲染与计算分离，不用 st.stop()）。
- verify：`python scripts/verify_unified_analysis.py` —— 个股/扫描/CLI 三路径
  score 差 ≤ 1 + 金标对比。

## 8. 测试与验收

- `pytest -q`：20 passed（models/core 合成 OHLC/czsc adapter mock K 线/金标 ≤ 1）。
- 金标 fixtures：`tests/fixtures/gold_{sh600519,sz000001,sh600150}.json`
  （由旧系统 `unified_stock_analysis` 生成，2026-08-27）。
- 三只票 score：0.0 / 49.0 / 0.0，与旧系统 0 分差（价格/日期/行业分全一致）。
- 数据源不可用时金标测试自动 skip（不阻塞离线 CI）。

## 9. 阶段状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 骨架/models/空Adapter | ✅ 0.1.0 |
| P1 | 规则迁入+金标（chan 关，分差≤1） | ✅ 0.2.0 |
| P2 | CzscAdapter 只展示（不进评分） | ✅ 0.2.0 |
| P3 | CLI/scan/sync/verify/Web 收口 | ✅ 0.3.0 |
| P4 | 小权重缠论分（0.55/0.25/0.20，年线滤网否决保护） | ✅ 0.4.0 |

P4 漂移验证（2026-08-28，20 只样本，同一份数据）：Top5 排序不变，
仅 up 笔股票分上移（sz000001 49→52.7，sz000651 22.8→34.0），否决股保持 0。

## 10. 运行环境

```bash
source /home/ai/ai_runner/venv/bin/activate   # 原机示例；任意 venv 均可
export MYSTERY_DB_PATH=/home/ai/ai_runner/stock/data/db/mystery_cache.db
export MYSTERY_CHAN_ENABLED=0        # 1=开启缠论混合分（默认关）
export HITHINK_FINANCE_API_KEY=...   # 环境已有；不入库
cd /home/ai/ai_runner/stock/czsc_mi
czsc-mi analyze --stock sh600519
```

## 11. 已知缺口（W1 记录，2026-08-28）

- 金标集成测依赖原机实盘数据（THS/TDX/生产 DB），已拆双层：离线 fixture 锁分（test_score_offline），
  集成测打标 @integration 默认跳过。
- `schema.sql` 已自举（幂等建表）；改列/迁移策略未实现（migrations/ 仅说明）。
- 缠论分仍较浅（末笔方向/中枢/日周同向，±10/±5/±8），非完整买卖点/背驰体系；默认关闭。
- 无 CI 之外的发布管道（无 wheel 构建/发布配置）。
- 在线源仅 ths_official + tdx_local 活跃；tdx_api 为接口占位（unused in default fallback）。
