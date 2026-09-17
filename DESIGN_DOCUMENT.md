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
- 只进 `AnalysisResult.chan`（freq→ChanStructure）。**混合分 S_chan 已于 0.10.0（6C）
  替换为标定规则表（docs/010.md §9）**：无日线结构 50；末笔确认 up/down ±12；
  收盘价 vs 末中枢 above+8/in+2/below−8（老缓存回退 `in_zs`+5）；日周同向 ±8；
  czsc 买卖点标签 一/二/三买 +10、一/二/三卖 −10（「其他」/无标签 0，不猜）；
  底背驰 +10 / 顶背驰 −10；夹紧 [0,100]。权重 `0.55/0.25/0.20` 不变。
  **年线滤网未通过 → 混合分强制 0**（一票否决语义）。**生产默认 `chan.score: false`
  （MYSTERY_CHAN_SCORE 缺省 0），综合分 = Mystery 1.22.30 原公式**；混合分仅结构开+
  分开关同时为 1 才生效，该路径 `rule_ver=mystery-0.10.0-chan`（勿与 1.22.30 金标比
  绝对值）。买卖点/背驰标签仅由 `CzscAdapter.signal_flags` 在混合分路径生成，分关零开销。
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

- `pytest -q -m "not integration"`：213 passed（models/core 合成 OHLC/czsc adapter
  mock K 线/金标 ≤ 1/scan_signals 三类信号/缠论图 plot_figure/technical 快照/
  Excel 单汇总页回归 + CLI 默认 THS 环境注入回归 + 换手覆盖率 QA + 除权事件股本重拉；
  web 页面冒烟自 v0.10.14 起归入 integration 池：`pytest -m integration` 本机跑）。
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
| W24 | 扫描提速（006.md 阶段 4，不改年线语义）：`_analyze_chan` 非日频 series 由 `fetch_bars`（重复读日 K 再 resample）改为 `resample_bars(daily)` 复用主流程已取日 K；主流程 W15 起已 resample、scan worker 复用 AnalysisService+W23 缓存、不增线程池（GIL 无收益）。实测（scripts/w24_bench.py，50 只样本）：resample 路 vs fetch 路规则分差 max=0.000（验收 ≤1）；每票非日频数据获取 0.206s→0.056s，省 ~0.15s/票 ≈ 全市场 5562 只省 ~14min；125 离线测试全过 | ✅ 0.9.24 |
| W25 | 扫描写库职责单一（006.md 阶段 5）：角色分工——① sync/每日管线写 kline/财务/板块/turn 派生+失效 analysis_cache；② sync-shares（周日 cron）仅股本快照；③ `czsc-mi scan`（**18:00 cron 为扫描写库固定写手**）写 scan_jobs/scan_results（同类型只留最新）；④ Web 默认只读 job，点「扫描」与 cron 互斥——`scan_market` 入口 flock 文件锁（`<db>.scanlock` 记录持有者 pid+时间），并发第二方**拒绝不排队**（进程退出/崩溃自动释放，无残留状态）；`--force`/`no_persist` 不加锁；Web 前台按钮捕获 RuntimeError 显示原因。实测：5 项锁语义测试全过（外部进程探测拒绝/崩溃自动释放/no_persist 与 force 旁路）、130 离线测试全过 | ✅ 0.9.25 |
| W26 | 007.md 三项：a) 规范/文档对齐代码现状（AGENTS 核对无需改、DESIGN §11 两句旧文案更新、README 补 sync-shares/sync-turnover 命令、migrations/README.txt 更新为迁移框架已落地——纯文档）；b) 换手覆盖率可观测：`turnover_coverage()` QA（近 20 交易日 turn 覆盖/股本可派生率）+ 日报 Excel 页脚行与 HTML 头部 `.qa` 行 + `sync-turnover --backfill-days`（回填仅 CLI，不进管线）+ scan 摘要日志结构化；不改 chip_low 判定与综合分；c) `sync-shares --from-adjustments [--since]`：本地 MarketDB `raw_adjustment_events` 筛送转/增发事件票（无事件零 HTTP 秒回），事件票 ≤3% 也写新 as_of 锚点（unchanged_event 计数），对账状态文件 `<db>.shares_state` 供 since 默认值，插入 daily_pipeline 2/4 步。实测：新增 11 项离线测试、141 全过、金标三只分差 0 | ✅ 0.9.26 |
| W27 | 008.md 四项（不升 rule_ver、不改 chip_low 阈值、不回填 date<as_of、不开阶段6）：a) P0 覆盖率 QA 双口径——日报 QA 行分【自选】/【全市场】两行（自选口径才是 18:00 验收指标，实测自选 86/86 有快照 46.4% vs 全市场 86/5559 44.5%）；b) P3 管线可观测——daily_pipeline 2/3 步退出码写 `<output>/pipeline_status.json`，日报页脚与飞书正文附「股本刷新/换手派生」状态行（仍不阻塞 scan）；c) P2 事件筛子探测——raw_adjustment_events 实测无回购注销/解禁/类型列，保持送转+增发口径，缺口写入 §11；d) P1 周日铺快照——`sync-shares --fresh-skip-days N`（fresh 票跳过 HTTP、事件票豁免）+ `scripts/weekly_shares.sh` 周日 10:00 cron（先自选后全市场，断点重跑安全）。实测：142 项离线全过、金标三只分差 0 | ✅ 0.9.27 |
| W28 | 009.md 四项（约束同 006–008）：a) W28a QA 第二口径——`turnover_qa_stats` 增加 fillable_rows/filled_rows/fillable_coverage/as_of_min/p50/max/skipped_before_asof/no_shares（只统计「有快照且 date≥as_of 且 volume>0」的根，只读不改写入），日报每口径附「可填覆盖率 | 快照 as_of 区间 | as_of前空turn n」明细行（实测：可填 0/2 根、as_of前 1898 根 → 46.4% 缺口全为 as_of 挡住的合法空窗，非派生失败）；b) W28b 周脚本可失败——weekly_shares.sh 自选 <只数×0.9 或全市场 <`WEEKLY_SHARES_MARKET_MIN`(默认1000) → exit 1，写 `<output>/weekly_shares_status.json`，双口径 QA 入日志，周一日报页脚 7 日内显示「上周铺盘 ✅/❌ market_with_shares=N」；c) W28c 运维——crontab 周日 10:00 已挂，自选 `--backfill-days 20` 已人工执行（当前 as_of=09-14 晚于窗口内多数 K 线，86 根计 skipped，属预期）；d) W28d 事件表列集守护测试（新列出现即红、提示扩口径，不猜列名）。实测：145 项离线全过、金标三只分差 0 | ✅ 0.9.28 |
| W29 | 观测补丁（009 后续 §3，不改判定公式）：a) `turnover_qa_stats` 增 fillable_days/shortfall_to_20，日报可填行附「可填日从 as_of 起还差 N 个交易日才满 20 根」人话，防把 46% 当故障；b) `sync-turnover` 当日 calc_float=0 且存在可填未写根 → stderr ERROR + exit 1（进 pipeline_status turnover_derive=fail；回填模式合法豁免）；c) weekly_shares.sh 铺盘达标后自动 `--backfill-days 20`（纯本地、不越 as_of）。145 全过 | ✅ 0.9.29 |
| 6A | 阶段6A 缠论摘要加深（010.md，展示层，不进 `chan_score`、不升 `RULE_VER`）：`ChanStructure` 新字段 `zs_position`（close vs zd/zg，价格口径，替代时间口径 `in_zs` 展示）、`leave_zs`（末笔方向+价出 zg/zd）、`bi_stretch`（末笔幅度/末中枢高度）、`n_zs`/`last_zs_finished`（暴露已有）；`bs_flag`/`divergence` 空占位（czsc 信号机不进分关路径）。`chan_from_dict` 缺键容忍（老缓存兼容）。展示落点：Excel/HTML/Web 三处 +「未进综合分」标注。回归 148 全过；金标脚本 sz000001 因行情漂移（年线滤网 09-14 起破）失效，基线代码同样 0 分，非 6A 回归 | ✅ 0.9.30 |
| 6B+6C | 阶段6B 标定（`scripts/calibrate_chan.py`，30 只快照：旧表 70% 挤 40-60 区分度不足，新表拉开两端 meanΔ=7.1/maxΔ=16，信号标签 30/30 可生成、「其他」计 0 不猜）+ 6C 混合分路径新 `S_chan` 规则表（010.md §9：末笔 ±12、zs_position above+8/in+2/below−8、老缓存回退 in_zs+5、日周 ±8、买卖点 ±10、背驰 ∓10、夹紧 [0,100]；**权重 0.55/0.25/0.20 不动**）。`RULE_VER_CHAN=mystery-0.10.0-chan` 仅分开路径（缓存键/输出 rule_ver 按 mix 选，分关仍 1.22.30-compat 原值）；`CzscAdapter.signal_flags` 直调 czsc `_native.call_signal` 4 信号，仅分开路径生成（分关零开销，core 不 import czsc）。实测 sh600036 分关 42.0/分开 51.4 复算一致。AGENTS §2.5 修订落地（a64106b）。回归 149 全过 | ✅ 0.10.0 |
| W30 | 011.md 稳定化（缠论公式与默认开关冻结）：a) W30a DESIGN §6/§11 + README 与 6C 标定表对齐（删「±10/±5/±8 浅规则」旧文案，补「打开 SCORE=1 后 rule_ver=mystery-0.10.0-chan 勿与 1.22.30 金标比绝对值」）；b) W30b `signal_flags` 返回 (bs, div, signals_ok)，分开路径无标签 INFO/异常 WARNING（分关仍零开销），mock 单测 4 项（test_signal_flags.py）；c) W30c 回归 153 全过，分关/分开 rule_ver 实测（000690 分关 40.0=compat/分开 38.4=chan，600036 触发新日志 signals_ok=True），6D 报表列实核（Excel 列全、空 `-`、「未进综合分」标注、排序 Mystery）。d) W30d 换手为运营验收（18:00 cron + 周日铺盘），代码冻结 | ✅ 0.10.1 |
| W31 | 012.md（缠论侧继续冻结）：a) W31b 数据指纹——`MysteryDB.data_fingerprint()`（db_path/kline_max/as_of_max 只读），QA 行第一行固定 `db=… kline_max=… as_of_max=…`，K线滞后/空库/快照表空出 WARN（消灭手动 scan 打到旧库时「0 只快照=治理回退」误判），CLI scan/sync-turnover 启动打同款 `db=` 行；weekly_shares.sh 开始行 echo db=；新增 2 项 tmp-db 单测。b) W31c 18:00+周日验收清单入 012.md，判定逻辑零改动 | ✅ 0.10.2 |
| W32 | 013.md（综合分/缠论继续冻结，只读增量）：a) W32a `docs/theory_index.md` 全书 `##` 级标题 303 条三层归类（金标已覆盖 18/可标签 87/仅心法 198，`scripts/theory_index.py` 可重跑）；b) W32b `mystery/core/turnover_tag.py` 零 IO 纯函数——换手分档 unknown/absorb(3-5)/inflow(8-15)/heavy(≥25)/flee(≥70)/other + 趋势/情绪/混合/未知风格，Excel/HTML/Web 三处只读两列（缺 turn 恒「未知」，不进综合分、不动 chip_low 门）；c) W32c `MysteryDB.sector_coverage_stats()` 行业覆盖进 QA 行（实测自选 97.6%、全市场 90.5%，板块K线滞后单独显示）；d) W32d 换手运营沿用 012 验收。新增 12 项单测 | ✅ 0.10.3 |
| W33 | 数据一致性修复（用户「执行1 2 3」）：a) P0-1 `stock_kline_data` 清洗——备份 `backup_pre_clean_20260916.db`（integrity ok）后 `scripts/clean_kline_dupes.py` 删同日双行 10,206,456 条 + 归一长格式日期 116,471 条（21,553,144→11,346,688 行），清空 analysis_cache/chan_cache；根因=DuckDB 预同步写 Timestamp 带 ` 00:00:00` 后缀，PK 视长/短格式为不同行。b) P0-2 防回潮——`store/db.py` 新增 `_d()` 写入端日期归一，`upsert_kline`/`upsert_kline_many` 全部走它 + 单测 `test_upsert_date_normalized`；「DuckDB/SQLite 复权口径差」实证为双行污染窗口假象（v_daily_qfq factor≈0.997，OHLCV 逐行一致）。c) P1-3 可见性——`AnalysisResult` 新增 `bars_fingerprint`(前8位)/`data_source` 顶层字段（to_dict/缓存还原/缓存命中补齐三路径），Web 详情页头部 caption + 扫描表「指纹」列只读展示 | ✅ 0.10.4 |
| W34 | P0 背驰卖点展示（20260524 版第二章【用缠论的背驰理论寻找不同级别的卖点】，用户「1 2」+「不做分钟级」→ 降级为日/周双级别，零新增存储）：a) `core/bc_resonance.py` 零 IO 纯函数——日+周同向背驰→「日+周共振顶/底背驰」、反向双列、单向单列、无→空（单一实现，四方展示只读调用）；b) `_analyze_chan` 标签生成从「mix 且仅 1d」放宽为「chan 开时 1d+1w」（周线 = 既有 resample 派生，不新增取数路径，遵守双归一），标签随结构进 chan_cache；c) Excel/HTML/Web 扫描列 + 详情页加「背驰共振」展示，**不进 scorer、rule_ver 与冻结规则零改动**（chan_score 背驰分只读日线 divergence，周线标签纯展示）；d) 新增 6 项离线单测，回归 174 全过 | ✅ 0.10.5 |
| W35 | 深度审查复核后的真 P0 修复（外部 review 13 条逐项对源码核实，失实 6 条不采纳；只修属实 3 条，零规则/权重/展示改动）：a) **换手缺失语义**：`Bar.turnover` 默认 0.0→`Optional[float]=None`，`market._turn_opt` 按治理口径 (0,80) 清洗入库行（历史 96.9% 的假 0 → None），`_avg_turnover_20` 跳过 0/None——修复缺数冒充 0 系统性拉低 20 日均换手、污染 chip_low/换手标签（chip_low_unknown 由假阳性转真 unknown）；b) **engine unavailable 不落盘**：`_analyze_chan` 中 czsc 缺装的空结构不再写 chan_cache（当次返回+ERROR 照旧，下次自然重试），杜绝缺装机器把「不可用」固化进共享缓存；c) **chan_cache 随 K 线失效**：`_invalidate_analysis` 同事务追加 `DELETE FROM chan_cache WHERE symbol=?`，堵盘后补洞/回写同日 K 后旧笔/中枢/背驰结构仍被命中的洞（trade_date 键只挡新一天）。新增 9 项离线单测，回归 183 全过 | ✅ 0.10.6 |
| W36 | 审查 P1 三连修（零规则/权重/展示改动）：a) **扫描链禁在线补财务**：`build_market_context/analyze_one_stock` 增 `fill_financial` 参数，scan 进程/线程 worker 与 `daily --watchlist` 批处理传 False（实测库内 ROE 覆盖 5510/5518、watchlist 86 只全命中，逐只外呼纯属浪费限流额度），个股 CLI/Web 详情保持 True 兜底补齐并回填库；缓存键 flags 加 F 位，F=0/F=1 结果互不命中；b) **set_financial 列级合并**：整行 INSERT OR REPLACE → `ON CONFLICT DO UPDATE SET col=COALESCE(excluded.col, col)`，部分列 upsert 不再把 net_profit/eps_ttm/divid_cash 洗成 NULL；c) **bars_fingerprint 加重**：末根 3 值+根数 → 首根 dt + 中间根 close + 末根 dt/close/volume + volume 校验和 + 根数，堵换源重刷/中段补洞指纹不变的脏缓存命中。新增 6 项离线单测（含外呼计数守卫），回归 189 全过 | ✅ 0.10.7 |
| W37 | 大盘滤网 P1（**纯展示，不进 scorer、不改 rule_ver**，用户「规则不变只改展示」）：a) **锚选型实证**：候选全A锚 `ths_880008.TI`、中证全指 `000985.SH`、`000510.SH`、`932000.CSI` 经 fuyao index-historical 实测全部 **0 条**（对照组 000001.SH 14 条正常）；库内 sector_kline 也无任何 8800 系行——原设想「880008 走 sector 渠道落库」无数据源支撑，降级为**双锚近似全市场：上证 000001.SH + 深证成指 399311.SZ**（深市覆盖；**注：399311 实为国证1000，显示名已于 W41 更正**），两者都走既有 `MarketDataClient.fetch_index` 归一通道（会话缓存，每进程一次降级链），零旁路取数；b) `core/market_env.py` 纯函数零 IO——`anchor_env`（收盘/MA250/站年线/多头顺次排列 MA5>MA20>MA60>MA250/20日涨跌，<250 根→全 None 诚实 unknown）+ `market_env` 三态（全部站线+多头→多头；任一破线→警示；否则震荡；缺锚→大盘未知）；c) 展示接线四方只读同源：`daily`/`scan --report` QA 行追加 `[大盘滤网]` 摘要（Excel/HTML 多行 QA 机制已支持）、Web 扫描页 caption（10 分钟缓存）；大盘态**不进个股 payload/缓存**（避免缓存命中返回过期大盘，现算+会话缓存）。实测 2026-09-16：上证 3885 站年线非多头 / 深成指 4863 破年线 → verdict 警示。新增 6 项离线单测，回归 195 全过 | ✅ 0.10.8 |
| W38 | **环境变量集中控制收口**（用户原则：所有环境变量集中在 `~/.stockrc`，所有进程必须导入）：a) `mystery/config.py` 导入时 `load_stockrc()` 自动解析 `~/.stockrc` 的 `export` 行以 **setdefault** 语义注入 `os.environ`（已有 env 优先、显式注入不被覆盖；文件缺失静默跳过；值去外层引号）——裸环境启动的 Python（含被 cron/systemd 直接 `python -m` 拉起、未 source rc 的路径）从此零配置即可拿到全套变量，`load_config()` 的 `${VAR}` 展开不再依赖外部 shell。b) **修 systemd 分叉**：`czsc-mi-web.service` 原 `Environment=` 硬编码整套且 `TDX_VIPDOC_DIR` 指向不存在的 `/home/ai/ai_runner/stock/data/tdx_vipdoc`（与 `.stockrc` 的 `/mnt/new_tdx/vipdoc` 冲突）——删全部硬编码，改 `ExecStart=/bin/bash -c 'set -a; source ~/.stockrc; set +a; exec …'` 单一事实源；缠论双开关 `MYSTERY_CHAN_ENABLED/SCORE` 从 systemd 收编进 `.stockrc`。c) 8 个 shell 脚本（daily_feishu/daily_pipeline/weekly_shares/daily_report/start_web + calibrate_chan.py 等）此前已 `source .stockrc`，纳入回归。验证：`env -i` 裸环境 import `mystery.config` 后 `MYSTERY_DB_PATH`/`MYSTERY_CHAN_*` 全部到位；Web restart 后进程 `/proc/<pid>/environ` 含 5+ 关键变量、HTTPS 200。新增 3 项离线单测（subprocess 裸环境/setdefault 优先/缺文件静默），回归 198 全过 | ✅ 0.10.9 |
| W39 | **板块指数日K同步链路 + date_ms 时区错位修复**（sector_kline 停更排查引出）：a) **停更根因**：sector_kline 历史上只有读路径没有写路径（无 cron 维护），上游 fuyao index-historical 数据正常（881101 实测到 2026-09-16）；b) **归一同步链路**：`MysteryDB.upsert_sector_kline()`（唯一写入口，UPSERT 幂等）+ `services.sync_sector_kline()`（增量：库内断点前扩 days 重拉，走 `ThsClient.get_index_daily` 归一通道，零旁路 HTTP）+ CLI `sector-sync-kline`（`--full-since` 重建模式：先 `clear_sector_kline()` 清错位孤儿行再全量重灌，清单/名称在清空前取好）；c) **重大存量 bug 实证**：fuyao `date_ms` 是**北京时间零点的 epoch**，`pd.to_datetime(ms, unit='ms')` 按 UTC 解释 → **所有 fuyao 通道日期标签整体 -1 天**（prices-historical 探针实测 9-06/9-13 出周日标签；sector_kline 99006/502601=19.7% 周末行、个股 daily 58419 周末行 99.99% 与次日周一价格同源即此坑；修复前后指数 tail 从 9-15 → 9-16）。`ths.py` `get_daily` 在线兜底与 `get_index_daily` 两处统一改 `utc=True → tz_convert('Asia/Shanghai') → tz_localize(None)`；d) **数据修复**：sector_kline 整表备份（sector_kline_backup_w39.db）后 `--full-since 2023-01-01` 全量重灌 688 板块 **599488 行、0 失败、周末行=0**（注意 fuyao 单次窗口 ~900 条截断：2021 起 0 条、2023-01 起 899 条封顶）；个股 daily 周末漂移重复行 58419 删除（整行先备份 removed_daily_weekend，仅删有次日邻居者，8 行孤儿保留待甄别；monthly/weekly 周末标签属正常不碰）；e) **接入每日管线**：daily_pipeline.sh 1/5→5/5 重排，新增 `sector-sync-kline --days 15`（失败不阻塞，SECTOR_OK 写 pipeline_status.json）。缓存影响：fuyao 兜底渠道占比小，analysis_cache 按 bars_fingerprint 键控次日自然重算。新增 7 项离线单测（重建/增量/清单守卫），回归 205 全过 | ✅ 0.10.10 |
| W40 | **飞书日报接入大盘滤网 + 指数行数据修复（W39 收尾）**：a) `scripts/feishu_notify.py` 日报正文第二行后插入 `[大盘滤网]` 行，复用 `market_env_summary/market_env_line` 单一实现走 fetch_index 归一通道（零旁路取数，失败 print 跳过不阻塞发送），补齐 W37 四方展示最后一方；b) **W39 后续数据修复**：date_ms 漂移经 fetch_index 在线回写同样污染 `stock_kline_data` 指数行——`sh.000001` 2015-2025 初混入平安银行股价值（早期代码归一串写）+2023-09 后 575/588 行 -1 漂移、`sz.399311` 574/588 漂移、`sz.399001/06` 值净但停更三周；三步制修复：3279 行备份 index_rows_backup_w39.db（行数断言守卫）→ DELETE → 走归一 fetch_index 重灌，终验 4 指数至 9-16、周末行=0、逐值对上上游；c) analysis_cache(92)/chan_cache(184) 经用户授权清空（删前 jsonl 留档 + 前后计数断言）；d) sector-sync-kline 实跑增量 686/688（2 板块上游真无数据，空返回属正常兜底），sector_kline 终态 599488 行/688 板块/周末行=0 | ✅ 0.10.11 |
| W41 | **外部深度审查复核 + 当天三修（零规则语义改动）**：外部 review 18 条逐条对源码核实——**15 条实锤**；2 条修正口径（#2 `_avg_turnover_20` 上界：`Bar.turnover` 构造时已过 `_turn_opt`，实际暴露面小，属口径一致性加固而非 P0 污染；#6 AnalysisCache get/put **均有 try/finally close，无连接泄漏**，真问题只剩绕过 `db._lock`，降 P2）；#1 改法有实证分歧：399001 上游停更三周（W39 实测），**不能换码只能改名**。本轮落地三件：a) `market_env.ANCHORS` 第二锚显示名「深证成指」→**「国证1000」**（399311.SZ 实为国证1000；docstring/analyze 说明/DESIGN W37 行同步纠偏），四方展示（CLI/Web/Excel-HTML/飞书）随常量自动纠正；b) `_avg_turnover_20` 显式 `0 < f < 80`（与治理口径同源，防旁路构造 Bar 带脏值进均值）；c) `data_fingerprint` 补 `try/finally: conn.close()`（全文件唯一漏点，WAL 下防句柄堆积）。新增 `tests/test_w41_review_fixes.py` 3 项（锚名断言/turn>80 与 80 边界剔除/指纹连接关闭 spy），回归 **208 全过**（205+3）。**同日第二批（本周项收口）**：d) Web 图缓存 `_plot_cache`/`_lightweight_cache` 键加 `data_ver`（详情页传 `trade_date|指纹前8`，与分数同源换代）+ `ttl=3600`，堵「盘后 sync 出新 K、进程不重启永远画旧中枢」与「分和图对不上」；e) 飞书 `feishu_notify.py`：`_send` 失败 → **exit 1**（cron 不再永远绿；未配置 webhook 仍按文档语义跳过），`MYSTERY_DB_PATH` 显式配置但文件不存在 → exit 1（防静默建空库报成功），job 选取改**优先当日 watchlist**（旧 `ORDER BY id DESC LIMIT 1` 不分类型会让全市场扫描后 Top8 变自选）；f) `daily_pipeline.sh` 开头 `export TZ=Asia/Shanghai`（`sync-turnover --date "$(date '+%F')"` 防 UTC 机器错一天）；g) **代码归一**：`db._dot` 复制的正则/推断分支删除，改为委托 `codes.normalize_symbol`（保留宽容回退：解析失败原样大写），同时 **BJ 号段补全 43/83/87**（旧只认 92，430/83x/87x 无前缀被误标 SZ → 扶摇/tdx 全链路错码；显式前缀仍优先于号段推断）；h) 删 `config.yaml` scoring 死配置段（留注释指路 scorer.py）；i) `make_cache_key` docstring 补 F 位；j) 负缓存 `_EMPTY_TTL` 1800→600s。测试增至 **209 全过**（+BJ 号段/归一/回退断言）。未修清单（下轮）：bs 标签先到先得、RSI=SMA 口径与缺数当 0、日 K close 缺失填 0（改动大需金标对拍）、AnalysisCache 绕 db._lock、两套 MACD 图注、周线「含未完成周」标题标注。**同日第三批（零语义风险项收口）**：k) `AnalysisCache` get/put/invalidate 整段入 `db._lock`（RLock 可重入，上层持锁不死锁；锁保护完整事务而非仅取连接，WAL 下与 upsert_kline 串行）；l) 缠论图标题加双注记——「MACD: czsc 口径（首值种子+柱×2），与规则分口径不同」（#10 不强行对齐、只标口径）+ 周/月图「含未完成周/月」（#12）；m) `analyze._warn_once`（模块级去重，每进程一次）：大盘锚/指数/行业获取失败、财务在线补齐失败从 debug 升级 warning（#18，生产 WARNING 级下管线日志可见环境级故障）；n) `scan._warn_fail_rate`：失败率>2% 且样本≥20 → warning（进程/线程池两条路径）。新增 4 项测试（锁 spy/一次性告警/失败率阈值/图标题断言），回归 **213 全过**。剩余（需用户定口径或金标对拍）：bs 标签优先级、RSI 口径与缺数当 0、缺 close 丢棒。**同日第四批（定口径三项收口，v0.10.13）**：o) **#9 bs 标签**改「三函数全收集候选 → 三>二>一 优先级」（czsc `call_signal` 返回快照值、Signal 无时间字段，无法按日期取最近，结构次序即时间次序等价物；`chan_score` 对一/二/三买卖统一 ±10，同向换档零分差，仅罕见「一买+三卖」混杂翻转符号且只作用 chan mix 路径=生产默认关）；p) **#4 缺 close 丢棒**：`_df_to_series` OHLC 任一 NaN/非数值→丢该根+warning（先侦查：生产库 close/open/high/low NULL或0 **全为 0 行**，纯在线源加固、零现库漂移；volume/amount 缺仍填 0——停牌行本就 0 量，amount NULL/0 合计 975 万行（约 86%）属数据源常态不碰）；q) **#11 RSI**：`calculate_rsi` 加 docstring 锁口径（SMA 均值版与 1.22.30 金标同源，改 Wilder 须升 rule_ver 重对金标——保持不改公式）；核查发现「缺数当 0」在唯一消费点 checklist 已有 `pd.notna` 双守卫（rsi_now/rsi_5ago），review 的「新股被当超卖」不成立（新股走 len<6 的「RSI数据不足」分支），只补缺数路径的详情标注「RSI缺失（数据不足，不按0处理）」。**自查纠错**：#11 首版误加 `dropna().tail(80)` 想「源头收口」——pandas 列赋值按 index 对齐，赋回照样落 NaN 无效，且误删临时列清理语句、注释凭空发明「W24 历史限制」，已回退为仅 docstring。金标对拍（管线 env）：sz000001 新代码 18.0 vs gold 49.0 不一致，**git stash 基线对拍同 18.0=行情漂移非回归**，三票三路径 max_diff 全 0。回归 **216 全过**（+优先级/丢棒/RSI缺失 3 项） | ✅ 0.10.13 |
| W42 | **CI 中断修复（v0.10.14）**：GitHub Actions 自 run15（`7ec2c64`, 08-30）起连续 54 次全挂——根因：该提交新增 `tests/test_web_pages_smoke.py` 顶层 `from streamlit.testing.v1 import AppTest`，而 CI 只装 `.[dev]`（无 streamlit）→ 收集期 ImportError（3.10 Failed 18s + fail-fast Cancel 3.11），与 W41 各批代码改动无关（job 明细：install success、test step failure）。修复：a) 该文件 `pytestmark = integration` + `pytest.importorskip("streamlit.testing.v1")` 双保险（单加标记不够——标记只挡运行不挡收集期 import）+ web app 符号改 `_app_mod()` 延迟导入；b) 次生雷预防：`test_w38_stockrc` 裸环境 subprocess 断言 `~/.stockrc` 注入，CI runner 无此文件 → 加 isfile skip 守卫；c) 依赖本机 sibling Financial-API 的 `test_cli_default_ths_env` 本已有 isdir skip 无需动。默认离线集 216→**207 passed**（9 个 web 冒烟转入 integration 池），本机 `-m integration` 该池 9/9 过 | ✅ 0.10.14 |
| W42b | **Web 访问鉴权（v0.10.15）**：服务直接暴露 18501 于公网（HTTPS 自签 cert），无任何认证。新增应用层登录门 `mystery/apps/web/auth.py`：凭据 PBKDF2-HMAC-SHA256(20万迭代)+随机 salt 存 `~/.config/czsc_mi/web_auth.json`（chmod 600，仿 feishu_webhook 文件先例，不进 git）；**文件存在即启用**，缺失或 `MYSTERY_WEB_AUTH=0` 即关闭（CI/integration 冒烟零影响）；会话态 `st.session_state["_auth_ok"]`（同连接 rerun 不掉线，刷新需重登）；连错 8 次锁 60 秒；密码 <8 位拒写。管理入口 `scripts/web_auth_init.py`（getpass 交互 / `--password-env` 非交互）。`main()` 首行 `require_login() or st.stop()`，未登录零业务渲染。测试：离线 6 测（哈希往返/坏凭据/env 关闭/init 端到端 600 权限），AppTest 实境三验（拦截出登录页/错密码拒/对密码放行导航渲染）；离线集 207→**213 passed**，CI 裸环境 186 passed+7 skip | ✅ 0.10.15 |
| W42c | **跨标签页免重登（v0.10.16）**：鉴权上线后「扫描结果→详情」LinkColumn 新标签=全新 session，又要求输密码（session_state 不跨标签）。方案：详情链接内嵌 HMAC 签名 token `&at=<exp>.<sig32>`（密钥 sha256("czsc_mi_auth_token:"+凭据hash)，不可反推密码；7 天时效；require_login 验签通过即放行并 `del query_params["at"]` 摘除地址栏 token）。已知暴露面：token 留在浏览器历史/新标签首帧 URL——本机池内网个人站，接受（DESIGN 记录）。裸模式守卫：`st.runtime.exists()` False 时放行（pytest 里 import app 会残留 form 上下文炸掉后续 AppTest，实测踩坑）。冒烟适配：_make 预置 `_auth_ok`、详情链接断言改前缀+`&at=` 与鉴权开关一致性断言。验证：token 单测 8 全过（签发/篡改/过期/伪造拒绝），AppTest 四场景（链接内嵌有效 token/带 token 免登录/无 token 拦截/伪造拒绝），integration 9/9，离线 207→**215**，CI 裸环境 188+7skip | ✅ 0.10.16 |
| W43 | **假 0 换手治理收口（v0.10.17）**：实测全库 `turn=0 且 volume>0` 历史行 1096 万（baostock/DuckDB 缺数以 0 填充），QA 口径 `turn IS NOT NULL` 把假 0 算「有值」（近 20 日覆盖率虚高至 99%），ffill 拿假 0 当锚点传播 15864 行。四件套：①写端 `db._t()`（0<t<80 有效，否则 NULL→COALESCE 保旧值）接入 upsert_kline/upsert_kline_many 防回潮；②ffill 锚点口径同源（sync_turnover._ok）；③turnover_coverage/turnover_qa_stats 诚实化（假 0 不算覆盖/已填）；④scripts/clean_turn_zeroes.py 存量清洗（UPDATE→NULL 与读端 _turn_opt 语义等价，无信息损失，dry-run 实测 10960565 行）。规则层本已防假 0（_turn_opt/_avg_turnover_20 跳 0），本次改动不改任何打分/标签/阈值语义。回归 tests/test_w43_turn_zero_guard.py 4 项。|
| W44 | **output_dir() 配置回退修复（v0.10.18）**：`config.output_dir()` 裸调用时 docstring 承诺的 config report.output_dir 回退从未生效（`cfg or {}`），直接落到不存在的 `<repo>/output` → weekly_shares 铺盘成功后写状态崩溃 exit 1、日报页脚 pipeline/铺盘验收两行长期静默缺失。修复为 cfg 省略时 `load_config()`；env 覆盖优先级不变。配套：全市场股本铺盘实跑成功（89→5547/5559 只，快照 as_of=2026-09-17），为 calc_float 换手回填补齐数据源。回归 219 passed。 |
| W45 | **baostock 历史换手回填 + 日K 2000 条滚动上限（v0.10.19）**：① `scripts/backfill_turn_baostock.py` 用 baostock 日K `turn` 真值回填 NULL 换手（按 query_stock_basic type='1' 过滤指数防污染；断点续跑 ckpt；3-worker 并发+错峰登录重登，实测服务端上限 ~3 会话；update 用 `date=?` 精确匹配命中 PK 索引，替代 substr 全表扫提速 190 倍）。范围按用户指令收窄至近一年（2025-09-17 起）：3090 票 / 740832 行 / 18.3min；近30日有效换手覆盖 0.42%→**94.9%**，近1年 94.3%。② 每票日K最多 2000 条滚动替换（用户指令）：`db._prune_daily()` 挂 upsert_kline/upsert_kline_many 同事务钩子（仅 daily；MYSTERY_DAILY_K_MAX_ROWS 可关），`scripts/prune_daily_kline.py` 存量剪除实跑 3284 票/1253427 行（0.7min，备份→apply→验收 0 超限→VACUUM 回收磁盘）。回归 223 passed。 |

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
- 迁移框架已落地（W21）：`_init_db` 按文件名序执行 `mystery/store/migrations/001–003`，
  记账表 `schema_migrations`，语句级执行、容忍重复列。改列/加表一律走迁移文件。
- 缠论混合分（0.10.0/6C 起）已用 30 只快照标定规则表替换旧浅规则（±10/±5/±8），
  含 czsc 买卖点/背驰标签（仅 `MYSTERY_CHAN_SCORE=1` 路径进分，「其他」标签计 0）；
  生产默认关，综合分仍 Mystery 1.22.30。打开后 `rule_ver=mystery-0.10.0-chan`，
  勿与 1.22.30 金标比绝对值。权重 0.55/0.25/0.20 冻结。
- 无 CI 之外的发布管道（无 wheel 构建/发布配置）。
- scan 三类信号中 `chip_low` 依赖近20日均换手：turn 来源 = 官方/legacy 原值优先，
  空值由本地股本快照派生（`sync-turnover`，W22/W26b）；无有效分母保持
  `chip_low_unknown`（不伪造）。覆盖率 QA 见日报「换手20日覆盖率/低位未知只数」，
  W27a 起按【自选】/【全市场】双口径分列（自选口径才是 18:00 验收指标）。
- 除权事件筛子（W26c `--from-adjustments`）事件类型以映射表为准：本地 MarketDB
  `raw_adjustment_events` 实测仅有 dividend_per_share/per_share_bonus/
  allotment_ratio/allotment_price 列（2026-09 探测，57232 行），**无回购注销/
  解禁/事件类型列** → 现只认送转+增发两类；此类股本变化依赖周日全市场
  `sync-shares`（W27d）兜底。上游若加列再扩 WHERE，不猜列名硬筛。
- tdx_api（tdx-api 容器）已实现并挂进 fallback，但容器未运行时会快速失败降级，
  不影响主链（db → ths_official 正常时不会触达）。
