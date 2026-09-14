"""W22：换手率治理（006.md 阶段 2）离线回归。

覆盖：派生公式（含手→股、越界丢弃）、股本推算、跳变校验、ffill 洞规则、
DB 派生链（calc_float 不覆盖已有 turn、无股本保持 unknown）。不打 fuyao HTTP。
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from mystery.core.turnover import (derived_turn, estimate_float_shares,
                                   ffill_gaps, shares_changed_enough)
from mystery.services.sync_turnover import sync_turnover
from mystery.store.db import MysteryDB


# ---------------- core 纯函数 ----------------

def test_derived_turn_basic():
    # 1亿股流通，日量 200万股 → 2%
    assert derived_turn(2e6, 1e8) == 2.0
    # 手单位：20000手 = 200万股
    assert derived_turn(2e4, 1e8, unit='lot') == 2.0


def test_derived_turn_invalid():
    assert derived_turn(None, 1e8) is None
    assert derived_turn(1e6, None) is None
    assert derived_turn(0, 1e8) is None
    assert derived_turn(1e6, 0) is None
    # 越界 >80% 丢弃（数据错误，不伪造）
    assert derived_turn(9e7, 1e8) is None


def test_estimate_float_shares():
    # 茅台口径：流通市值 15975.5亿 / 1277.96 → ≈12.5亿股
    s = estimate_float_shares(1.5975e12, 1277.96)
    assert s and 1.2e9 < s < 1.3e9
    assert estimate_float_shares(0, 10) is None
    assert estimate_float_shares(100, None) is None


def test_shares_changed_threshold():
    assert shares_changed_enough(1e9, 1.05e9) is True    # 5% > 3% → 覆盖
    assert shares_changed_enough(1e9, 1.01e9) is False   # 1% ≤3% → 不覆盖
    assert shares_changed_enough(1e9, 1.1e9) is True
    assert shares_changed_enough(None, 1e9) is True      # 无旧值 → 写
    assert shares_changed_enough(1e9, None) is False


def test_ffill_gaps_rules():
    ser = [('d1', 1.0), ('d2', None), ('d3', None), ('d4', 2.0),
           ('d5', None), ('d6', None), ('d7', None), ('d8', None),
           ('d9', None), ('d10', None), ('d11', 3.0)]
    out, filled = ffill_gaps(ser, max_gap=5)
    # d2/d3 洞 2 日 → 填 1.0
    assert dict(out)['d2'] == 1.0 and dict(out)['d3'] == 1.0
    # d5..d10 洞 6 日 >5 → 保持 NULL
    assert dict(out)['d7'] is None
    assert filled == ['d2', 'd3']


def test_ffill_leading_gap_not_filled():
    ser = [('d1', None), ('d2', 1.0), ('d3', None), ('d4', 2.0)]
    out, filled = ffill_gaps(ser)
    assert dict(out)['d1'] is None      # 开头无前值不填
    assert dict(out)['d3'] == 1.0


# ---------------- DB 派生链 ----------------

@pytest.fixture()
def turnover_db(tmp_path):
    db_path = str(tmp_path / 't.db')
    db = MysteryDB(db_path)
    # 两只票：一只股本已知、一只无快照；turn 已有值的不许被覆盖
    df = pd.DataFrame({
        'code': ['sh.600000', 'sh.600000', 'sh.600000', 'sh.600001', 'sh.600001'],
        'date': ['2026-09-11', '2026-09-10', '2026-09-09',
                 '2026-09-11', '2026-09-10'],
        'period': ['daily'] * 5,
        'open': [1] * 5, 'high': [1] * 5, 'low': [1] * 5,
        'close': [1] * 5,
        'volume': [2e6, 3e6, 5e6, 4e6, 4e6],
        'amount': [1e7] * 5,
        'turn': [None, 1.5, None, None, None],   # 09-10 已有 legacy turn
    })
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO stock_kline_data (code,date,period,open,high,low,close,"
        "volume,amount,turn) VALUES (?,?,?,?,?,?,?,?,?,?)",
        list(df.itertuples(index=False, name=None)))
    conn.commit()
    conn.close()
    db.upsert_float_share('600000.SH', '2026-09-09',
                          float_market_cap=1e10, last_price=10.0,
                          float_shares=1e9)
    return db


def test_sync_turnover_calc_float_and_protection(turnover_db):
    qa = sync_turnover('2026-09-11', db=turnover_db)
    assert qa['calc_float'] == 1           # 600000 当日 2e6/1e9=0.2%
    conn = sqlite3.connect(turnover_db.db_path)
    row = conn.execute("SELECT turn, turn_source FROM stock_kline_data "
                       "WHERE code='sh.600000' AND date='2026-09-11'").fetchone()
    assert row == (0.2, 'calc_float')
    # 已有 turn 的行未被触碰（legacy 保护）
    old = conn.execute("SELECT turn, turn_source FROM stock_kline_data "
                       "WHERE code='sh.600000' AND date='2026-09-10'").fetchone()
    assert old == (1.5, None)
    # 无股本快照 → 保持 unknown（不伪造）
    u = conn.execute("SELECT turn FROM stock_kline_data "
                     "WHERE code='sh.600001'").fetchall()
    assert all(r[0] is None for r in u)
    conn.close()
    assert qa['no_shares'] == 1


def test_sync_turnover_idempotent(turnover_db):
    sync_turnover('2026-09-11', db=turnover_db)
    qa2 = sync_turnover('2026-09-11', db=turnover_db)
    # 第二次：当日本已派生，不再计入 null_rows
    assert qa2['calc_float'] == 0
    conn = sqlite3.connect(turnover_db.db_path)
    n = conn.execute("SELECT COUNT(*) FROM stock_kline_data "
                     "WHERE turn_source='calc_float'").fetchone()[0]
    assert n == 1
    conn.close()


def test_turnover_coverage(turnover_db):
    sync_turnover('2026-09-11', db=turnover_db)
    cov = turnover_db.turnover_coverage(days=20)
    assert cov['anchor'] == '2026-09-11'
    assert cov['rows'] == 5
    # with_turn: 09-10 legacy + 09-11 calc_float + ffill(600001 无锚不填) → 2 或 3
    assert cov['coverage'] is not None and cov['coverage'] >= 0.4
    assert 'calc_float' in cov['by_source']
