-- W59：清理孤儿死表 mystery_analysis_cache。
-- 调查确认：仅 schema.sql 历史 CREATE 定义与一处测试建表清单引用，
-- 业务代码全部使用 analysis_cache（由 migrations/002_analysis_cache.sql 创建）。
-- 本迁移幂等，可安全重复执行。
DROP TABLE IF EXISTS mystery_analysis_cache;
