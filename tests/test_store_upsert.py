"""test_store_upsert — W4 schema 自举 + upsert 换手保护（临时 sqlite，不碰生产库）。"""
import os
import tempfile

import pandas as pd

from mystery.store.db import MysteryDB


def _fresh_db() -> MysteryDB:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return MysteryDB(db_path=path)


def test_schema_bootstrap():
    """空目录首次 MysteryDB() 能建出全部核心表。"""
    db = _fresh_db()
    conn = db._connect()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    for t in ["stock_kline_data", "stock_industry_info", "stock_financial_data",
              "sector_kline", "sector_meta", "stock_sector_rel",
              "sector_constituents", "mystery_analysis_cache", "chan_cache"]:
        assert t in tables, f"缺表 {t}"


def test_upsert_turn_protected():
    """写入 turn=None 不得覆盖库内旧换手（COALESCE 保护）。"""
    db = _fresh_db()
    df1 = pd.DataFrame([{'日期': '2026-08-01', '开盘价': 10, '最高价': 11,
                         '最低价': 9, '收盘价': 10.5, '成交量': 1e6,
                         '成交额': 1e7, '换手率': 2.5, '涨跌幅': 1.0}])
    db.upsert_kline(df1, 'sh.600519', 'daily')
    # 第二次写入同日期，换手率 None（ths 数据形态）→ 不得覆盖 2.5
    df2 = df1.copy()
    df2['换手率'] = None
    db.upsert_kline(df2, 'sh.600519', 'daily')
    conn = db._connect()
    try:
        turn = conn.execute(
            "SELECT turn FROM stock_kline_data WHERE code='sh.600519' AND date='2026-08-01'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert turn == 2.5, f"换手率被 None 覆盖: {turn}"


def test_upsert_overwrite_values():
    """非 turn 字段正常覆盖（close 更新）。"""
    db = _fresh_db()
    df1 = pd.DataFrame([{'日期': '2026-08-01', '开盘价': 10, '最高价': 11,
                         '最低价': 9, '收盘价': 10.5, '成交量': 1e6,
                         '成交额': 1e7, '换手率': 2.5, '涨跌幅': 1.0}])
    db.upsert_kline(df1, 'sh.600519', 'daily')
    df2 = df1.copy()
    df2['收盘价'] = 11.0
    df2['换手率'] = 3.0
    db.upsert_kline(df2, 'sh.600519', 'daily')
    conn = db._connect()
    try:
        close, turn = conn.execute(
            "SELECT close, turn FROM stock_kline_data WHERE code='sh.600519' AND date='2026-08-01'"
        ).fetchone()
    finally:
        conn.close()
    assert close == 11.0 and turn == 3.0
