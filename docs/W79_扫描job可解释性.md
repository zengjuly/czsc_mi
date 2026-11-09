# W79：扫描 job 可解释性（范围 / 耗时 / 三浪口径）（v0.10.38）

## 问题

用户建议 P0：「`scan_jobs` 增加 `scope`（watchlist / limit / full）、耗时、
是否启用三浪。真三振池和系统状态展示范围与时间」，核心诉求是
**避免自选股扫描被误当成全市场扫描**。此前 `scan_jobs` 只有 7 列
（id/trade_date/started_at/finished_at/n_ok/n_fail/scan_type），且
`started_at == finished_at`（都取写库时刻），没有真实耗时。

## 落地改动

### 1. 表结构（新列，全部可空/有默认，历史 job 不回填）

`mystery/store/schema.sql` 的 `scan_jobs` 与新增迁移
`migrations/006_scan_job_meta.sql`：

| 列 | 类型 | 含义 |
| --- | --- | --- |
| `scope` | TEXT DEFAULT '' | `watchlist`/`limit`/`full`/`sector`/`board` |
| `universe_n` | INTEGER | 本次扫描股票池只数（limit 后） |
| `limit_n` | INTEGER | limit 参数（None→NULL） |
| `duration_sec` | REAL | 扫描耗时秒（含 auto_sync/逐票/三浪收尾/写库） |
| `wave3_enabled` | INTEGER DEFAULT 0 | 是否启用三浪（1/0） |
| `wave3_mode` | TEXT DEFAULT '' | 三浪上限模式 auto/fixed/full/off |

- 新库由 `schema.sql` 直接建出全部列；旧库靠 006 迁移 `ALTER TABLE ADD COLUMN`
  补列（每条独立语句，框架容忍 `duplicate column name`，幂等）。
- **老 job 不回填**：未带 W79 列的历史 job 值为默认（scope=''/wave3=0/其余 NULL），
  页面对空值有兜底，历史数据完全不动。

### 2. 写入（`mystery/services/scan.py`）—— 动态列探测，零回归

- `_build_scan_meta(watchlist, universe, limit, scan_type, universe_n, t0)`：
  纯计算 + 只读 `runtime_settings.scan_wave3_options_effective()`（与 W76 网页
  配置同源），构造 meta dict；`_infer_scan_scope` 五种范围判定。
- **INSERT 保持原 6 列不变**，写库后由 `_apply_scan_job_meta` 用
  `PRAGMA table_info` 动态探测：W79 列齐全才 `UPDATE` 补写，缺列静默跳过。
  这样所有直接调用 `_write_scan_batch` 的既有测试 / 老库都零回归。
- `meta` 从 `_scan_market_impl` 开头构造一次，透传到 **5 个写库点**
  （进程池/线程池两条 `_persist_or_return`，3 处 `_finalize_resume_job`），
  断点续跑 job 也有范围与耗时。`scan_market` 及所有外部调用点（CLI/Web/日报/
  后台任务）**签名零改动**。
- `started_at` 取真实扫描开始时刻，`finished_at` 取写库/收尾时刻，
  `duration_sec = time.time() - t0`。

### 3. 展示（Web）

- `components.py::_recent_jobs` 用动态列 SELECT 返回 10 元组（老库缺列以
  NULL/默认补齐），新增 `_job_label(j)` 生成中文标签：
  `[范围] N只 耗时Xs 三浪开 · 日期 成功a 失败b`。
- 扫描页 / 系统状态页「最近扫描任务与结果」下拉与 caption 均显示
  `scan_job_meta(job_id).label`（范围/股票池/耗时/三浪/时间）。
- **真三振池**顶部新增一行「📐 扫描范围：…」，明确本次 job 是自选股还是
  全市场、耗时多久、三浪是否开启——直接回应「避免自选扫描被误当成全市场」。

### 4. 新增 service 函数

`scan_job_meta(job_id, cfg=None) -> dict`：返回原始字段 + `scope_cn`/
`wave3_cn`/`label`（可直接展示的一行摘要）。老库未迁移或无该 job 返回 `{}`，
Web 侧兜底。

## 约束遵守

- **不改评分 / 信号 / `RULE_VER`**；`core/` 零改动、零 IO 约束不变。
- 元数据是「本次扫描过程」的观测，不参与任何判定，不改变结果内容。
- 三浪开关只读取运行时有效配置，不新开/新关三浪。
- 唯一分析入口不变；`MYSTERY_CHAN_SCORE` 保持 0。

## 测试

`tests/test_w79_scan_job_meta.py`（6 例）：新库带列 / 带 meta 写全列 /
meta=None 零回归 / 老库动态列安全 / 五种范围判定 / 不存在 job 返回空 dict。
配合既有 W77/W69/store 迁移测试全量回归。

## 回滚

`MYSTERY_SCAN_JOB_KEEP` 不受影响；W79 列全部可空，即使回退代码，老库
多出的列也不影响 6 列 INSERT/查询（SQLite 允许）。
