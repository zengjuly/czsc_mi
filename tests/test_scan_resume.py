"""④ scan 断点续跑核心机制（helper 级，避免拉全市场行情）。"""
from __future__ import annotations

import json
import os
import tempfile

from mystery.services import scan as scan_mod
from mystery.store.db import MysteryDB


def _db():
    p = os.path.join(tempfile.mkdtemp(), "scan.db")
    MysteryDB(p)  # init schema (含 scan_jobs/scan_results)
    return MysteryDB(p)


def _rows(db, jid):
    conn = db._connect()
    try:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM scan_results WHERE job_id=?",
            (jid,)).fetchall()]
    finally:
        conn.close()


def test_open_resume_job_creates_then_reuses():
    db = _db()
    jid1, done1 = scan_mod._open_resume_job(db, "market", "2026-11-03")
    assert isinstance(jid1, int) and jid1 > 0
    assert done1 == set()
    # 同 scan_type+交易日 复用
    jid2, done2 = scan_mod._open_resume_job(db, "market", "2026-11-03")
    assert jid2 == jid1 and done2 == set()
    # 不同交易日 → 新 job
    jid3, _ = scan_mod._open_resume_job(db, "market", "2026-11-02")
    assert jid3 != jid1


def test_append_is_idempotent_and_finalize():
    db = _db()
    jid, _ = scan_mod._open_resume_job(db, "market", "2026-11-03")
    res = [{"symbol": "600519.SH", "trade_date": "2026-11-03", "score": 8.5,
            "true_resonance": True, "vap_atr_break": False, "chip_low": False},
           {"symbol": "000001.SZ", "trade_date": "2026-11-03", "score": 3.0}]
    scan_mod._append_scan_results(db, jid, res, failed_inc=0)
    # 同 symbol 覆盖（INSERT OR REPLACE 幂等）
    scan_mod._append_scan_results(db, jid, [
        {"symbol": "600519.SH", "trade_date": "2026-11-03", "score": 9.0,
         "true_resonance": True, "vap_atr_break": False, "chip_low": False}],
        failed_inc=1)
    scan_mod._finalize_resume_job(db, jid)
    out = _rows(db, jid)
    assert len(out) == 2
    by_sym = {r["symbol"]: r for r in out}
    assert by_sym["600519.SH"]["score"] == 9.0
    conn = db._connect()
    try:
        job = conn.execute(
            "SELECT n_ok, n_fail FROM scan_jobs WHERE id=?",
            (jid,)).fetchone()
    finally:
        conn.close()
    assert job[0] == 2 and job[1] == 1


def test_resume_skips_done_symbols():
    """模拟：首跑 2 只 → 中断；resume 跑剩余，合并为完整 job。"""
    db = _db()
    jid, _ = scan_mod._open_resume_job(db, "market", "2026-11-03")
    scan_mod._append_scan_results(db, jid, [
        {"symbol": "600519.SH", "trade_date": "2026-11-03", "score": 8},
        {"symbol": "000001.SZ", "trade_date": "2026-11-03", "score": 3}], 0)
    # 模拟中断：job 未 finalize（仍 partial）
    # resume：重新 open 同 scan_type+交易日 → 复用，done={600519,000001}
    jid2, done = scan_mod._open_resume_job(db, "market", "2026-11-03")
    assert jid2 == jid
    assert done == {"600519.SH", "000001.SZ"}
    # 剩余个股扫描后追加 + finalize
    scan_mod._append_scan_results(db, jid2, [
        {"symbol": "300750.SZ", "trade_date": "2026-11-03", "score": 7}], 0)
    scan_mod._finalize_resume_job(db, jid2)
    out = _rows(db, jid2)
    assert {r["symbol"] for r in out} == {
        "600519.SH", "000001.SZ", "300750.SZ"}
