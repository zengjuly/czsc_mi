"""W60 回归：scan_results 批量写入（executemany）正确性与幂等。

验证 _write_scan_batch / _append_scan_results 在改为 executemany 后：
- 所有结果行都被落库（行数 == 输入数，不再逐行 execute）
- 空结果列表不报错
- _append_scan_results 的 INSERT OR REPLACE 幂等（同 symbol 覆盖不新增）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod
from mystery.services.scan import (_append_scan_results, _write_scan_batch)


def _mk(code, score):
    return {
        "symbol": code, "trade_date": "2024-01-15", "score": score,
        "true_resonance": score and score >= 80, "vap_atr_break": False,
        "chip_low": True, "name": code,
    }


def test_write_scan_batch_bulk_count(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    results = [_mk(f"sh.60000{i}", 50 + i) for i in range(120)]
    job_id = _write_scan_batch(db, results, 0, "2024-01-15", scan_type="market")
    assert job_id is not None
    n = db._connect().execute(
        "SELECT COUNT(*) FROM scan_results WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    assert n == 120


def test_write_scan_batch_empty_ok(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    # 空列表不应触发 executemany 报错
    job_id = _write_scan_batch(db, [], 0, "2024-01-15", scan_type="market")
    assert job_id is not None
    n = db._connect().execute(
        "SELECT COUNT(*) FROM scan_results WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    assert n == 0


def test_append_scan_results_idempotent(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    # 先建一个空 working job（提交并关闭，避免持锁）
    conn = db._connect()
    try:
        conn.execute(
            "INSERT INTO scan_jobs (trade_date, started_at, finished_at, "
            "n_ok, n_fail, scan_type) VALUES (?,?,?,?,?,?)",
            ("2024-01-15", "t", "t", 0, 0, "market"))
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    part1 = [_mk("sh.600001", 60), _mk("sh.600002", 70)]
    _append_scan_results(db, job_id, part1, 0)
    # 续跑第二批，其中 sh.600001 重复（应覆盖而非新增）
    part2 = [_mk("sh.600001", 99), _mk("sh.600003", 80)]
    _append_scan_results(db, job_id, part2, 1)
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT symbol, score FROM scan_results WHERE job_id=? "
            "ORDER BY symbol", (job_id,)).fetchall()
        fail = conn.execute(
            "SELECT n_fail FROM scan_jobs WHERE id=?", (job_id,)).fetchone()[0]
    finally:
        conn.close()
    by_sym = {s: sc for s, sc in rows}
    assert len(by_sym) == 3, by_sym  # sh.600001/002/003
    assert by_sym["sh.600001"] == 99  # 被覆盖为新分
    assert fail == 1
