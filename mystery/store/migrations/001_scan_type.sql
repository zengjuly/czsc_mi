-- W18 的 scan_jobs.scan_type 列（从 db.py 零散 ALTER 收编为正式迁移）。
-- 全新库由 schema.sql 直接建出该列；本迁移对已存在列幂等跳过（框架
-- 容忍 duplicate column）。
ALTER TABLE scan_jobs ADD COLUMN scan_type TEXT DEFAULT 'market';
