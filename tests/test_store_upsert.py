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


def test_get_sector_stocks_code_normalize():
    """板块成分查询归一：ths_881101 / 881101.TI / 881101 三种入参都命中 rel 表。

    rel 表 sector_code 存「881101.TI」（无 ths_ 前缀）；005.md 后不允许
    把 ths_881101 直接当 key 查（会 0 行，进而空 universe 落回全 A）。
    """
    db = _fresh_db()
    conn = db._connect()
    try:
        conn.execute("INSERT INTO sector_meta (sector_code, sector_name) "
                     "VALUES ('881101.TI', '种植业与林业')")
        for i in range(3):
            conn.execute(
                "INSERT INTO stock_sector_rel (stock_code, sector_code, is_primary) "
                "VALUES (?, '881101.TI', 1)",
                (f"sh.60051{i}",))
        conn.commit()
    finally:
        conn.close()
    for code in ["881101.TI", "ths_881101", "881101"]:
        out = db.get_sector_stocks(code)
        assert len(out) == 3, f"{code} -> {out}"
        assert all("." not in c for c in out)
    # 无关板块 / 空：返回空列表
    assert db.get_sector_stocks("ths_885863") == []


def test_upsert_stock_sector_rel_normalize():
    """成分写入归一：600519.SH → sh.600519、ths_886015 → 886015.TI；重复写不增行。"""
    db = _fresh_db()
    db.upsert_stock_sector_rel("600519.SH", "ths_886015", is_primary=0)
    db.upsert_stock_sector_rel("sh600519", "886015", is_primary=0)
    db.upsert_stock_sector_rel("000078.SZ", "881143.TI", is_primary=1)
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT stock_code, sector_code, is_primary FROM stock_sector_rel "
            "ORDER BY sector_code").fetchall()
    finally:
        conn.close()
    assert rows == [
        ('sz.000078', '881143.TI', 1),
        ('sh.600519', '886015.TI', 0),
    ], f"归一/去重失败: {rows}"


def test_upsert_rel_does_not_break_primary_industry():
    """概念成分 is_primary=0 不得影响主行业查询（get_industry 用 is_primary=1）。"""
    db = _fresh_db()
    conn = db._connect()
    try:
        conn.execute("INSERT INTO stock_sector_rel (stock_code, sector_code, "
                     "is_primary) VALUES ('sz.000078', '881143.TI', 1)")
        conn.commit()
    finally:
        conn.close()
    db.upsert_stock_sector_rel("000078.SZ", "ths_886015", is_primary=0)
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT r.sector_code FROM stock_sector_rel r "
            "WHERE r.stock_code='sz.000078' AND r.is_primary=1 LIMIT 1"
        ).fetchone()
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_sector_rel WHERE stock_code='sz.000078'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row == ('881143.TI',) and n == 2


def test_ensure_sector_meta_keeps_name():
    """ensure_sector_meta 不覆盖已有板块名；缺失时插入占位行。"""
    db = _fresh_db()
    conn = db._connect()
    try:
        conn.execute("INSERT INTO sector_meta (sector_code, sector_name, "
                     "parent_type, base_code, is_active) "
                     "VALUES ('886015.TI', '创新药', '行业/概念', '886015', 1)")
        conn.commit()
    finally:
        conn.close()
    db.ensure_sector_meta('886015.TI')
    db.ensure_sector_meta('886999.TI')  # 不存在 → 插占位
    conn = db._connect()
    try:
        rows = dict(conn.execute(
            "SELECT sector_code, sector_name FROM sector_meta").fetchall())
    finally:
        conn.close()
    assert rows['886015.TI'] == '创新药', f"板块名被覆盖: {rows}"
    assert rows['886999.TI'] == '886999.TI'


def test_ths_sector_code_normalize():
    """ThsClient 板块代码归一：ths_ 前缀/裸代码 → .TI（fuyao 只认 .TI）。"""
    from mystery.adapters.ths import ThsClient
    assert ThsClient._to_sector_ti('ths_886015') == '886015.TI'
    assert ThsClient._to_sector_ti('886015') == '886015.TI'
    assert ThsClient._to_sector_ti('886015.TI') == '886015.TI'

