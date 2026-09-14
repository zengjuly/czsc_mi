-- 阶段 3（006.md W23）：分析结果缓存。
-- 键 = symbol + trade_date + adjust + rule_ver + chan_enabled + chan_score
--      + czsc_ver + include_detail + bars_fingerprint（日K末根 dt/close/
-- volume + 根数哈希）。收盘后日级有效；sync 更新该票 K 线时删除对应日缓存。
CREATE TABLE IF NOT EXISTS analysis_cache (
  symbol       TEXT NOT NULL,
  trade_date   TEXT NOT NULL,
  cache_key    TEXT NOT NULL,      -- 开关+版本+指纹的归一哈希
  adjust       TEXT,
  rule_ver     TEXT,
  chan_enabled INTEGER,
  chan_score   INTEGER,
  czsc_ver     TEXT,
  include_detail INTEGER,
  bars_fingerprint TEXT,
  payload_json TEXT NOT NULL,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (symbol, trade_date, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_analysis_cache_sym
  ON analysis_cache (symbol, trade_date);
