"""P2 延伸：数据质量审计（非破坏性）回归。

直接构造带异常的 stock_kline_data 行，验证 audit_kline 能检出：
重复主键 / 空非正收盘价 / OHLC 逻辑错误 / 量>0 额缺失 / 交易日跳空。
并验证正常数据（upsert_kline 写入）审计为 0 异常。全部只读，不改动任何写入行为。
"""
import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod
from mystery.store.audit import audit_kline

_COLS = ("code", "date", "period", "open", "high", "low", "close",
         "volume", "amount", "turn", "pctChg")


def _raw_insert(db_path, rows):
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            f"INSERT INTO stock_kline_data {_COLS} VALUES ("
            f"{','.join('?' * len(_COLS))})", rows)
        conn.commit()
    finally:
        conn.close()


def test_audit_detects_anomalies(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "audit.db"))
    db_path = db.db_path  # 落盘路径（MysteryDB 可能内存映射，用其属性）
    fp = str(tmp_path / "audit.db")
    # 用与 db 同一文件直接注入异常行
    _raw_insert(fp, [
        # 重复主键（日级）：同日不同时刻，绕过 (code,date,period) 主键，
        # 但 substr(date,1,10) 折叠为同一天 → 审计器按日聚合应检出重复
        ("sh.600519", "2024-01-01 09:30:00", "daily", 10, 11, 9, 10.5, 1000, 10500, 1.5, 0.5),
        ("sh.600519", "2024-01-01 15:00:00", "daily", 10, 11, 9, 10.5, 1000, 10500, 1.5, 0.5),
        # 空收盘价
        ("sz.000001", "2024-01-02", "daily", 10, 11, 9, None, 1000, 10500, 1.5, 0.5),
        # OHLC 逻辑错误：high < low
        ("sz.000002", "2024-01-02", "daily", 10, 5, 9, 8, 1000, 10500, 1.5, 0.5),
        # 量>0 但额缺失（amount NULL）
        ("bj.830001", "2024-01-02", "daily", 10, 11, 9, 10.5, 1000, None, 1.5, 0.5),
    ])
    # 交易日跳空：同一票相邻日期差 >7 天
    _raw_insert(fp, [
        ("sh.600519", "2024-02-01", "daily", 10, 11, 9, 10.5, 1000, 10500, 1.5, 0.5),
    ])

    rep = audit_kline(fp, period="daily")
    assert rep["row_count"] >= 6
    assert rep["checks"]["duplicate_pk"] >= 1
    assert rep["checks"]["null_or_nonpos_close"] >= 1
    assert rep["checks"]["invalid_ohlc"] >= 1
    assert rep["checks"]["volume_no_amount"] >= 1
    assert rep["checks"]["date_gaps_gt_7d"] >= 1


def test_audit_clean_data_zero_anomalies(tmp_path):
    """upsert_kline 写入的正常数据，审计应为 0 异常。"""
    fp = str(tmp_path / "clean.db")
    db = dbmod.MysteryDB(db_path=fp)
    df = pd.DataFrame([{
        "日期": f"2024-01-0{i}", "开盘价": 10.0 + i, "最高价": 11.0 + i,
        "最低价": 9.5 + i, "收盘价": 10.5 + i, "成交量": 1000,
        "成交额": 10500.0 + i * 10, "换手率": 1.5, "涨跌幅": 0.5,
    } for i in range(1, 4)])
    db.upsert_kline(df, "sh.600519", "daily")
    rep = audit_kline(fp, period="daily")
    assert rep["checks"]["duplicate_pk"] == 0
    assert rep["checks"]["null_or_nonpos_close"] == 0
    assert rep["checks"]["invalid_ohlc"] == 0
    assert rep["checks"]["volume_no_amount"] == 0
    assert rep["checks"]["date_gaps_gt_7d"] == 0
