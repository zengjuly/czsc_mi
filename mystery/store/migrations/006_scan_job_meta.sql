-- W79：scan_jobs 增加扫描可解释性列（范围/股票池/limit/耗时/三浪开关）。
-- 新库由 schema.sql 直接建出全部列；本迁移为已有生产库补列。
-- 每条 ALTER 独立语句执行，框架容忍 "duplicate column name"（新库重复应用不报错）。
-- 老 job 无这些列时值为默认（scope='' / wave3_enabled=0 / 其余 NULL），
-- Web 展示对 NULL 有兜底，历史 job 不受影响、不回填。
ALTER TABLE scan_jobs ADD COLUMN scope TEXT DEFAULT '';
ALTER TABLE scan_jobs ADD COLUMN universe_n INTEGER;
ALTER TABLE scan_jobs ADD COLUMN limit_n INTEGER;
ALTER TABLE scan_jobs ADD COLUMN duration_sec REAL;
ALTER TABLE scan_jobs ADD COLUMN wave3_enabled INTEGER DEFAULT 0;
ALTER TABLE scan_jobs ADD COLUMN wave3_mode TEXT DEFAULT '';
