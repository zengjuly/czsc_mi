"""W62 验证：_ensure_scan_data 的三连查询合并为单条 SQL 后语义不变。

覆盖：合并 SQL 与原 MAX + 两遍 COUNT(DISTINCT) 三连查询在「多日混合数据」
与「空表」两种场景下结果完全一致（db_latest / cov_n / cov_all）。
"""
import pytest

_MERGED = (
    "SELECT "
    "(SELECT MAX(date) FROM stock_kline_data WHERE period='daily') AS db_latest, "
    "COUNT(DISTINCT CASE WHEN date = "
    "(SELECT MAX(date) FROM stock_kline_data WHERE period='daily') "
    "THEN code END) AS cov_n, "
    "COUNT(DISTINCT code) AS cov_all "
    "FROM stock_kline_data WHERE period='daily'"
)


def _insert(conn, code, date, period="daily"):
    conn.execute(
        "INSERT INTO stock_kline_data (code,date,period) VALUES (?,?,?)",
        (code, date, period))


def test_coverage_sql_merge_equivalent(tmp_path):
    from mystery.store.db import MysteryDB
    db = MysteryDB(db_path=str(tmp_path / "cov.db"))
    conn = db._connect()
    # 最新日 3 只
    _insert(conn, "sh.600001", "2024-12-31")
    _insert(conn, "sh.600002", "2024-12-31")
    _insert(conn, "sh.600003", "2024-12-31")
    # 历史日 2 只（含与最新日重叠的 600001）
    _insert(conn, "sh.600004", "2024-12-30")
    _insert(conn, "sh.600005", "2024-12-30")
    _insert(conn, "sh.600001", "2024-12-30")
    # 非 daily 不计入覆盖率
    _insert(conn, "sh.600001", "2024-12-31", "weekly")
    conn.commit()
    conn.close()

    def merged():
        c = db._connect()
        try:
            row = c.execute(_MERGED).fetchone()
            return str(row[0] or ""), int(row[1] or 0), int(row[2] or 0)
        finally:
            c.close()

    def original():
        c = db._connect()
        try:
            row = c.execute(
                "SELECT MAX(date) FROM stock_kline_data WHERE period='daily'"
            ).fetchone()
            db_latest = str(row[0]) if row and row[0] else ""
            cov_n, cov_all = 0, 0
            if db_latest:
                cov_n = c.execute(
                    "SELECT COUNT(DISTINCT code) FROM stock_kline_data "
                    "WHERE period='daily' AND date=?", (db_latest,)
                ).fetchone()[0] or 0
                cov_all = c.execute(
                    "SELECT COUNT(DISTINCT code) FROM stock_kline_data "
                    "WHERE period='daily'").fetchone()[0] or 0
            return db_latest, cov_n, cov_all
        finally:
            c.close()

    assert merged() == original() == ("2024-12-31", 3, 5)


def test_coverage_sql_empty_db(tmp_path):
    from mystery.store.db import MysteryDB
    db = MysteryDB(db_path=str(tmp_path / "empty.db"))
    c = db._connect()
    try:
        row = c.execute(_MERGED).fetchone()
        assert (str(row[0] or ""), int(row[1] or 0), int(row[2] or 0)) == ("", 0, 0)
    finally:
        c.close()
