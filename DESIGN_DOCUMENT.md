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

优先级：**本地库(SQLite) 未过期 → ths_official → tdx_api → tdx_local**（W2-A 接入 tdx-api 容器）。

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
- **缠论图 W4 起 plotly 自绘**：`CzscAdapter.plot_figure(series)` → plotly Figure
  （K线 + MA 5/10/20/55/233/610 + 分型虚线/笔实线 + 中枢矩形 ZG-ZD/GG-DD +
  成交量 + MACD(12,26,9,×2，czsc 首值种子 EMA 口径)）。弃用
  `czsc.utils.plotting.lightweight.plot_czsc`（其 MainPane 只画 SMA5/20 且不画
  中枢区间，无法满足展示需求）。MA/MACD 在全集 K 线上计算再截尾窗（长周期
  均线有暖机值）；涨红跌绿，配色沿用 czsc 历史主题。`plot_html` 保留为
  `plot_figure(...).to_html(include_plotlyjs="cdn")` 的兼容壳。

## 7. 入口（P3 + 002.md W1-W2）

- CLI：`czsc-mi analyze --stock sh600519 [--quick]`（输出 JSON）/
  `daily --watchlist [--symbols ...] [--limit] [--min-score]`（写
  `{OUTPUT_DIR}/每日股票分析报告_{YYYYMMDD}.xlsx/.html`，Excel 评分=analyze score）/
  `scan --limit 100 [--min-score] [--signal vap_atr|chip_low|true_resonance] [--no-persist]`
  （逐票 `analyze_one_stock(include_detail=True)` + `core.scan_signals.classify` 三类信号，
  写 `scan_jobs`/`scan_results`，同日重复扫描默认新 job）/
  `sync --period daily [--period weekly] --days 365 [--symbols ...] [--limit] [--force]`
  （断点 `data/sync_checkpoint.json`，参数变化丢弃旧断点，中断再跑跳过已完成；
  周/月由日K重采样写入；证券列表为空报错退出）
- Web：`streamlit run mystery/apps/web/app.py`（七视图：个股/自选/扫描/板块钻取/
  真三振池/系统状态/板块强度表，只调 Service；session 只存结果 dict；
  渲染与计算分离，不用 st.stop()；个股页 chan 开启时 plotly 自绘缠论图，
  支持 日线/周线/月线 切换，`st.plotly_chart` 原生渲染；W5 起「分析明细」
  展示 均线排列/破五反五/量价/筹码/换手率/多周期，财务补 EPS/股息/利润率；
  W6 起个股/自选输入改名称搜索 selectbox（`_stock_pick_options` 缓存全市场），
  全市场扫描与板块钻取支持后台扫描（线程任务 `_bg_launch`，进度自动刷新，
  结果落 `scan_jobs`/`scan_results`），系统状态与扫描页可查最近任务结果；
  W7 起扫描结果表/真三振池下方可「下载 Excel 报告」（汇总+每只个股详情，
  同 daily 格式；web 扫描落库缺明细，下载时按需补详情 `_enrich_scan_rows`，
  `excel_bytes` 生成 bytes 经 `st.download_button` 下载）。
- verify：`python scripts/verify_unified_analysis.py` —— 个股/扫描/CLI 三路径
  score 差 ≤ 1 + 金标对比。

## 8. 测试与验收

- `pytest -q -m "not integration"`：91 passed（models/core 合成 OHLC/czsc adapter
  mock K 线/金标 ≤ 1/scan_signals 三类信号/缠论图 plot_figure/technical 快照/
  web 页面冒烟 + 后台任务仓库跨 rerun 持久回归 + Excel 超链接导航回归 +
  CLI 默认 THS 环境注入回归）。
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
| W1 | 报表（Excel/HTML，daily 落盘）+ 扫描三类信号写库 | ✅ 0.5.0 |
| W2 | sync 断点/多周期 + tdx_api 接入 + Web 真三振池/系统状态/板块强度表 | ✅ 0.5.0 |
| W3 | plot_czsc 嵌入个股页 + Excel 缠论列 + daily_pipeline.sh + 去绝对路径 | ✅ 0.5.0 |
| W3-perf | indicators 逐行索引→numpy 向量化（712x，新旧 57 列逐元素一致）；web 名称搜索/侧栏列表加缓存；个股分析 66s→5s | ✅ 0.5.0 |
| W4 | 个股页缠论图增强：plotly 自绘（K线+MA 5/10/20/55/233/610+分型/笔+中枢矩形+量+MACD）；日/周/月切换；chan 摘要补月线 | ✅ 0.6.0 |
| W5 | 个股页补齐技术面明细：均线排列/破五反五/量价/筹码/换手率/多周期（technical 快照，仅展示）；财务补 EPS/股息/利润率；换手率缺失不伪造 | ✅ 0.7.0 |
| W6 | Web 交互升级：个股/自选输入改名称搜索 selectbox（缓存全市场）；全市场扫描/板块钻取支持后台扫描（线程任务+进度自动刷新+结果落库）；系统状态与扫描页新增「最近扫描任务与结果」查询入口 | ✅ 0.8.0 |
| W7 | 扫描报告下载：扫描结果/真三振池下方「下载 Excel 报告」按钮（汇总 sheet + 每只个股详情 sheet，与 daily 的 `write_excel` 同格式）；web 扫描落库缺 VAP/平台明细，下载时按需 `include_detail=True` 补详情再生成 | ✅ 0.9.0 |
| W7-fix | 后台扫描任务仓库从模块级 dict 改为 `st.cache_resource` 进程级 store（Streamlit 每次 rerun 重跑脚本顶层，模块级 dict 被重置 → 后台任务全丢"暂无后台任务"；改为跨 rerun/session 共享同一对象后任务/进度/结果保留），新增跨 rerun 持久回归测试 | ✅ 0.9.1 |
| W8 | Excel 报告导航对齐 misteryanalyze fa1444ff：汇总报告「代码」列超链接 → 对应个股 sheet A1（`Hyperlink.location` 内部引用，避免外部 target 补全文件路径）；个股 sheet 第 1 行导航 首页(→汇总报告)/前一页/后一页（跟随 results 顺序，首尾无对应链接）；daily 落盘与 web 下载共用 `_build_buffer` 同时生效；新增 `test_excel_hyperlinks.py` 回归（无死链校验） | ✅ 0.9.2 |
| W8-fix | tdx_api 交易所判定 bug：内部格式 `600000.SH` 因 `startswith('sh')` 恒 False 被误判为 BJ → 请求 BJ600000 永远返回空 → 降级链 ths→tdx_api→tdx_local 全空、sync 卡死。改用 `codes.to_tdx_api()` 统一归一（兼容 600000.SH/sh600000/SH600000），指数 `/api/index` 判定同步修正 | ✅ 0.9.2 |
| W8-env | czsc-mi CLI 入口自动补 THS 默认路径：未设 `THS_FUYAO_SCRIPT`/`THS_MARKETDB_DIR` 时从仓库 sibling（`../Financial-API`）推导注入（路径存在才设，不写死单机绝对路径；须在 `load_config()` 展开 `${THS_...}` 前调用）；新增 `test_cli_default_ths_env.py` 回归 | ✅ 0.9.3 |
| W8-fix2 | sync 写库 key 不一致导致数据永远"过期"：sync.py 用 `code if '.' in code` 原样保留内部格式 `600010.SH` 写库，而读取走 `db_code_of()=sh.600010` → 每次写入新行、读取看到旧行，新鲜度检查永远失败。改用 `db_code_of()` 统一；market.py 降级链加新鲜度择优（ths 落后参照日不再短路，继续尝试 tdx_api/tdx_local，fuyao 晚发布一天时自动取更新源）；清理历史脏格式行 | ✅ 0.9.4 |
| W9 | 主升浪满足数量修正：checklist 统计含 `平台范围`(dict, truthy) 被误计 → 满足数量+1，改只统计 8 项布尔指标。财务数据链路：analyze 本地库缺 ROE 时走 fuyao 在线补齐（`valuations-snapshot` PE/PB + `financials-indicators` 扣非加权ROE/毛利率/净利率，最近已披露季度优先）并回填 `set_financial`，下次命中缓存；db.py 增 `set_financial` upsert | ✅ 0.9.5 |

P4 漂移验证（2026-08-28，20 只样本，同一份数据）：Top5 排序不变，
仅 up 笔股票分上移（sz000001 49→52.7，sz000651 22.8→34.0），否决股保持 0。

性能修复（2026-08-29）：`indicators.py` 9 个热点函数原用 `.iloc[i]['col']`/`.loc[i, col]`
逐行访问，pandas 3.0 Arrow 后端下单票 enrich 达 90s+。已全部改为 numpy 数组向量化
（递推类 OBV 用 numpy 循环），新旧实现对照 57 列全等（含旧版 down 条件用 i-1 MA20
的不对称逐字复刻）。坑：pandas Series 与 numpy 混算会按 index 并集对齐（up 变 2669 行），
必须统一转 numpy 数组。web 端 `_stock_pick_options`（全市场名称列表）加
`st.cache_data(ttl=600)`，避免每次 rerun 无缓存拉全市场列表。

## 10. 运行环境

```bash
source /home/ai/ai_runner/venv/bin/activate   # 原机示例；任意 venv 均可
export MYSTERY_DB_PATH=/home/ai/ai_runner/stock/data/db/mystery_cache.db
export MYSTERY_CHAN_ENABLED=0        # 1=开启缠论混合分（默认关）
export HITHINK_FINANCE_API_KEY=...   # 环境已有；不入库
export THS_FUYAO_SCRIPT=/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py
export THS_MARKETDB_DIR=/home/ai/ai_runner/stock/Financial-API/data
cd /home/ai/ai_runner/stock/czsc_mi
czsc-mi analyze --stock sh600519
```

本机路径约定全部收敛在 `scripts/start_web.sh` 与 `scripts/daily_pipeline.sh`
（脚本内允许默认值）；`mystery/` 与 `config/` 业务文件零绝对路径（验收：`rg -n "/home/ai/ai_runner" mystery config` 为空）。

## 11. 已知缺口

- 金标集成测依赖原机实盘数据（THS/TDX/生产 DB），已拆双层：离线 fixture 锁分（test_score_offline），
  集成测打标 @integration 默认跳过。
- `schema.sql` 已自举（幂等建表）；改列/迁移策略未实现（migrations/ 仅说明）。
- 缠论分仍较浅（末笔方向/中枢/日周同向，±10/±5/±8），非完整买卖点/背驰体系；默认关闭。
- 无 CI 之外的发布管道（无 wheel 构建/发布配置）。
- scan 三类信号中 `chip_low` 依赖近20日均换手：ths/tdx 数据换手率常缺 → 大多标
  `chip_low_unknown`（不伪造）；tdx 数据按 SQLite 日期补齐后可恢复。
- tdx_api（tdx-api 容器）已实现并挂进 fallback，但容器未运行时会快速失败降级，
  不影响主链（db → ths_official 正常时不会触达）。
