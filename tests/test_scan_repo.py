"""P2 延伸：scan_repo 换手率缺口与覆盖率回归。

验证 null_turn_rows（turn IS NULL 且 volume>0 的日K行）、set_turn（仅填空、
绝不覆盖已有值）、turnover_coverage（近 N 日有效 turn 覆盖率）。行为须与拆分前一致。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod


def _kline_df(dates, turn):
    rows = []
    for d in dates:
        rows.append({
            "日期": d, "开盘价": 10.0, "最高价": 11.0, "最低价": 9.5,
            "收盘价": 10.5, "成交量": 1000, "成交额": 10500.0,
            "换手率": turn, "涨跌幅": 0.5,
        })
    return pd.DataFrame(rows)


def test_null_turn_rows_finds_gap(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    # 缺一：turn=0（归一为 NULL）→ 应被 null_turn_rows 捕获
    db.upsert_kline(_kline_df(["2024-01-02"], 0), "sh.600519", "daily")
    # 正常：turn 有效 → 不应出现
    db.upsert_kline(_kline_df(["2024-01-03"], 1.5), "sh.600519", "daily")
    gaps = db.null_turn_rows("2024-01-02")
    assert len(gaps) == 1
    assert gaps[0]["code"] == "sh.600519"
    assert gaps[0]["date"] == "2024-01-02"
    assert gaps[0]["volume"] == 1000
    # 正常日无缺口
    assert db.null_turn_rows("2024-01-03") == []


def test_null_turn_rows_between(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    db.upsert_kline(_kline_df(["2024-01-05", "2024-01-10", "2024-01-15"], 0),
                    "sh.600519", "daily")
    gaps = db.null_turn_rows_between("2024-01-01", "2024-01-12")
    dates = {g["date"] for g in gaps}
    assert dates == {"2024-01-05", "2024-01-10"}
    assert "2024-01-15" not in dates   # 右开区间


def test_set_turn_fills_only_null(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    db.upsert_kline(_kline_df(["2024-01-02"], 0), "sh.600519", "daily")
    db.set_turn("sh.600519", "2024-01-02", 1.23, "auction_final")
    got = db.load_kline("sh.600519", "daily")
    assert got.iloc[0]["turn"] == 1.23

    # 已有值（非 NULL）绝不被覆盖
    db.upsert_kline(_kline_df(["2024-01-03"], 2.0), "sh.600519", "daily")
    db.set_turn("sh.600519", "2024-01-03", 9.99, "auction_final")
    got = db.load_kline("sh.600519", "daily")
    row03 = got[got["date"] == "2024-01-03"].iloc[0]
    assert row03["turn"] == 2.0


def test_turnover_coverage(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "sc.db"))
    db.upsert_kline(_kline_df(["2024-01-02", "2024-01-03", "2024-01-04"], 1.5),
                    "sh.600519", "daily")
    cov = db.turnover_coverage(days=20)
    assert cov["anchor"] == "2024-01-04"
    assert cov["rows"] == 3
    assert cov["with_turn"] == 3
    assert cov["coverage"] == 1.0
