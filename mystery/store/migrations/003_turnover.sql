-- 阶段 2（006.md W22）：换手率治理 schema。
-- 股本低频快照：float_shares = float_market_cap / last_price（竞价 final 快照推算）。
-- turn 每日派生标记：legacy（baostock 历史）/ calc_float（本地股本算）/
--   ffill（近 20 日 ≤5 日连续洞填充）/ official（在线源真值）/ NULL（无有效换手）。
CREATE TABLE IF NOT EXISTS float_share_snapshot (
  thscode          TEXT NOT NULL,     -- 600519.SH 格式
  as_of            TEXT NOT NULL,     -- 快照日期 YYYY-MM-DD（<= 使用日）
  float_market_cap REAL,
  last_price       REAL,
  float_shares     REAL,              -- 股（不是手）
  source           TEXT DEFAULT 'auction_final',
  fetched_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (thscode, as_of)
);
CREATE INDEX IF NOT EXISTS idx_fss_code_asof
  ON float_share_snapshot (thscode, as_of DESC);

-- stock_kline_data 增加 turn 来源列（turn 写入仍 COALESCE 保护，禁止空值覆盖旧换手）
ALTER TABLE stock_kline_data ADD COLUMN turn_source TEXT;
