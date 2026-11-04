"""P2 延伸：sector_repo.upsert_sector_kline 向量化回归（替代 iterrows 逐行）。

覆盖：中文/英文列归一、回读一致、缺失 volume/amount 列兜底为 0（等价原
r.get() 容错）、ths_ 前缀归一。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod


def test_upsert_sector_kline_cn_roundtrip(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    df = pd.DataFrame([
        {"date": "2024-01-01", "open": 100.0, "high": 101.0, "low": 99.0,
         "close": 100.5, "volume": 1000, "amount": 100000.0},
        {"date": "2024-01-02", "open": 100.5, "high": 102.0, "low": 100.0,
         "close": 101.5, "volume": 1100, "amount": 110000.0},
    ])
    n = db.upsert_sector_kline(df, "ths_881101", "银行")
    assert n == 2
    sd = db.get_sector_kline("881101")  # 入参归一为 ths_881101
    assert sd is not None and len(sd) == 2
    assert list(sd["收盘价"]) == [100.5, 101.5]


def test_upsert_sector_kline_missing_volume(tmp_path):
    """缺 volume/amount 列不应 KeyError，兜底为 0（等价原 r.get() 返回 None → 0）。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    df = pd.DataFrame([
        {"date": "2024-01-01", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
    ])
    n = db.upsert_sector_kline(df, "881101", "银行")
    assert n == 1
    sd = db.get_sector_kline("881101")
    assert sd.iloc[0]["成交量"] == 0
    assert sd.iloc[0]["成交额"] == 0.0


def test_upsert_sector_kline_upsert_idempotent(tmp_path):
    """同 (sector_code, trade_date) UPSERT 幂等：二次写同日期不翻倍。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "s.db"))
    df = pd.DataFrame([
        {"date": "2024-01-01", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
         "volume": 1000, "amount": 100000.0},
    ])
    db.upsert_sector_kline(df, "881101", "银行")
    db.upsert_sector_kline(df, "881101", "银行")  # 二次同键
    sd = db.get_sector_kline("881101")
    assert len(sd) == 1
    assert sd.iloc[0]["收盘价"] == 100.5
