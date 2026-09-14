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
  chan   = {}                                   # MYSTERY_CHAN_ENABLED=1 时（默认开，仅结构展示）
  bd     = run_rules(daily_df, weekly, monthly, ctx, chan)   # 指标加工→规则
  score, advice, true_res = scorer.combine(bd, chan)         # 默认 Mystery 原公式；混合分需另开 MYSTERY_CHAN_SCORE=1
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
- 只进 `AnalysisResult.chan`（freq→ChanStructure）。**P4 公式已落地**：
  `S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan`；S_chan 缺省 50，
  有 1d 结构时按最新笔方向 ±10、中枢内 +5；**年线滤网未通过 → 混合分强制 0**
  （一票否决语义）。**生产默认 `chan.score: false`（MYSTERY_CHAN_SCORE 缺省 0），
  综合分 = Mystery 1.22.30 原公式**；混合分仅结构开+分开关同时为 1 才生效。
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
  W7 起扫描结果表/真三振池下方可「下载 Excel 报告」（同 daily 格式；web
  扫描落库缺明细，下载时按需补详情 `_enrich_scan_rows`，`excel_bytes`
  生成 bytes 经 `st.download_button` 下载）；W14 起 Excel 为单「汇总报告」
  页：取消个股详情 sheet，全部个股详情并入汇总列，并新增年线过滤系统
  各条件达成情况列（来自 `mystery.signal['年线条件']`，analyze 阶段逐项判定）。
  W10 起扫描结果表「详情」列超链接带 `nav_key/nav_idx`，点击直达个股页并
  恢复「上一只/下一只/返回列表」导航；W10-fix4 起导航列表存进程级缓存
  `mystery/apps/web/nav_cache.py`（LinkColumn 新标签页 = 全新 session，
  session_state 不共享，必须进程级共享；TTL 30 分钟，nav_key 过期/未知时
  退化为独立查看不报错）；无 `nav_key` 的直达链接（收藏/外部）不显示导航。
  W10-fix 起扫描结果表增加判定列：年线滤网/周线锚定/破五反五/主升浪8项
  （年线滤网等来自 `mystery.signal`；主升浪8项来自 `checklist8.满足数量`，显示
  纯数值 int 便于表格过滤/排序；只展示不改判；表格数据构建抽为纯函数
  `_scan_table_data`，渲染与计算分离）。
- verify：`python scripts/verify_unified_analysis.py` —— 个股/扫描/CLI 三路径
  score 差 ≤ 1 + 金标对比。

## 8. 测试与验收

- `pytest -q -m "not integration"`：99 passed（models/core 合成 OHLC/czsc adapter
  mock K 线/金标 ≤ 1/scan_signals 三类信号/缠论图 plot_figure/technical 快照/
  web 页面冒烟 + 后台任务仓库跨 rerun 持久回归 + Excel 单汇总页回归 +
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
| P4 | 小权重缠论分公式（0.55/0.25/0.20，年线滤网否决保护；生产默认关 `score: false`） | ✅ 0.4.0 |
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
| W9-fix | 板块成分同步能力补齐（修复「板块钻取：创新药 ths_886015 暂无成分股」）：czsc_mi 缺写 `stock_sector_rel` 的入口（存量 9000+ 行系旧仓迁来，只覆盖 881/884 行业板块；388 个 ths_ 概念板块无成分）。新增 `ThsClient.fetch_constituents`（index-constituents，入参 ths_886015/886015/886015.TI 归一 → 886015.TI，fuyao 只认 .TI，重试 3 次 timeout=120）+ `MysteryDB.upsert_stock_sector_rel/upsert_sector_meta/ensure_sector_meta`（stock 归一 sh.600519、sector 归一 .TI；概念成分 is_primary=0 不覆盖主行业；ensure 不覆盖已有板块名）+ `sync_sector_constituents` 服务 + CLI `czsc-mi sector-sync --sector <code>`；web 钻取空成分提示给出确切同步命令。北交所 920xxx.BJ 被内部代码归一拒绝（仅 SH/SZ），跳过不中断。实测 886015 创新药 277 成分 → 268 落库，get_sector_stocks 可查；新增 5 个离线单测 | ✅ 0.9.6 |
| W10 | 扫描结果「详情」超链接导航：链接带 `nav_key/nav_idx`，进入个股页恢复「上一只/下一只/返回列表」；列表存 session `<key>_nav_rows`，无 nav_key 的直达链接保持独立查看；新增 2 个 AppTest 回归（恢复导航/无导航） | ✅ 0.9.7 |
| W10-fix | 扫描结果表增加判定列：年线滤网/周线锚定/破五反五/主升浪8项（年线滤网等来自 `mystery.signal`，主升浪8项来自 `checklist8.满足数量` 显示 N/8；只展示不改判；表格数据构建抽纯函数 `_scan_table_data`，渲染与计算分离） | ✅ 0.9.8 |
| W10-fix2 | 扫描结果表「主升浪」列改为「主升浪8项」显示满足数量（来自 `checklist8.满足数量`，与 Excel/HTML/个股页口径一致） | ✅ 0.9.9 |
| W10-fix3 | 扫描结果表「主升浪8项」改为纯数值 int（去掉 N/8 文本），便于表格过滤/排序；缺失显示空 | ✅ 0.9.10 |
| W10-fix4 | 扫描结果「详情」导航列表从 session_state 改存进程级缓存 `mystery/apps/web/nav_cache.py`（LinkColumn 新标签页 = 全新 session 不共享 state；TTL 30 分钟，过期/未知 nav_key 退化为独立查看不报错） | ✅ 0.9.11 |
| W11 | 定时管线去重与提速：①下线重复 crontab `daily_stock_report.sh`（与新 daily_feishu.sh 双管线 18:00 并发跑同一 SQLite，互相锁竞争致 86 只耗 100 分钟；xlsx 链接+git push 职责并入新管线）②`czsc-mi daily` 逐股串行改 `--workers`（默认 4）ThreadPoolExecutor 并发，DB 层已有 `_lock` 线程安全，实测稳态 21min→5min ③feishu_notify 补 xlsx GitHub raw 下载链接（quote 编码）④daily_pipeline 末尾 git push 报告 | ✅ 0.9.12 |
| W12 | 18:00 管线提速（62min→约13min，冷场景实测）：①`_batch_presync_from_duckdb` 增量化——旧实现 5222 只逐股全历史拉取+全行 upsert 写 4.2GB SQLite（约50分钟黑盒，日志不可见），改为「DuckDB 一次 GROUP BY 全市场 MAX(date) 预筛 → 只对非最新票查增量行 `date > cache_last` → 只 upsert 增量行」，冷场景实测 10.6min，并加 INFO 耗时日志（写入只数/检查数/耗时）②修 `MysteryDB.get_financial` 永远返回空的 bug——`report_date`（'2026-2' 字符串）被塞进 `float()` 抛 ValueError 被 except 吞掉，86 只自选每天逐股 2 次在线补财务；修复后本地 ROE 直接命中（600938 roe=10.2 验证）③`fetch_index` 会话级缓存（`_index_cache`+锁，仅无 start/end 切片时）——daily 86 只逐股 build_market_context 共享一次指数获取；④`fetch_index` ths 分支改调新增 `ThsClient.get_index_daily`（fuyao `index-historical` 专用接口）——旧调 `get_daily`（prices-historical）对指数代码返回空，每天逐票白等 0.8s 后降级 tdx_local（指数停在 09-04 旧数据）；修复后 source=ths_official 且指数数据更新到最新交易日 | ✅ 0.9.13 |
| W12b | 18:00 管线 sync 再提速（2026-09-11 生产实测 62min→7.3min）：`_batch_presync_from_duckdb` 从「逐股查 DuckDB 增量（~0.14s×5222≈12min 冷场景）→ 逐股 upsert_kline」改为「一次全市场增量查询（`WHERE date > 全局基准 min_last`，DuckDB 内聚合秒级）+ pandas groupby 按各票 cache_last 过滤增量 + `upsert_kline_many` 单大事务 executemany（5200 事务 7.9s→0.05s）」；db.py 新增 `upsert_kline_many`。生产实测 sync 32s + daily 86 只 6.5min（ThreadPool 4，CPU 175% 受 GIL 限制，为当前唯一瓶颈）+ git/飞书 11s；降级失败股（次新无数据）由行情链负缓存秒级返回 | ✅ 0.9.14 |
| W13 | daily 提速（86 只 6.5min→68.7s，2026-09-11 实测）：①`czsc_adapter._to_df` dt 批量一次 `pd.to_datetime`——旧逐根调用 `pd.to_datetime(b.dt)` 约 2945 次/票、占单票分析 40%+ 耗时；列表一次性转换走 C 向量化，单只 sh600519 5.96s→0.76s（cProfile）②`czsc-mi daily` 共享 `AnalysisService` 实例——旧模块级 `analyze_one_stock` 每只 new service（重复初始化 SQLite 连接/指数/日历缓存），4 线程并发实测独立 svc 慢 3 倍+（10 只 27s→8.8s，86 只 390s→68.7s）。金标三只改前后分数/建议/价格逐字段一致；`_to_df` 字符串与 datetime 输入输出一致性单测通过 | ✅ 0.9.15 |
| W14 | Excel 单汇总页：取消个股详情 sheet（原 `_detail_rows`/导航/代码列超链接删除），个股详情全部以列并入「汇总报告」（section 前缀防跨段同名冲突，与核心列同义项不重复，冻结首行+表头加粗）；新增年线过滤系统各条件达成情况列（收盘价>MA250 / 收盘价>MA60 / 均线多头排列 / MA5·10·20·60>MA250，来自 `signal['年线条件']`——`mystery_rules._basic_filter_checks` 逐项判定写入，`basic_filter` 旧签名与判定语义不变，报表只读不改判）；web 下载与 daily 落盘共用 `_build_buffer` 同时生效；`tests/test_excel_hyperlinks.py` → `test_excel_summary.py`（单 sheet/年线列/详情列/无超链接回归）；pytest.ini testpaths 修 JSON 列表写法（`["tests"]` → `tests`，消除 warning） | ✅ 0.9.16 |
| W15 | 全市场扫描提速（实测单只 ~0.9s、5222 只串行 ~80min）：①`MarketDataClient.resample_bars`——周/月由已取日 K 直接重采样派生，`analyze_one_stock` 不再重复读库（旧 `fetch_bars('1w'/'1M')` 内部再读一遍完整日 K 5038 根+重采样，每只 ~0.3s 白花；新旧路径周/月数据与评分逐字段一致）②`scan_market` 并行：ThreadPool 实测 GIL 完全串行无收益（32 只 29.7s vs 串行 29.3s）→ 改进程池（fork，默认 4 进程，batch=16 切成 300+ 任务均衡分发；冷缓存场景实测 2x，首次全市场预计 ~50min）；Pool 创建/运行异常自动回退线程池；`MYSTERY_SCAN_WORKERS` env 可调；拆分 `_scan_worker`/`_scan_thread_worker`/`_persist_or_return`，min_score 过滤在 worker 内（进度按批精确回报），进程/线程结果一致性验证通过 | ✅ 0.9.17 |
| W16 | 扫描结果表增加年线系统与主升浪具体指标列：年线 7 项条件（价>MA250 / 价>MA60 / 均线多头 / MA5·10·20·60>250，来自 `signal['年线条件']`）+ 主升浪 8 项指标（长期横盘 / MA60向上 / 突破平台 / 放量2倍 / 回踩+金叉 / RSI>50 / 资金流入 / 板块走强，来自 `checklist8`），✅/❌ 展示、缺失为空，只读不改判；新增 `_yl_flag`/`_mainwave_flag` + `_YEARLINE_DISP`/`_MAINWAVE_DISP`；test_scan_table_shows_signal_flags 扩展新列与值映射断言 | ✅ 0.9.18 |
| W17 | 每日任务改「定时触发后台扫描自选股 + 生成 Excel 报告」：`czsc-mi scan` 新增 `--report`（扫描落库后生成 Excel/HTML 日报，文件名与 daily 一致 `每日股票分析报告_YYYYMMDD.xlsx/.html`，飞书 xlsx 链接与 git push 段零改动）；`--limit` 默认逻辑改为 `--watchlist` 时全自选（原默认 100 会截断自选）、全市场仍防呆 100；`daily_pipeline.sh` 的 2/2 由 `daily --watchlist`（只出报告不落库）改为 `scan --watchlist --report`——自选股走 scan_market 落 `scan_jobs/scan_results`（Web 真三振池/扫描页可查，feishu_notify 本就读 scan_jobs + 最新 xlsx）。真实验证：86 只自选落库 job#27、Excel 单汇总页 65 列/87 行 | ✅ 0.9.19 |
| W18 | 扫描历史同类型只保留最新一份：`scan_jobs` 加 `scan_type` 列（schema.sql + `_init_db` 旧库自动 ALTER 补列，幂等）；`_write_scan_batch` 先删同类型旧 job 及其 results 再插新；`scan_market` 加 `scan_type` 参数（None 自动推断：watchlist→'watchlist'、universe→'sector'、否则 'market'）；Web 板块钻取传 `sector:{板块名}`（不同板块各一份）；「最近扫描任务」显示 `[类型]` 标签。存量历史 28 条 market（迁移默认值）在下次全市场扫描时自动收敛为 1 条；新增 3 个离线测试（同类型覆盖/类型隔离/旧库迁移） | ✅ 0.9.20 |
| W19 | Web 稳定性：①`start_web.sh` stop 加固——TERM → 等 `STOP_TIMEOUT`（默认 8s）→ 仍存活 KILL -9 → 最终确认失败报错并保留 PIDFILE 不假成功；按命令行 pgrep 兜底收集进程（PIDFILE 失效/多实例残留也能杀干净）；status 遇 PIDFILE 失效自动修复。②systemd 用户服务 `czsc-mi-web.service`（崩溃自动拉起 Restart=always + 开机自启 enable+linger；环境变量对齐 start_web.sh+.stockrc；模板同步 scripts/）——事故复盘：2026-09-13 Web 被 SIGKILL（约 6h 运行后）导致无法访问，旧进程 D 状态占端口致 restart 假成功。③`start_web.sh` 检测 systemd 已启用时提示改用 `systemctl --user`（`SYSTEMD_FORCE_LEGACY=1` 可强制旧逻辑）。实测：kill 主进程后 NRestarts=1 自动拉起、HTTP 200 | ✅ 0.9.21 |
| W20 | 文档与开关对齐（006.md 阶段 0）：明确两个独立开关——结构展示 `MYSTERY_CHAN_ENABLED` 默认开、混合分 `MYSTERY_CHAN_SCORE` 默认关（综合分 = Mystery 1.22.30）。README 环境变量拆两个、删「CHAN_ENABLED=0 即关分」；DESIGN §4/§6/§9/§10、AGENTS §2.5、scorer.py docstring 统一为「P4 公式已落地、生产默认 score:false」；`daily_pipeline.sh`/`daily_feishu.sh`/`start_web.sh`/systemd service 显式 export 两个开关。验收：未设 env 时 chan_enabled()=True、chan_score_enabled()=False（实测通过），102 离线测试全过 | ✅ 0.9.22 |
| W21 | schema migration 框架（006.md 阶段 1）：`MysteryDB._init_db` 执行 schema.sql 后按序应用 `migrations/*.sql`；记账表 `schema_migrations(id, applied_at)`，语句级执行、容忍 duplicate column（新库幂等）；零散 ALTER（W18 scan_type）收编为 001；新增 002 `analysis_cache`（symbol+trade_date+cache_key 主键，含 rule_ver/chan 开关/czsc_ver/bars_fingerprint）、003 `float_share_snapshot`+`stock_kline_data.turn_source`。测试 `test_store_migrations.py`：新库/旧库迁移保历史/重复初始化幂等 | ✅ 0.9.22 |
| W22 | 换手率治理（006.md 阶段 2）：`core/turnover.py` 纯函数（turn=volume/float_shares×100、手→股、0<turn<80 越界丢弃、股本=流通市值/last_price、>3% 跳变才覆盖、近端洞 ≤5 日 ffill）；`adapters/ths.get_auction_snapshot`（final 批 100 只，auction_turnover_pct 禁用）；`services/sync_shares.py`（低频：--force 初始化/周对账）；`services/sync_turnover.py`（每日仅派生：当日空 turn→calc_float、legacy/official 绝不覆盖、无股本保持 unknown、覆盖率 QA）；CLI `sync-shares`/`sync-turnover`；`daily_pipeline.sh` 1/3→2/3→3/3 加派生步骤；股本周对账 cron（周日 09:30 自选 86 只）。实测：86 只快照初始化成功、9-11 行 as_of(9-14)>当日不回填（守规范）、测试 9 项全过 | ✅ 0.9.22 |
| W23 | 分析结果缓存（006.md 阶段 3）：`store/cache.py`——`bars_fingerprint`（根数+末根 dt/close/volume 的 sha1）、`make_cache_key`（adjust+rule_ver+归因串 D/C/S+czsc_ver 的 sha1）、`AnalysisCache.get/put`（payload=to_dict JSON）；`analyze_one_stock(use_cache)` 头读尾写、`_result_from_payload` 还原 AnalysisResult（含 MysteryBreakdown/嵌套 dict 明细，综合分经 combine 复算一致）；sync 写 K 线（`upsert_kline`/`upsert_kline_many`）同事务 DELETE 该票 analysis_cache（点号格式归一）；scan 链 env `MYSTERY_SCAN_CACHE` 门控（默认开）。设计修正：epoch 曾入键，因 analyze 在线降级自动落库会自触发失效抖动而移除，改为写入即删。实测：现网二次调用 0 次重算（命中）、关键字段一致、125 离线测试全过（新增 11 项缓存回归） | ✅ 0.9.23 |

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
export MYSTERY_CHAN_ENABLED=1        # 1=缠论结构展示（默认开）
export MYSTERY_CHAN_SCORE=0          # 1=缠论混合分（默认关，综合分=Mystery 1.22.30）
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
