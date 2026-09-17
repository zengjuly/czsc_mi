"""W35 审查修复回归（P0-A/B/C）：
A. Bar.turnover 缺失语义 = None；_turn_opt 清洗无效值（None/0/NaN/越界）；
   _avg_turnover_20 只计有效值，0 不再拉低均值。
B. engine_ver=unavailable 的缠论结构不写 chan_cache（可正常写入路径不受影响）。
C. upsert_kline / upsert_kline_many 同事务失效该票 chan_cache。
全部临时 sqlite + stub，不打网络、不碰生产库。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mystery.core.models import Bar, BarSeries, ChanStructure, MarketContext, \
    MysteryBreakdown
from mystery.services import analyze as A
from mystery.store.db import MysteryDB


def _bars(turnovers):
    base = dt.datetime(2026, 9, 1)
    return [Bar(dt=base + dt.timedelta(days=i), open=10, high=11, low=9,
                close=10, volume=1000, turnover=t)
            for i, t in enumerate(turnovers)]


# ---------------- A: 缺失语义 ----------------

def test_bar_turnover_default_none():
    """默认构造的 Bar 换手必须是 None（缺数）而非 0.0（真数）。"""
    b = Bar(dt=dt.datetime(2026, 9, 1), open=1, high=1, low=1, close=1)
    assert b.turnover is None
    assert b.to_dict()['turnover'] is None


def test_turn_opt_rejects_fake_values():
    from mystery.adapters.market import _turn_opt
    for bad in (None, 0, 0.0, -1.5, 80.0, 99.0, float('nan'), 'abc'):
        assert _turn_opt(bad) is None, f"{bad!r} 应判无效"
    for ok in (1.5, '2.5', 0.001, 79.9):
        assert _turn_opt(ok) is not None, f"{ok!r} 应判有效"


def test_avg_turnover_20_skips_zero_and_none(monkeypatch):
    """0/None 是缺数冒充，不得进 20 日均值（旧实现会把 0 计入拉低）。
    W47 后小样本需显式降门槛，专注测值过滤语义。"""
    monkeypatch.setenv('MYSTERY_TURNOVER_20_MIN_VALID', '1')
    d = BarSeries(symbol='x', freq='1d', adjust='qfq',
                  bars=_bars([None, 0.0, 4.0, 2.0]), source='db')
    assert A._avg_turnover_20(d) == 3.0
    # 全无效（None + 0 混合）→ None，不再返回 0.0
    d2 = BarSeries(symbol='x', freq='1d', adjust='qfq',
                   bars=_bars([None, 0.0, 0.0]), source='db')
    assert A._avg_turnover_20(d2) is None


def test_df_to_series_turn_sanitized():
    """_df_to_series 读库行：turn=0/NaN → bar.turnover None。"""
    from mystery.adapters.market import _df_to_series
    df = pd.DataFrame([
        {'日期': '2026-09-01', '开盘价': 10, '最高价': 11, '最低价': 9,
         '收盘价': 10.5, '成交量': 1e6, '成交额': 1e7, '换手率': 0, '涨跌幅': 1},
        {'日期': '2026-09-02', '开盘价': 10, '最高价': 11, '最低价': 9,
         '收盘价': 10.5, '成交量': 1e6, '成交额': 1e7, '换手率': None, '涨跌幅': 1},
        {'日期': '2026-09-03', '开盘价': 10, '最高价': 11, '最低价': 9,
         '收盘价': 10.5, '成交量': 1e6, '成交额': 1e7, '换手率': 3.2, '涨跌幅': 1},
    ])
    s = _df_to_series(df, '600519.SH', '1d', 'qfq', 'db')
    assert [b.turnover for b in s.bars] == [None, None, 3.2]


# ---------------- B: unavailable 不写缓存 ----------------

@pytest.fixture()
def db(tmp_path):
    return MysteryDB(db_path=str(tmp_path / 'test.db'))


class _FakeAdapter:
    def __init__(self, engine_ver):
        self._ver = engine_ver

    def analyze(self, series):
        return ChanStructure(freq=series.freq, engine_ver=self._ver)


def _chan_service(db):
    svc = A.AnalysisService.__new__(A.AnalysisService)
    svc.cfg = {}
    daily = BarSeries(symbol='600519.SH', freq='1d', adjust='qfq',
                      bars=_bars([2.0, 3.0]), source='db')
    svc.market = type('M', (), {
        'db': db,
        'resample_bars': lambda s, d_, f: BarSeries(
            symbol='600519.SH', freq=f, adjust='qfq', bars=[], source='db'),
    })()
    return svc, daily


def test_chan_cache_not_written_when_unavailable(db, monkeypatch):
    import mystery.adapters.czsc_adapter as ca
    monkeypatch.setattr(ca, 'CzscAdapter', lambda: _FakeAdapter('unavailable'))
    svc, daily = _chan_service(db)
    out = svc._analyze_chan(daily, with_signals=False)
    assert out['1d'].engine_ver == 'unavailable'          # 当次仍返回空结构
    n = db._connect().execute(
        "SELECT COUNT(*) FROM chan_cache").fetchone()[0]
    assert n == 0, "unavailable 结构被写进了 chan_cache"


def test_chan_cache_written_when_available(db, monkeypatch):
    import mystery.adapters.czsc_adapter as ca
    monkeypatch.setattr(ca, 'CzscAdapter', lambda: _FakeAdapter('czsc-test'))
    svc, daily = _chan_service(db)
    out = svc._analyze_chan(daily, with_signals=False)
    assert out['1d'].engine_ver == 'czsc-test'
    n = db._connect().execute(
        "SELECT COUNT(*) FROM chan_cache").fetchone()[0]
    assert n == 1, "正常结构应写入 chan_cache（不得过度拦截）"


# ---------------- C: K 线写入失效 chan_cache ----------------

def _seed_chan_row(db, code='600519.SH'):
    db.set_chan_cache(code, '1d', '2026-09-01', 'v', '{"x":1}')


def _kline_df(date='2026-09-01', close=10.5):
    return pd.DataFrame([{'日期': date, '开盘价': 10, '最高价': 11,
                          '最低价': 9, '收盘价': close, '成交量': 1e6,
                          '成交额': 1e7, '换手率': 2.5, '涨跌幅': 1.0}])


def test_upsert_kline_invalidates_chan_cache(db):
    _seed_chan_row(db)
    db.upsert_kline(_kline_df(), 'sh.600519', 'daily')
    assert db.get_chan_cache('600519.SH', '1d', '2026-09-01', 'v') is None


def test_upsert_kline_many_invalidates_chan_cache(db):
    _seed_chan_row(db)
    df = _kline_df()
    df['thscode'] = '600519.SH'
    db.upsert_kline_many(df, 'daily')
    assert db.get_chan_cache('600519.SH', '1d', '2026-09-01', 'v') is None


def test_upsert_kline_only_touches_own_symbol(db):
    _seed_chan_row(db, '600519.SH')
    _seed_chan_row(db, '000001.SZ')
    db.upsert_kline(_kline_df(), 'sh.600519', 'daily')
    assert db.get_chan_cache('600519.SH', '1d', '2026-09-01', 'v') is None
    assert db.get_chan_cache('000001.SZ', '1d', '2026-09-01', 'v') is not None
