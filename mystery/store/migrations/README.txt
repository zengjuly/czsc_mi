# migrations dir — 增量 SQL 脚本按文件名排序执行（NNN_名字.sql，如 001_scan_type.sql）。
# _init_db 先跑 schema.sql（幂等建表），再按序应用本目录 *.sql，
# 记账表 schema_migrations(id, applied_at)；语句级执行，重复列可忽略
# （新库 schema.sql 已含新列时，迁移重复应用不报错；旧库靠迁移补列）。
# 改列、加表一律走本目录迁移文件，禁止只改 schema.sql 或在 _init_db 手写 ALTER。
