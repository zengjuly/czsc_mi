"""W45 回归：每票日K最多 2000 条，超限滚动替换最旧（用户 2026-09-17 指令）。

写端钩子在 upsert_kline / upsert_kline_many（period='daily'）同事务内剪除；
weekly/monthly 不受限；MYSTERY_DAILY_K_MAX_ROWS 可关（0=关闭）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from mystery.store import db as dbmod


def _mk_df(dates, code=None):
    rows = [{"日期": d, "开盘价": 10.0, "最高价": 11.0, "最低价": 9.5,
             "收盘价": 10.5, "成交量": 1000, "成交额": 10500.0,
             "换手率": 1.5, "涨跌幅": 0.5} for d in dates]
    if code is not None:
        for r in rows:
            r["thscode"] = code
    return pd.DataFrame(rows)


def _dates(n, start_year=2000):
    # n 个升序日期（YYYY-MM-DD，用 pd 生成避免手写日历）
    return [d.strftime("%Y-%m-%d")
            for d in pd.date_range(f"{start_year}-01-01", periods=n, freq="D")]


def test_upsert_kline_rolls_to_cap(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w45.db"))
    cap = dbmod.DAILY_KLINE_MAX_ROWS
    assert cap == 2000
    all_dates = _dates(cap + 50)
    db.upsert_kline(_mk_df(all_dates), "sh.600999", "daily")
    c = db._connect()
    n, mn, mx = c.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM stock_kline_data "
        "WHERE code='sh.600999' AND period='daily'").fetchone()
    assert n == cap
    # 保留的必须是最晚 cap 条（滚动删最旧）
    assert mn == all_dates[-cap]
    assert mx == all_dates[-1]


def test_upsert_kline_many_rolls_to_cap(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w45.db"))
    cap = dbmod.DAILY_KLINE_MAX_ROWS
    df = _mk_df(_dates(cap + 5), code="sz.000001")
    db.upsert_kline_many(df, "daily", code_col="thscode")
    c = db._connect()
    n = c.execute("SELECT COUNT(*) FROM stock_kline_data "
                  "WHERE code='sz.000001' AND period='daily'").fetchone()[0]
    assert n == cap


def test_weekly_not_pruned(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w45.db"))
    cap = dbmod.DAILY_KLINE_MAX_ROWS
    db.upsert_kline(_mk_df(_dates(cap + 5)), "sh.600998", "weekly")
    c = db._connect()
    n = c.execute("SELECT COUNT(*) FROM stock_kline_data "
                  "WHERE code='sh.600998' AND period='weekly'").fetchone()[0]
    assert n == cap + 5


def test_below_cap_untouched(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "w45.db"))
    dates = _dates(100)
    db.upsert_kline(_mk_df(dates), "sh.600997", "daily")
    db.upsert_kline(_mk_df(dates[-1:]), "sh.600997", "daily")  # 重复写同一天
    c = db._connect()
    n = c.execute("SELECT COUNT(*) FROM stock_kline_data "
                  "WHERE code='sh.600997' AND period='daily'").fetchone()[0]
    assert n == 100
