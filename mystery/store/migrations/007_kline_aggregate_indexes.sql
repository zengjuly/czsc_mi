-- W117：全市场按日/按票聚合索引，压缩换手率回算与 scan auto_sync 耗时。
-- 无索引时 COUNT(DISTINCT code) WHERE period='daily' 全表扫 555万行需 70-150s；
-- idx_kline_period_date 让 date=? / MAX(date) 变索引查询（0.001-0.02s）；
-- idx_kline_period_code 让 COUNT(DISTINCT code) 走覆盖索引（~1.8s）。
CREATE INDEX IF NOT EXISTS idx_kline_period_date
    ON stock_kline_data (period, date);
CREATE INDEX IF NOT EXISTS idx_kline_period_code
    ON stock_kline_data (period, code);
