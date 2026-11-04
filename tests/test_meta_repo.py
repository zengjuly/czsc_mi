"""P2 延伸：meta_repo 元数据读取回归。

验证 stock_industry_info 的证券列表/名称读取（含 stock_only 过滤）、
data_fingerprint 指纹聚合（K线末日 + 股本快照最新 as_of）。行为须与拆分前一致。
"""
import os
import sys

import pandas as pd  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod


def _seed_stock(db, rows):
    """rows: [(code, code_name, type), ...] 直接写 stock_industry_info。"""
    conn = db._connect()
    try:
        conn.execute("DELETE FROM stock_industry_info")
        conn.executemany(
            "INSERT INTO stock_industry_info (code, code_name, type) VALUES (?,?,?)",
            rows)
        conn.commit()
    finally:
        conn.close()


def test_get_stock_list_stock_only(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "m.db"))
    _seed_stock(db, [
        ("sh.600519", "贵州茅台", "1"),
        ("sz.000001", "平安银行", None),
        ("sh.000300", "沪深300", "4"),   # 指数，非股票
        ("sz.399001", "深证成指", "2"),  # 指数，非股票
    ])
    stocks = db.get_stock_list(stock_only=True)
    codes = {s["code"] for s in stocks}
    assert "sh.600519" in codes
    assert "sz.000001" in codes
    assert "sh.000300" not in codes
    assert "sz.399001" not in codes
    # stock_only=False 含指数
    allc = {s["code"] for s in db.get_stock_list(stock_only=False)}
    assert "sh.000300" in allc


def test_get_stock_name(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "m.db"))
    _seed_stock(db, [("sh.600519", "贵州茅台", "1")])
    assert db.get_stock_name("sh.600519") == "贵州茅台"
    assert db.get_stock_name("sh.600518") == ""   # 不存在 → 空串


def test_data_fingerprint(tmp_path):
    """W31b：指纹聚合须返回 K线末日 与 股本快照最新 as_of。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "m.db"))
    dates = [f"2024-01-0{i}" for i in range(1, 4)]
    df = pd.DataFrame([{
        "日期": d, "开盘价": 10.0, "最高价": 11.0, "最低价": 9.5,
        "收盘价": 10.5, "成交量": 1000, "成交额": 10500.0,
        "换手率": 1.5, "涨跌幅": 0.5,
    } for d in dates])
    db.upsert_kline(df, "sh.600519", "daily")
    db.upsert_float_share("600519.SH", "2024-01-02", 1.9e12, 1680.0, 1.256e9)
    fp = db.data_fingerprint()
    assert fp["kline_max"] == "2024-01-03"
    assert fp["as_of_max"] == "2024-01-02"
    assert fp["db_path"].endswith("m.db")
