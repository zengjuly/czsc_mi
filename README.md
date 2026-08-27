# czsc_mi — Mistery 趋势交易自动化分析（czsc 缠论内核）

以 **czsc**（缠中说禅技术分析工具，Rust/PyO3）为缠论识别内核，迁入
**misteryanalyze / stock_analyzer** 的 Mystery 规则（三振共振、主升浪、平台、
VAP-ATR、形态）、数据中枢、板块指数、扫描与日报/Web 的新产品。

## 架构

```
apps (CLI/Web/cron)
    → services.analyze / scan / sync   （唯一计算入口 analyze_one_stock）
        → core                          （纯函数 + dataclass，零 IO）
        → adapters                      （唯一外部依赖：czsc / 扶摇 / tdx / sqlite）
```

- `mystery/core/**` 禁止 import czsc、禁止读网络、禁止读 DB。
- 只有 `mystery/adapters/czsc_adapter.py` 可以 `import czsc`。
- 跨层只传 `mystery.core.models` 的 dataclass（BarSeries / ChanStructure / AnalysisResult …）。
- 一轮分析锁定一种复权 `adjust="qfq"`，写入 `BarSeries.adjust` 与 `source`。
- 板块强度只用 `sector_kline` 真实指数，禁止成分股抽样。

## 安装

```bash
source /home/ai/ai_runner/venv/bin/activate
pip install -e /home/ai/ai_runner/stock/czsc   # 或 pip install "czsc>=1.0.1,<2.0"
pip install -e "/home/ai/ai_runner/stock/czsc_mi[web]"
```

环境变量（只读，不入库）：

```bash
export MYSTERY_DB_PATH=/home/ai/ai_runner/stock/data/db/mystery_cache.db  # 复用现网生产库
export MYSTERY_CHAN_ENABLED=0                                             # 一期默认关
```

## 使用

```bash
czsc-mi analyze --stock sh600519
czsc-mi daily --watchlist
czsc-mi scan --limit 100
czsc-mi sync --period daily --days 365
```

## 测试

```bash
cd /home/ai/ai_runner/stock/czsc_mi && pytest -q
```

## 阶段

- P0 骨架（models / 包结构 / 空 Adapter 接口）✅
- P1 规则迁入 + 金标（关闭缠论，score 与 stock_analyzer 差 ≤ 1）✅
  - core 规则全量迁入（mystery_rules/indicators/resonance/platform/patterns）
  - 多源行情：DB(未过期) → ths_official(MarketDB→fuyao) → tdx_local
  - sh600519 / sz000001 / sh600150 三只金标 0 分差（fixtures/gold_*.json）
- P2 CzscAdapter 只展示（ChanStructure 进 AnalysisResult，不进评分）✅
  - czsc 1.0.1：日/周双周期笔+中枢抽取，chan_cache 缓存，NaN 拒绝
- P3 入口收口（CLI / daily / scan / verify_unified_analysis）
- P4 小权重缠论分（需用户确认）
