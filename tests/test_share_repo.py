"""P2 延伸：share_repo 股本快照读写回归。

用临时 SQLite 验证 float_share_snapshot 的 upsert/get 往返、NULL 归一、
as_of 时间窗取最新、以及批量 upsert_float_share_many。行为须与拆分前一致。
"""
import os
import sys

import pandas as pd  # noqa: F401  (确保 pandas 可用，与运行环境一致)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod


def test_upsert_get_roundtrip(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    db.upsert_float_share("600519.SH", "2024-01-01", 1.9e12, 1680.0, 1.256e9)
    got = db.get_float_share("600519.SH")
    assert got is not None
    assert got["as_of"] == "2024-01-01"
    assert got["float_shares"] == 1.256e9
    assert got["float_market_cap"] == 1.9e12
    assert got["last_price"] == 1680.0


def test_null_normalized_to_none(tmp_path):
    """缺失/None 的市值与价格须写 NULL（_f 归一），读回为 None 而非 0。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    db.upsert_float_share("600519.SH", "2024-01-01", None, None, 1.256e9)
    got = db.get_float_share("600519.SH")
    assert got["float_market_cap"] is None
    assert got["last_price"] is None
    assert got["float_shares"] == 1.256e9


def test_as_of_window_picks_latest_le(tmp_path):
    """get_float_share 须取 as_of <= as_of_max 的最新一条。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    db.upsert_float_share("600519.SH", "2024-01-01", None, None, 1.0e9)
    db.upsert_float_share("600519.SH", "2024-02-01", None, None, 2.0e9)
    before = db.get_float_share("600519.SH", as_of_max="2024-01-15")
    assert before["as_of"] == "2024-01-01"
    assert before["float_shares"] == 1.0e9
    latest = db.get_float_share("600519.SH")
    assert latest["as_of"] == "2024-02-01"
    assert latest["float_shares"] == 2.0e9


def test_upsert_float_share_many(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    rows = [
        ("600519.SH", "2024-01-01", 1.9e12, 1680.0, 1.256e9),
        ("000001.SZ", "2024-01-01", 3.0e11, 12.0, 1.9e10, "manual"),
    ]
    n = db.upsert_float_share_many(rows)
    assert n == 2
    a = db.get_float_share("600519.SH")
    b = db.get_float_share("000001.SZ")
    assert a["float_shares"] == 1.256e9
    assert b["float_shares"] == 1.9e10
    # 6 元组（含 source）路径不报错即视为通过；get_float_share 读取面不暴露 source


def test_get_missing_returns_none(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    assert db.get_float_share("999999.SH") is None
