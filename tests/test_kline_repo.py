"""P2 延伸：kline_repo 写入路径向量化回归（替代 iterrows 逐行 + 逐语句 execute）。

用临时 SQLite 构造 DataFrame，upsert 后回读校验，覆盖：中文/英文列归一、
turn 假0→None 的 COALESCE、daily 上限剪除、upsert_kline_many 多票、缺失列兜底。
这些行为须与拆分前 iterrows 版本逐字节一致（零回归）。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod
from mystery.store._repos._helpers import DAILY_KLINE_MAX_ROWS


def _cn_df(dates, turn=1.5, with_pct=True):
    rows = []
    for i, d in enumerate(dates):
        r = {
            '日期': d, '开盘价': 10.0 + i, '最高价': 11.0 + i, '最低价': 9.5 + i,
            '收盘价': 10.5 + i, '成交量': 1000, '成交额': 10500.0,
            '换手率': turn,
        }
        if with_pct:
            r['涨跌幅'] = 0.5
        rows.append(r)
    return pd.DataFrame(rows)


def test_upsert_kline_cn_roundtrip(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "k.db"))
    dates = [f"2024-01-0{i}" for i in range(1, 4)]
    db.upsert_kline(_cn_df(dates), "sh.600519", "daily")
    df = db.load_kline("sh.600519", "daily")
    assert len(df) == 3
    assert list(df["close"]) == [10.5, 11.5, 12.5]


def test_upsert_kline_en_columns(tmp_path):
    """英文列 → to_cn_columns 归一后再写入，与中文列等价。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "k.db"))
    df = pd.DataFrame([{
        "date": "2024-01-01", "open": 10.0, "high": 11.0, "low": 9.5,
        "close": 10.5, "volume": 1000, "amount": 10500.0, "turn": 1.5, "pctChg": 0.5,
    }])
    db.upsert_kline(df, "sh.600519", "daily")
    got = db.load_kline("sh.600519", "daily")
    assert len(got) == 1
    assert got.iloc[0]["close"] == 10.5


def test_turn_fake_zero_to_none(tmp_path):
    """W43：turn<=0 假0 须归一为 NULL（不被写成 0 污染 COALESCE 与 ffill 锚点）。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "k.db"))
    df = _cn_df(["2024-01-01", "2024-01-02"], turn=2.0)
    df.loc[1, "换手率"] = 0  # 第二天 baostock 缺数假0
    db.upsert_kline(df, "sz.000001", "daily")
    conn = db._connect()
    try:
        turns = [r[0] for r in conn.execute(
            "SELECT turn FROM stock_kline_data WHERE code='sz.000001' ORDER BY date").fetchall()]
    finally:
        conn.close()
    assert turns[0] == 2.0
    assert turns[1] is None


def test_daily_prune_cap(tmp_path):
    """daily 行数超上限滚动删最旧。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "k.db"))
    cap = DAILY_KLINE_MAX_ROWS
    dates = [f"2020-01-{i:02d}" for i in range(1, cap + 6)]
    db.upsert_kline(_cn_df(dates), "sh.600000", "daily")
    df = db.load_kline("sh.600000", "daily")
    assert len(df) == cap
    assert df["date"].iloc[0] == dates[-cap]


def test_upsert_kline_many_multi(tmp_path):
    """多票单事务 executemany：代码归一 + 行数正确。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "k.db"))
    dates = ["2024-01-01", "2024-01-02"]
    df = pd.DataFrame([
        {"thscode": "SH600519", "日期": dates[0], "开盘价": 10.0, "最高价": 11.0,
         "最低价": 9.5, "收盘价": 10.5, "成交量": 1000, "成交额": 10500.0, "换手率": 1.5, "涨跌幅": 0.5},
        {"thscode": "SH600519", "日期": dates[1], "开盘价": 10.1, "最高价": 11.1,
         "最低价": 9.6, "收盘价": 10.6, "成交量": 1000, "成交额": 10500.0, "换手率": 1.5, "涨跌幅": 0.5},
        {"thscode": "SZ000001", "日期": dates[0], "开盘价": 5.0, "最高价": 5.5,
         "最低价": 4.9, "收盘价": 5.2, "成交量": 800, "成交额": 4200.0, "换手率": 1.0, "涨跌幅": 0.3},
    ])
    db.upsert_kline_many(df, "daily", code_col="thscode")
    assert len(db.load_kline("sh.600519", "daily")) == 2
    assert len(db.load_kline("sz.000001", "daily")) == 1


def test_missing_pct_column_no_crash(tmp_path):
    """缺 涨跌幅 列不应 KeyError（等价原 r.get() 返回 None → pctChg 写 NULL）。"""
    db = dbmod.MysteryDB(db_path=str(tmp_path / "k.db"))
    df = _cn_df(["2024-01-01"], with_pct=False)
    db.upsert_kline(df, "sh.600999", "daily")
    got = db.load_kline("sh.600999", "daily")
    assert len(got) == 1
    assert pd.isna(got.iloc[0]["pctChg"])
