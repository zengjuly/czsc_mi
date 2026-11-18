"""W101：历史数据定时清理（retention）单测。

纯 sqlite3 临时库，不依赖生产库/可选依赖；覆盖：
- dry-run 不删只统计；
- scan/replay 分档保留期（replay 更宽松）；
- 孤儿结果清理；
- 陈旧缓存按天清理；
- 冗余日K按 cap 删最旧；
- VACUUM 阈值判断；
- run_cleanup 事务与结构化返回。
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mystery.services import retention as R  # noqa: E402


def _d(offset_days: int) -> str:
    return (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture()
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE scan_jobs (
        id INTEGER PRIMARY KEY, trade_date TEXT, started_at TEXT,
        scan_type TEXT, n_ok INTEGER, n_fail INTEGER);
    CREATE TABLE scan_results (
        job_id INTEGER, symbol TEXT, trade_date TEXT, score REAL,
        true_resonance INTEGER, vap_atr_break INTEGER, chip_low INTEGER,
        payload_json TEXT, PRIMARY KEY(job_id, symbol));
    CREATE TABLE analysis_cache (
        symbol TEXT, trade_date TEXT, cache_key TEXT, payload_json TEXT,
        created_at TEXT);
    CREATE TABLE chan_cache (
        symbol TEXT, freq TEXT, trade_date TEXT, czsc_ver TEXT, payload_json TEXT);
    CREATE TABLE stock_kline_data (
        code TEXT, date TEXT, period TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, PRIMARY KEY(code, date, period));
    """)
    con.close()
    yield path
    os.unlink(path)


def _seed(con):
    # scan_jobs: 普通 job 新/旧各一，replay 旧一份
    jobs = [
        (1, _d(2), _d(2) + "T10:00:00", "market"),
        (2, _d(60), _d(60) + "T10:00:00", "market"),      # 过期(>30天)
        (3, _d(90), _d(90) + "T10:00:00", "replay"),       # replay 90天，<180 → 保留
        (4, _d(300), _d(300) + "T10:00:00", "replay"),     # replay 300天 → 过期
    ]
    con.executemany(
        "INSERT INTO scan_jobs (id,trade_date,started_at,scan_type,n_ok,n_fail) "
        "VALUES (?,?,?,?,0,0)", jobs)
    # results: job1 2行, job2 1行, job3 1行, job4 1行, 孤儿 job999 1行
    rows = [
        (1, "sh600001", _d(2), 90.0, '{}'),
        (1, "sh600002", _d(2), 80.0, '{}'),
        (2, "sh600003", _d(60), 70.0, '{}'),
        (3, "sh600004", _d(90), 60.0, '{}'),
        (4, "sh600005", _d(300), 50.0, '{}'),
        (999, "sh600006", _d(1), 40.0, '{}'),             # 孤儿
    ]
    con.executemany(
        "INSERT INTO scan_results (job_id,symbol,trade_date,score,payload_json) "
        "VALUES (?,?,?,?,?)", rows)
    # 缓存：新/旧各一
    con.execute("INSERT INTO analysis_cache VALUES (?,?,?,?,?)",
                ("sh600001", _d(2), "k", "{}", _d(2)))
    con.execute("INSERT INTO analysis_cache VALUES (?,?,?,?,?)",
                ("sh600009", _d(90), "k", "{}", _d(90)))  # 过期
    con.execute("INSERT INTO chan_cache VALUES (?,?,?,?,?)",
                ("sh600001", "1d", _d(2), "v", "{}"))
    con.execute("INSERT INTO chan_cache VALUES (?,?,?,?,?)",
                ("sh600009", "1d", _d(90), "v", "{}"))     # 过期
    # 日K：sh600001 有 cap+5 根；sh600002 有 3 根
    for i in range(205):
        con.execute("INSERT INTO stock_kline_data VALUES (?,?,?,?,?,?,?,?)",
                    ("sh600001", f"2000-01-{i+1:02d}", "daily", 1, 1, 1, 1, 100))
    for i in range(3):
        con.execute("INSERT INTO stock_kline_data VALUES (?,?,?,?,?,?,?,?)",
                    ("sh600002", f"2026-11-{i+1:02d}", "daily", 1, 1, 1, 1, 100))
    con.commit()


def test_scan_history_dry_run_reports_without_deleting(db):
    con = sqlite3.connect(db)
    _seed(con)
    r = R.cleanup_scan_history(con, scan_days=30, replay_days=180, apply=False)
    assert r["scan_jobs_expired"] == 2          # job2(market 60d) + job4(replay 300d)
    assert r["scan_results_expired"] == 2       # job2 1行 + job4 1行
    assert r["scan_results_orphan"] == 1
    # 未删
    assert con.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()[0] == 4
    con.close()


def test_scan_history_apply_deletes_expired_and_orphan(db):
    con = sqlite3.connect(db)
    _seed(con)
    R.cleanup_scan_history(con, scan_days=30, replay_days=180, apply=True)
    con.commit()
    ids = [r[0] for r in con.execute("SELECT id FROM scan_jobs").fetchall()]
    assert ids == [1, 3]                        # 新 market + 90d replay 保留
    # 孤儿 999 被删
    assert con.execute(
        "SELECT COUNT(*) FROM scan_results WHERE job_id=999").fetchone()[0] == 0
    # job1 的 2 行 + job3 的 1 行保留
    assert con.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0] == 3
    con.close()


def test_replay_kept_longer_than_normal(db):
    con = sqlite3.connect(db)
    _seed(con)
    # replay 90 天：普通口径(30d)会删，replay 口径(180d)应保留
    R.cleanup_scan_history(con, scan_days=30, replay_days=180, apply=True)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM scan_jobs WHERE id=3").fetchone()[0] == 1
    # 若把 replay_days 收紧到 30，则 90 天 replay 也应删
    con2 = sqlite3.connect(db)
    R.cleanup_scan_history(con2, scan_days=30, replay_days=30, apply=True)
    con2.commit()
    assert con2.execute("SELECT COUNT(*) FROM scan_jobs WHERE id=3").fetchone()[0] == 0
    con.close(); con2.close()


def test_cache_cleanup_by_date(db):
    con = sqlite3.connect(db)
    _seed(con)
    r = R.cleanup_analysis_cache(con, cache_days=30, apply=False)
    assert r["analysis_cache"] == 1 and r["chan_cache"] == 1
    R.cleanup_analysis_cache(con, cache_days=30, apply=True)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM chan_cache").fetchone()[0] == 1
    con.close()


def test_kline_cap_prunes_oldest(db):
    con = sqlite3.connect(db)
    _seed(con)
    r = R.cleanup_stale_kline(con, cap=200, apply=False)
    assert r["kline_over_codes"] == 1 and r["kline_rows"] == 5
    R.cleanup_stale_kline(con, cap=200, apply=True)
    con.commit()
    n = con.execute(
        "SELECT COUNT(*) FROM stock_kline_data WHERE code='sh600001'").fetchone()[0]
    assert n == 200
    con.close()


def test_kline_cap_zero_disabled(db):
    con = sqlite3.connect(db)
    _seed(con)
    r = R.cleanup_stale_kline(con, cap=0, apply=True)
    assert r["kline_cap"] == 0 and r["kline_rows"] == 0
    con.close()


def test_vacuum_threshold_logic(db):
    con = sqlite3.connect(db)
    # 禁用
    assert R.maybe_vacuum(con, apply=True, ratio_threshold=1)["reason"] == "disabled"
    # dry-run
    assert R.maybe_vacuum(con, apply=False, ratio_threshold=0.15)["reason"] == "dry-run"
    # 每次都回收（阈值 0）
    r = R.maybe_vacuum(con, apply=True, ratio_threshold=0)
    assert r["vacuum"] is True
    con.close()


def test_run_cleanup_dry_run_never_modifies(db):
    before = sqlite3.connect(db)
    _seed(before)
    jobs_before = before.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()[0]
    before.close()
    result = R.run_cleanup(db, apply=False, do_vacuum=False)
    assert result["ok"] is True and result["applied"] is False
    assert result["vacuum"]["reason"] == "dry-run"
    after = sqlite3.connect(db)
    assert after.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()[0] == jobs_before
    after.close()


def test_run_cleanup_apply_all(db):
    _seed(sqlite3.connect(db))
    result = R.run_cleanup(db, apply=True, scan_days=30, replay_days=180,
                           cache_days=30, kline_cap=200, do_vacuum=False)
    assert result["ok"] is True
    assert result["scan"]["scan_jobs_expired"] == 2
    assert result["cache"]["analysis_cache"] == 1
    assert result["kline"]["kline_over_codes"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()[0] == 2
    con.close()
