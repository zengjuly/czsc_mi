-- mystery 本地库 schema（与现网 mystery_cache.db 兼容；幂等建表，不做改列迁移）
-- 由 MysteryDB.__init__ 在库不存在或缺表时自动执行

-- 1. 证券信息表（代码/名称/类型/行业）
CREATE TABLE IF NOT EXISTS stock_industry_info (
    code        TEXT PRIMARY KEY,
    code_name   TEXT,
    ipo_date    TEXT,
    out_date    TEXT,
    type        TEXT,
    status      TEXT,
    industry    TEXT
);

-- 2. 核心行情表（联合主键，日/周/月线合并存储）
CREATE TABLE IF NOT EXISTS stock_kline_data (
    code      TEXT NOT NULL,
    date      TEXT NOT NULL,
    period    TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    preclose  REAL,
    volume    REAL,
    amount    REAL,
    adjustflag REAL,
    turn      REAL,
    tradestatus REAL,
    pctChg    REAL,
    isST      REAL,
    PRIMARY KEY (code, date, period)
);
CREATE INDEX IF NOT EXISTS idx_kline_fast_query
    ON stock_kline_data (code, period, date);

-- 3. 基本面快照表
CREATE TABLE IF NOT EXISTS stock_financial_data (
    code         TEXT NOT NULL,
    report_date  TEXT NOT NULL,
    roe REAL, roe_avg REAL, np_margin REAL, gp_margin REAL,
    net_profit REAL, eps_ttm REAL, PB REAL, PE REAL,
    divid_cash REAL,
    PRIMARY KEY (code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_financial_query
    ON stock_financial_data (code, report_date DESC);

-- 4. 分析结果缓存表
CREATE TABLE IF NOT EXISTS mystery_analysis_cache (
    stock_code      TEXT NOT NULL,
    period          TEXT NOT NULL,
    last_trade_date TEXT NOT NULL,
    report_json     TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code, period, last_trade_date)
);

-- 5. 行业板块指数行情表（真实指数，非个股抽样）
CREATE TABLE IF NOT EXISTS sector_kline (
    sector_code TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL,
    low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER NOT NULL, amount REAL NOT NULL,
    source_type TEXT DEFAULT 'ths',
    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sector_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_sector_kline_date
    ON sector_kline (trade_date, sector_code);
CREATE INDEX IF NOT EXISTS idx_sector_kline_code_date
    ON sector_kline (sector_code, trade_date DESC);

-- 6. 板块元数据表
CREATE TABLE IF NOT EXISTS sector_meta (
    sector_code TEXT PRIMARY KEY,
    sector_name TEXT NOT NULL,
    parent_type TEXT,
    base_code TEXT,
    is_active INTEGER DEFAULT 1,
    last_sync_date TEXT
);

-- 7. 股票-板块关系表
CREATE TABLE IF NOT EXISTS stock_sector_rel (
    stock_code  TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    is_primary  INTEGER DEFAULT 0,
    PRIMARY KEY (stock_code, sector_code)
);

-- 8. 板块成分表
CREATE TABLE IF NOT EXISTS sector_constituents (
    sector_code TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    stock_name  TEXT,
    PRIMARY KEY (sector_code, stock_code)
);

-- 9. 缠论结构缓存：行情日变化或 czsc_ver 变化才失效
CREATE TABLE IF NOT EXISTS chan_cache (
  symbol TEXT, freq TEXT, trade_date TEXT, czsc_ver TEXT,
  payload_json TEXT,
  PRIMARY KEY(symbol, freq, trade_date, czsc_ver)
);

-- 10. 扫描任务（002.md W1-B：同日重复扫描默认新 job，不覆盖历史）
CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT, started_at TEXT, finished_at TEXT, n_ok INTEGER, n_fail INTEGER
);

-- 11. 扫描结果（三类信号 + 完整 payload）
CREATE TABLE IF NOT EXISTS scan_results (
  job_id INTEGER, symbol TEXT, trade_date TEXT,
  score REAL, true_resonance INTEGER,
  vap_atr_break INTEGER, chip_low INTEGER,
  payload_json TEXT,
  PRIMARY KEY (job_id, symbol)
);
