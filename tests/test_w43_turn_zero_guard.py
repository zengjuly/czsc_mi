"""W43 假 0 换手治理回归。

背景（2026-09-17 实测）：上游缺数以 0 填充换手，全库历史行 ~97% 为假 0。
写端口径（db._t：0<t<80 有效）+ ffill 锚点 + QA 诚实化 + 清洗脚本。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from mystery.store.db import _t


def test_t_rejects_fake_zero():
    assert _t(0) is None
    assert _t(0.0) is None
    assert _t(-1.0) is None
    assert _t(80.0) is None
    assert _t(99.9) is None
    assert _t("") is None
    assert _t("x") is None
    assert _t(None) is None


def test_t_keeps_valid():
    assert _t(12.5) == 12.5
    assert _t(79.9) == 79.9
    assert _t("3.2") == 3.2


def test_upsert_kline_swallows_fake_zero(tmp_path):
    """上游 0/越界换手入库变 NULL；有效值正常写。"""
    import pandas as pd
    from mystery.store.db import MysteryDB
    db = MysteryDB(db_path=str(tmp_path / "w43.db"))
    df = pd.DataFrame([
        {"日期": "2026-09-15", "开盘价": 10.0, "最高价": 11.0, "最低价": 9.5,
         "收盘价": 10.5, "成交量": 1e6, "成交额": 1e7, "换手率": 0.0},
        {"日期": "2026-09-16", "开盘价": 10.5, "最高价": 11.5, "最低价": 10.0,
         "收盘价": 11.0, "成交量": 2e6, "成交额": 2e7, "换手率": 1.25},
    ])
    db.upsert_kline(df, "sh.600519", "daily")
    c = db._connect()
    try:
        rows = c.execute(
            "SELECT substr(date,1,10), turn FROM stock_kline_data "
            "WHERE code='sh.600519' ORDER BY date").fetchall()
    finally:
        c.close()
    assert rows == [("2026-09-15", None), ("2026-09-16", 1.25)]


def test_turnover_coverage_excludes_fake_zero(tmp_path):
    """QA 覆盖率：turn=0 不算已填（诚实口径）。"""
    from mystery.store.db import MysteryDB
    db = MysteryDB(db_path=str(tmp_path / "w43b.db"))
    c = db._connect()
    try:
        c.execute("INSERT INTO stock_kline_data (code,date,period,close,volume,"
                  "turn,turn_source) VALUES "
                  "('sh.600000','2026-09-15','daily',10,1000,0,'duck')")
        c.execute("INSERT INTO stock_kline_data (code,date,period,close,volume,"
                  "turn,turn_source) VALUES "
                  "('sh.600000','2026-09-16','daily',10,1000,2.5,'duck')")
        c.commit()
    finally:
        c.close()
    cov = db.turnover_coverage()
    assert cov["rows"] == 2
    assert cov["with_turn"] == 1          # 假 0 不算覆盖
    assert cov["coverage"] == 0.5
    assert cov["by_source"].get("duck") == 2
