"""W79 回归：扫描 job 可解释性（范围/股票池/limit/耗时/三浪口径）。

覆盖：
1. schema.sql 新库带全部 W79 列；006 迁移对老库补列。
2. _write_scan_batch 带 meta 时写全列（scope/universe_n/limit_n/
   duration_sec/wave3_enabled/wave3_mode/started_at/finished_at）。
3. meta=None（所有既有调用/测试）时行为零回归，不写 W79 列也不报错。
4. 动态列探测：未迁移老库（无 W79 列）写入/查询均不报错。
5. _infer_scan_scope 五种范围判定正确（避免自选扫描被误当全市场）。
6. scan_job_meta 返回中文标签与字段，老库/无该 job 返回空 dict。
"""
from __future__ import annotations

import sqlite3
import time

from mystery.services import scan as scan_mod
from mystery.store import db as dbmod


def _mk(code, score):
    return {"symbol": code, "trade_date": "2026-11-08", "score": score,
            "true_resonance": score >= 80, "vap_atr_break": False,
            "chip_low": False, "name": code}


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(scan_jobs)")}


def test_fresh_db_has_w79_columns(tmp_path):
    """schema.sql 新库直接带全部 W79 列（无需迁移补列）。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w79.db"))
    conn = db._connect()
    try:
        cols = _cols(conn)
        for c in scan_mod._SCAN_META_COLS:
            assert c in cols, f"schema.sql 缺 W79 列 {c}"
        # 006 迁移已在新库上幂等应用（duplicate column 被框架忽略）
        applied = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
        assert "006_scan_job_meta.sql" in applied
    finally:
        conn.close()


def test_write_scan_batch_with_meta(tmp_path):
    """带 meta 写库：范围/股票池/limit/耗时/三浪/起止时刻全部落列。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w79m.db"))
    meta = scan_mod._build_scan_meta(
        watchlist=["a", "b"], universe=None, limit=None,
        scan_type="watchlist", universe_n=2, t0=time.time() - 1.5)
    jid = scan_mod._write_scan_batch(
        db, [_mk("sh.600001", 88)], 0, "2026-11-08",
        scan_type="watchlist", meta=meta)
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT scope, universe_n, limit_n, duration_sec, wave3_enabled, "
            "wave3_mode, started_at, finished_at FROM scan_jobs WHERE id=?",
            (jid,)).fetchone()
        assert row[0] == "watchlist"          # 范围：自选股
        assert row[1] == 2                     # 股票池只数
        assert row[2] is None                  # 无 limit
        assert row[3] is not None and row[3] >= 0   # 耗时
        assert row[4] in (0, 1)               # 三浪开关
        assert row[6] and row[7]              # 起止时刻（iso 字符串）
    finally:
        conn.close()
    jm = scan_mod.scan_job_meta(jid, cfg={"db_path": str(tmp_path / "w79m.db")})
    assert jm.get("scope") == "watchlist"
    assert "自选股" in jm.get("label", "")
    assert jm.get("scope_cn") == "自选股"


def test_write_scan_batch_without_meta_no_regression(tmp_path):
    """meta=None（所有既有调用点）时零回归：不写 W79 列、用默认值。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w79n.db"))
    jid = scan_mod._write_scan_batch(
        db, [_mk("sh.600001", 60)], 1, "2026-11-08", scan_type="market")
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT n_ok, n_fail, scope, duration_sec, wave3_enabled "
            "FROM scan_jobs WHERE id=?", (jid,)).fetchone()
        assert row[0] == 1 and row[1] == 1
        assert row[2] == ""          # scope 默认
        assert row[3] is None        # duration 未写
        assert row[4] == 0           # wave3 默认 0
    finally:
        conn.close()


def test_apply_meta_on_unmigrated_db_is_safe():
    """老库无 W79 列时，动态列探测让写入/UPDATE 静默跳过、不报错。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE scan_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "trade_date TEXT, started_at TEXT, finished_at TEXT, n_ok INTEGER, "
        "n_fail INTEGER, scan_type TEXT DEFAULT 'market')")
    meta = {"scope": "full", "universe_n": 10, "wave3_enabled": 1,
            "wave3_mode": "auto", "started_at": "2026-11-08T00:00:00",
            "_t0": 1_700_000_000.0}
    # 无列 → _apply_scan_job_meta 直接 return，不抛异常
    scan_mod._apply_scan_job_meta(conn, 1, meta, "2026-11-08T00:00:01")
    assert "scope" not in _cols(conn)
    conn.close()


def test_infer_scan_scope():
    """五种范围判定：board/watchlist/sector/limit/full。"""
    f = scan_mod._infer_scan_scope
    assert f(["a"], None, None, "watchlist") == "watchlist"
    assert f(None, ["a"], None, "board") == "board"
    assert f(None, ["a"], None, "sector") == "sector"
    assert f(None, None, 100, "market") == "limit"
    assert f(None, None, None, "market") == "full"


def test_scan_job_meta_missing_returns_empty(tmp_path):
    """不存在的 job → 空 dict（Web 侧兜底文案）。"""
    db_path = str(tmp_path / "w79e.db")
    dbmod.MysteryDB(db_path=db_path)  # 初始化库
    assert scan_mod.scan_job_meta(999999, cfg={"db_path": db_path}) == {}
