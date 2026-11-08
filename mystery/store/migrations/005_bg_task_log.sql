-- W77：后台扫描任务持久化台账（Web「最近任务」可见全部历史）。
-- 新库由 schema.sql 直接建表；本迁移为已有生产库补表（幂等，重复执行无副作用）。
-- 每条任务一行，启动/进度/终态由 mystery/services/bg_task_log.py 维护。
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
