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

-- 4. （已清理）原 mystery_analysis_cache 为孤儿死表：无代码引用，真实分析结果缓存为 analysis_cache（migrations/002_analysis_cache.sql 创建）。
--    清理见 migrations/004_drop_orphan_analysis_cache.sql（DROP TABLE IF EXISTS，幂等）。

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

-- 10. 扫描任务（W18 起按 scan_type 分类；W77 改为每类保留最近若干份）
CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT, started_at TEXT, finished_at TEXT, n_ok INTEGER, n_fail INTEGER,
  scan_type TEXT DEFAULT 'market'
);

-- 11. 扫描结果（三类信号 + 完整 payload）
CREATE TABLE IF NOT EXISTS scan_results (
  job_id INTEGER, symbol TEXT, trade_date TEXT,
  score REAL, true_resonance INTEGER,
  vap_atr_break INTEGER, chip_low INTEGER,
  payload_json TEXT,
  PRIMARY KEY (job_id, symbol)
);

-- 12. 后台扫描任务台账（W77）：启动/进度/终态全留痕，服务重启后仍可查。
--     与 scan_jobs 通过 job_id 关联；详见 mystery/services/bg_task_log.py。
CREATE TABLE IF NOT EXISTS bg_task_log (
  id           TEXT PRIMARY KEY,
  label        TEXT NOT NULL,
  status       TEXT NOT NULL,
  done         INTEGER DEFAULT 0,
  total        INTEGER DEFAULT 0,
  stage        TEXT DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  job_id       INTEGER,
  n_results    INTEGER DEFAULT 0,
  error        TEXT DEFAULT '',
  scan_type    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bg_task_log_created
  ON bg_task_log (created_at DESC);
