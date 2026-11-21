"""W117 日报提速：索引友好 SQL 改写 + 索引声明的回归测试（离线，内存 SQLite）。

覆盖：
1. schema.sql 与 migration 007 都声明了两个聚合索引；
2. date 为标准10位时，substr(date,1,10)=? 与 date=? 结果完全等价（改写安全性）；
3. 改写后的 auto_sync 三子查询 cov_n/cov_all 与原 CASE 聚合语义一致；
4. 索引确实被 SQLite 用于 date=? 查询（避免回退全表扫描）。
"""
import os
import sqlite3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


class TestIndexDeclared:
    def test_schema_has_indexes(self):
        schema = _read("mystery/store/schema.sql")
        assert "idx_kline_period_date" in schema
        assert "idx_kline_period_code" in schema

    def test_migration_has_indexes(self):
        mig = _read("mystery/store/migrations/007_kline_aggregate_indexes.sql")
        assert "idx_kline_period_date" in mig
        assert "idx_kline_period_code" in mig


class TestSqlEquivalence:
    def setup_method(self):
        self.c = sqlite3.connect(":memory:")
        self.c.execute(
            "CREATE TABLE stock_kline_data (code TEXT, date TEXT, period TEXT, "
            "volume REAL, turn REAL, turn_source TEXT)")
        rows = [
            ("sh.600519", "2026-11-20", "daily", 1000, 1.5, "official"),
            ("sz.000001", "2026-11-20", "daily", 2000, None, None),
            ("sh.601318", "2026-11-19", "daily", 3000, 2.0, "calc_float"),
            ("sz.000002", "2026-11-20", "daily", 4000, 0.5, "ffill"),
        ]
        self.c.executemany("INSERT INTO stock_kline_data VALUES (?,?,?,?,?,?)", rows)
        self.c.commit()

    def teardown_method(self):
        self.c.close()

    def test_substr_vs_direct_equal(self):
        d = "2026-11-20"
        old = self.c.execute(
            "SELECT code FROM stock_kline_data WHERE period='daily' "
            "AND substr(date,1,10)=? ORDER BY code", (d,)).fetchall()
        new = self.c.execute(
            "SELECT code FROM stock_kline_data WHERE period='daily' "
            "AND date=? ORDER BY code", (d,)).fetchall()
        assert old == new

    def test_autosync_new_sql_matches_old(self):
        new = self.c.execute(
            "SELECT (SELECT MAX(date) FROM stock_kline_data WHERE period='daily') AS db_latest, "
            "(SELECT COUNT(DISTINCT code) FROM stock_kline_data WHERE period='daily' "
            " AND date=(SELECT MAX(date) FROM stock_kline_data WHERE period='daily')) AS cov_n, "
            "(SELECT COUNT(DISTINCT code) FROM stock_kline_data WHERE period='daily') AS cov_all"
        ).fetchone()
        old_latest = self.c.execute(
            "SELECT MAX(date) FROM stock_kline_data WHERE period='daily'").fetchone()[0]
        old_cov_n = self.c.execute(
            "SELECT COUNT(DISTINCT CASE WHEN date=(SELECT MAX(date) FROM stock_kline_data "
            "WHERE period='daily') THEN code END) FROM stock_kline_data WHERE period='daily'"
        ).fetchone()[0]
        old_cov_all = self.c.execute(
            "SELECT COUNT(DISTINCT code) FROM stock_kline_data WHERE period='daily'"
        ).fetchone()[0]
        assert tuple(new) == (old_latest, old_cov_n, old_cov_all)

    def test_index_used_for_date_query(self):
        self.c.execute("CREATE INDEX ix ON stock_kline_data(period, date)")
        plan = self.c.execute(
            "EXPLAIN QUERY PLAN SELECT code FROM stock_kline_data "
            "WHERE period='daily' AND date='2026-11-20'").fetchall()
        assert any("ix" in (r[3] or "") for r in plan)
