"""W41 审查修复回归锁（0.10.12）。

1. 大盘锚 399311.SZ 显示名必须是「国证1000」（曾误标「深证成指」，
   深证成指实为 399001.SZ；399001 上游停更不用，见 market_env docstring）。
2. _avg_turnover_20 上界：0 < turn < 80 才进均值（[None,0,4,90]→4.0）。
3. data_fingerprint 不泄漏 SQLite 连接（调用后线程无残留连接句柄）。
"""
import os
import sqlite3

import pytest

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


def test_avg_turnover_20_upper_bound(monkeypatch):
    # None/0/越界(90) 全部剔除，只剩 4.0（review 建议样本）
    # W47 后小样本需显式降门槛，专注测边界过滤语义。
    monkeypatch.setenv('MYSTERY_TURNOVER_20_MIN_VALID', '1')
    assert A._avg_turnover_20(_series([None, 0, 4.0, 90.0])) == 4.0
    # 80 边界本身也无效（治理口径是开区间）
    assert A._avg_turnover_20(_series([80.0])) is None
    # 正常均值不受影响
    assert A._avg_turnover_20(_series([2.0, 4.0])) == 3.0


def test_bj_segments_inference():
    """W41 #8：无前缀 43/83/87/92 段推断为 BJ（旧实现只认 92→其余标 SZ）。"""
    from mystery.adapters.codes import normalize_symbol
    assert normalize_symbol("430047") == "430047.BJ"
    assert normalize_symbol("832000") == "832000.BJ"
    assert normalize_symbol("873122") == "873122.BJ"
    assert normalize_symbol("920002") == "920002.BJ"
    # 回归护栏：沪深推断不变
    assert normalize_symbol("600519") == "600519.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300750") == "300750.SZ"
    assert normalize_symbol("510300") == "510300.SH"
    # 显式前后缀优先于推断（SZ 老三板等不受影响）
    assert normalize_symbol("430047.SZ") == "430047.SZ"
    # db._dot 已收编同一路径（W41 代码归一）；显式前缀优先于号段推断（设计内）
    from mystery.store.db import _dot
    assert _dot("430047") == "430047.BJ"
    assert _dot("bj.430047") == "430047.BJ"
    assert _dot("sh.430047") == "430047.SH"  # 前缀冲突以前缀为准（宽容不改）
    assert _dot("乱码XX") == "乱码XX"  # 宽容回退语义保留


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


# ---- 第二批（本周项收口）----

def test_analysis_cache_uses_db_lock(tmp_path):
    """W41 #6：AnalysisCache get/put/invalidate 全程持 db._lock。"""
    import threading
    from unittest.mock import patch as upatch
    from mystery.store.db import MysteryDB
    from mystery.store.cache import AnalysisCache

    db = MysteryDB(db_path=str(tmp_path / "c.db"))
    cache = AnalysisCache(db)
    cache.put("600000.SH", "2026-09-17", "k1", {"score": 1})
    assert cache.get("600000.SH", "2026-09-17", "k1") == {"score": 1}

    # 锁内调用断言：wrap db._lock.acquire，验证 get 路径持锁
    observed = []
    real_lock = db._lock

    class SpyLock:
        def __enter__(self):
            observed.append(True)
            return real_lock.__enter__()

        def __exit__(self, *a):
            return real_lock.__exit__(*a)

    with upatch.object(db, "_lock", SpyLock()):
        cache.get("600000.SH", "2026-09-17", "k1")
        cache.put("600000.SH", "2026-09-17", "k2", {"x": 1})
        cache.invalidate_symbol("600000.SH")
    assert len(observed) == 3, "cache 读写未经过 db._lock"


def test_warn_once_helper():
    """W41 #18：_warn_once 每 key 只警告一次（升级可观测，防扫描刷屏）。"""
    from mystery.services import analyze as A
    A._warned_once.discard("test-key")
    assert "test-key" not in A._warned_once
    A._warn_once("test-key", "boom")
    assert "test-key" in A._warned_once  # 第二次调用只进 set 不再 emit


def test_scan_fail_rate_warning(caplog):
    """W41 #18：失败率>2% 且样本≥20 → warning；正常情况不打。"""
    import logging
    from mystery.services.scan import _warn_fail_rate
    with caplog.at_level(logging.WARNING, logger="mystery.services.scan"):
        _warn_fail_rate([{"score": 1}] * 98, failed=10)  # 9.2% → warn
        assert "失败率" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mystery.services.scan"):
        _warn_fail_rate([{"score": 1}] * 99, failed=1)   # 1% → 静默
        _warn_fail_rate([{"score": 1}] * 5, failed=2)    # 样本<20 → 静默
        assert caplog.text == ""


def test_chan_figure_title_annotations():
    """W41 #10/#12：缠论图标题含 MACD 口径注记；周/月含「未完成」标注。"""
    import pytest
    pytest.importorskip("czsc")
    from mystery.adapters.czsc_adapter import CzscAdapter
    bars = [Bar(dt=f"2026-{4 + i // 28:02d}-{i % 28 + 1:02d}",
                open=1 + i * .01, high=1.1 + i * .01, low=.9 + i * .01,
                close=1 + i * .01, volume=100 + i, amount=100 + i)
            for i in range(120)]
    ser = BarSeries(symbol="600000.SH", freq="1d", adjust="qfq", bars=bars)
    fig = CzscAdapter().plot_figure(ser)
    assert fig is not None
    title = fig.layout.title.text
    assert "czsc 口径" in title          # MACD 双口径标注
    assert "未完成" not in title          # 日线不带未完成标注


# ---- 第四批（定口径三项：#9 标签优先级 / #4 丢棒 / #11 RSI 缺失显式化）----

def test_bs_label_priority_third_over_first(monkeypatch):
    """W41 #9：同时命中一买与三买 → 取三买（最新结构），不再先到先得。"""
    import types
    pytest.importorskip("czsc")  # CI .[dev] 无 czsc（本会话 CI 复现踩坑）
    import czsc
    from mystery.adapters.czsc_adapter import CzscAdapter
    from mystery.core.models import BarSeries, Bar

    def sig(val):
        return types.SimpleNamespace(value=val)

    def behavior(name, c, params=None):
        if name == "cxt_first_buy_V221126":
            return [sig("一买_1")]
        if name == "cxt_second_bs_V240524":
            return [sig("其他_1")]
        if name == "cxt_third_bs_V230319":
            return [sig("三买_5_20")]
        return [sig("其他_1")]

    fake = types.SimpleNamespace(call_signal=behavior)
    monkeypatch.setattr(czsc, "_native", fake)

    import numpy as np
    rng = np.random.default_rng(7)
    dates = ["2025-%02d-%02d" % (i // 28 + 1, i % 28 + 1) for i in range(300)]
    close = 10 * np.cumprod(1 + rng.normal(0.0008, 0.015, 300))
    bars = [Bar(dt=d, open=float(c), high=float(c) * 1.01, low=float(c) * .99,
                close=float(c), volume=1e6, amount=1e7)
            for d, c in zip(dates, close)]
    s = BarSeries(symbol="600519.SH", freq="1d", adjust="qfq", bars=bars,
                  source="test")
    bs, div, ok = CzscAdapter().signal_flags(s)
    assert bs == "三买" and ok is True


def test_df_to_series_drops_bad_ohlc_rows():
    """W41 #4：close/任一 OHLC 为 NaN → 丢该根，不填 0；缺列容忍不变。"""
    import numpy as np
    import pandas as pd
    from mystery.adapters.market import _df_to_series

    df = pd.DataFrame({
        "日期": pd.date_range("2026-01-01", periods=5),
        "开盘价": [10, 11, 12, 13, 14],
        "最高价": [10.5, 11.5, 12.5, 13.5, 14.5],
        "最低价": [9.5, 10.5, 11.5, 12.5, 13.5],
        "收盘价": [10.2, np.nan, 12.2, 13.2, 14.2],   # 第2根缺 close
        "成交量": [100, 100, 100, 100, 100],
        "成交额": [1000, 1000, 1000, 1000, 1000],
    })
    s = _df_to_series(df, "600000.SH", "1d", "qfq", "test")
    assert len(s.bars) == 4
    assert all(b.close > 0 for b in s.bars)
    assert [b.dt for b in s.bars] == ["2026-01-01", "2026-01-03",
                                      "2026-01-04", "2026-01-05"]
    # 全 NaN 也不炸（丢光 → 空序列）
    df2 = df.copy()
    df2["收盘价"] = np.nan
    assert _df_to_series(df2, "600000.SH", "1d", "qfq", "test").bars == []


def test_rsi_missing_marked_not_silent():
    """W41 #11：RSI 末两行 NaN → checklist 不判分、详情含缺失标注（行为=旧 notna 守卫，新增可见性）。"""
    import numpy as np
    import pandas as pd
    from mystery.core.mystery_rules import MysteryLogic

    n = 80
    df = pd.DataFrame({
        "最高价": np.linspace(11, 20, n), "最低价": np.linspace(10, 19, n),
        "收盘价": np.linspace(10.5, 19.5, n), "成交量": [1000] * n,
        "成交额": [1e6] * n, "涨跌幅": [0.01] * n, "MA60": [None] * 70 + [15.0] * 10,
        "换手率": [None] * n, "量比": [1.0] * n,
    })
    df["RSI"] = [np.nan] * (n - 2) + [np.nan, np.nan]  # 末两行缺数
    cl = MysteryLogic().main_bull_wave_checklist(df)
    assert cl["RSI>50继续走强"] is False
    assert any("RSI缺失" in d for d in cl["详情"])
    # 有值路径不受影响
    df2 = df.copy()
    df2["RSI"] = 60.0
    cl2 = MysteryLogic().main_bull_wave_checklist(df2)
    assert cl2["RSI>50继续走强"] is True
