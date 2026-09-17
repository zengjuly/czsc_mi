"""W41 审查修复回归锁（0.10.12）。

1. 大盘锚 399311.SZ 显示名必须是「国证1000」（曾误标「深证成指」，
   深证成指实为 399001.SZ；399001 上游停更不用，见 market_env docstring）。
2. _avg_turnover_20 上界：0 < turn < 80 才进均值（[None,0,4,90]→4.0）。
3. data_fingerprint 不泄漏 SQLite 连接（调用后线程无残留连接句柄）。
"""
import os
import sqlite3

from mystery.core.market_env import ANCHORS
from mystery.services import analyze as A
from mystery.core.models import Bar, BarSeries


def _series(turns):
    bars = [Bar(dt=f"2026-09-{10+i:02d}", open=1, high=1, low=1,
                close=1, volume=1, amount=1, turnover=t)
            for i, t in enumerate(turns)]
    return BarSeries(symbol="600000.SH", freq="1d", adjust="qfq", bars=bars)


def test_anchor_names():
    d = dict((code, name) for name, code in ANCHORS)
    assert d["000001.SH"] == "上证指数"
    assert d["399311.SZ"] == "国证1000"
    # 防止再漂：399311 绝不允许挂「深证成指」名
    assert all(not (code == "399311.SZ" and "深证" in name)
               for name, code in ANCHORS)


def test_avg_turnover_20_upper_bound():
    # None/0/越界(90) 全部剔除，只剩 4.0（review 建议样本）
    assert A._avg_turnover_20(_series([None, 0, 4.0, 90.0])) == 4.0
    # 80 边界本身也无效（治理口径是开区间）
    assert A._avg_turnover_20(_series([80.0])) is None
    # 正常均值不受影响
    assert A._avg_turnover_20(_series([2.0, 4.0])) == 3.0


def test_data_fingerprint_closes_connection(tmp_path):
    from mystery.store.db import MysteryDB
    db = MysteryDB(db_path=str(tmp_path / "fp.db"))
    before = len(sqlite3.Connection.__subclasses__() and []) or 0  # noqa: F841
    opened = []
    real_connect = sqlite3.connect

    def spy(*a, **k):
        c = real_connect(*a, **k)
        opened.append(c)
        return c

    sqlite3.connect = spy
    try:
        db.data_fingerprint()
        db.data_fingerprint()
    finally:
        sqlite3.connect = real_connect
    # 两次调用后所有句柄都已 close（对已关闭连接执行 SQL 会抛）
    assert len(opened) >= 2, "spy 未捕获到连接（可能绕过了 sqlite3.connect）"
    for c in opened:
        try:
            c.execute("SELECT 1")
            closed_ok = False
        except sqlite3.ProgrammingError:
            closed_ok = True
        assert closed_ok, "data_fingerprint 泄漏了未关闭的 SQLite 连接"
