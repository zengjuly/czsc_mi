-- mystery 本地库 schema（与现网 mystery_cache.db 兼容；chan_cache 为新增表）

-- 缠论结构缓存：行情日变化或 czsc_ver 变化才失效
CREATE TABLE IF NOT EXISTS chan_cache (
  symbol TEXT,
  freq TEXT,
  trade_date TEXT,
  czsc_ver TEXT,
  payload_json TEXT,
  PRIMARY KEY(symbol, freq, trade_date, czsc_ver)
);
