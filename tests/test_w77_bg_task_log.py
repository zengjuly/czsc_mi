"""W77 回归：后台扫描任务台账 + scan_jobs 历史保留。

覆盖本次生产问题「后台扫描全部记录在最近任务中看不到」的四条修复：
1. 任务启动/进度/终态持久化（服务重启后 recent() 仍可读）；
2. 失败也留痕（并发锁拒绝、线程被打断不再无声无息）；
3. 超时 running 标记 interrupted、台账裁剪不无限膨胀；
4. ``_write_scan_batch`` 不再删光同类型历史 job，按 KEEP 保留最近若干份。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.services import bg_task_log as btl
from mystery.services.scan import _write_scan_batch, _scan_job_keep
from mystery.store import db as dbmod


def _mk(code, score):
    return {
        "symbol": code, "trade_date": "2024-01-15", "score": score,
        "true_resonance": score >= 80, "vap_atr_break": False,
        "chip_low": True, "name": code,
    }


def test_start_progress_finish_persisted(tmp_path):
    path = str(tmp_path / "log.db")
    btl.log_start(path, "abcd1234", "后台扫描全部股票", created=1_700_000_000)
    rows = btl.recent(path)
    assert len(rows) == 1
    assert rows[0]["id"] == "abcd1234"
    assert rows[0]["status"] == "running"
    assert rows[0]["label"] == "后台扫描全部股票"

    btl.log_progress(path, "abcd1234", 120, 5580, "逐票扫描")
    btl.log_finish(path, "abcd1234", "done", job_id=95, n_results=5578,
                   updated=1_700_000_300)
    rows = btl.recent(path)
    assert rows[0]["status"] == "done"
    assert rows[0]["job_id"] == 95
    assert rows[0]["n_results"] == 5578
    assert rows[0]["done"] == 120 and rows[0]["total"] == 5580
    assert btl.status_label(rows[0]["status"]) == "✅ 完成"


def test_error_and_cleared_leave_trace(tmp_path):
    path = str(tmp_path / "log.db")
    btl.log_start(path, "err00001", "后台扫描全部自选股")
    btl.log_finish(path, "err00001", "error", error="已有扫描在运行（pid 123）")
    btl.log_start(path, "clr00001", "后台扫描全部股票")
    btl.log_finish(path, "clr00001", "cleared", error="页面手动清除（疑似失联）")
    by_id = {r["id"]: r for r in btl.recent(path)}
    assert by_id["err00001"]["status"] == "error"
    assert "已有扫描在运行" in by_id["err00001"]["error"]
    assert by_id["clr00001"]["status"] == "cleared"


def test_mark_stale_only_old_running(tmp_path):
    path = str(tmp_path / "log.db")
    btl.log_start(path, "fresh001", "新任务")
    old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    btl.ensure_table(path)
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO bg_task_log (id,label,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?)", ("old00001", "旧任务", "running", old, old))
    conn.commit()
    conn.close()

    assert btl.mark_stale(path, timeout_seconds=600) == 1
    by_id = {r["id"]: r for r in btl.recent(path)}
    assert by_id["old00001"]["status"] == "interrupted"
    assert "判定中断" in by_id["old00001"]["error"]
    assert by_id["fresh001"]["status"] == "running"


def test_prune_keeps_latest(tmp_path):
    path = str(tmp_path / "log.db")
    for i in range(10):
        btl.log_start(path, f"t{i:06d}", f"任务{i}",
                      created=1_700_000_000 + i)
    btl.prune(path, keep=3)
    rows = btl.recent(path, limit=20)
    assert len(rows) == 3
    assert [r["id"] for r in rows] == ["t000009", "t000008", "t000007"]


def test_recent_limit_and_empty(tmp_path):
    path = str(tmp_path / "log.db")
    assert btl.recent(path) == []          # 读侧自动建表，不抛异常
    for i in range(8):
        btl.log_start(path, f"z{i:06d}", "任务", created=1_700_000_000 + i)
    assert len(btl.recent(path, limit=5)) == 5


def test_scan_jobs_keep_recent_history(tmp_path, monkeypatch):
    """W18 旧行为会删光同类型历史；W77 应保留最近 KEEP 份及各自结果。"""
    monkeypatch.setenv("MYSTERY_SCAN_JOB_KEEP", "2")
    assert _scan_job_keep() == 2
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    ids = []
    for i in range(4):
        ids.append(_write_scan_batch(db, [_mk(f"sh.60000{i}", 50 + i)], 0,
                                    "2024-01-15", scan_type="market"))
    conn = db._connect()
    try:
        jobs = [r[0] for r in conn.execute(
            "SELECT id FROM scan_jobs WHERE scan_type='market' ORDER BY id DESC")]
        counts = dict(conn.execute(
            "SELECT job_id, COUNT(*) FROM scan_results GROUP BY job_id"))
    finally:
        conn.close()
    assert jobs == [ids[3], ids[2]], jobs          # 只留最近 2 份
    assert counts.get(ids[3]) == 1 and counts.get(ids[2]) == 1
    assert ids[0] not in counts and ids[1] not in counts  # 老 job 的结果一并清理


def test_scan_jobs_keep_one_restores_w18(tmp_path, monkeypatch):
    """KEEP=1 回退到 W18「同类型只保留最新一份」语义。"""
    monkeypatch.setenv("MYSTERY_SCAN_JOB_KEEP", "1")
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    _write_scan_batch(db, [_mk("sh.600001", 60)], 0, "2024-01-15", "market")
    _write_scan_batch(db, [_mk("sh.600002", 70)], 0, "2024-01-15", "market")
    conn = db._connect()
    try:
        n_jobs = conn.execute(
            "SELECT COUNT(*) FROM scan_jobs WHERE scan_type='market'").fetchone()[0]
        n_rows = conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0]
    finally:
        conn.close()
    assert n_jobs == 1 and n_rows == 1


def test_scan_jobs_other_type_untouched(tmp_path, monkeypatch):
    """清理只针对同一 scan_type，自选/板块 job 不受全市场扫描影响。"""
    monkeypatch.setenv("MYSTERY_SCAN_JOB_KEEP", "1")
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    wl_id = _write_scan_batch(db, [_mk("sh.600001", 60)], 0,
                              "2024-01-15", scan_type="watchlist")
    _write_scan_batch(db, [_mk("sh.600002", 70)], 0,
                      "2024-01-15", scan_type="market")
    conn = db._connect()
    try:
        exists = conn.execute(
            "SELECT COUNT(*) FROM scan_jobs WHERE id=?", (wl_id,)).fetchone()[0]
    finally:
        conn.close()
    assert exists == 1
