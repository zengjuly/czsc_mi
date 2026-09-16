"""W34 — 日/周背驰共振（纯函数 + 双周期标签生成，离线 mock）。"""
import numpy as np
import pandas as pd

from mystery.core.bc_resonance import bc_resonance
from mystery.core.models import Bar, BarSeries
from mystery.services import analyze as an


def _chan(d1d='', d1w='', b1d=''):
    return {'1d': {'divergence': d1d, 'bs_flag': b1d},
            '1w': {'divergence': d1w}}


def test_no_div_empty():
    assert bc_resonance({}) == ''
    assert bc_resonance(_chan()) == ''
    assert bc_resonance({'1d': None, '1w': {}}) == ''


def test_single_level():
    assert bc_resonance(_chan(d1d='顶背驰')) == '日线顶背驰'
    assert bc_resonance(_chan(d1w='底背驰')) == '周线底背驰'
    assert bc_resonance(_chan(b1d='三卖')) == ''


def test_resonance():
    assert bc_resonance(_chan(d1d='顶背驰', d1w='顶背驰')) == '日+周共振顶背驰'
    assert bc_resonance(_chan(d1d='底背驰', d1w='底背驰')) == '日+周共振底背驰'


def test_conflict_levels():
    assert bc_resonance(_chan(d1d='顶背驰', d1w='底背驰')) == '日线顶背驰 · 周线底背驰'


def _series(n=300, freq='1d', seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2025-01-01', periods=n)
    close = 10 * np.cumprod(1 + rng.normal(0.0008, 0.015, n))
    bars = []
    for i, d in enumerate(dates):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        bars.append(Bar(dt=str(d.date()), open=o, high=max(o, c) * 1.01,
                        low=min(o, c) * 0.99, close=c, volume=1e6,
                        amount=1e7))
    return BarSeries(symbol='600519.SH', freq=freq, adjust='qfq',
                     bars=bars, source='test')


class _FakeDB:
    def get_chan_cache(self, *a, **k):
        return None

    def set_chan_cache(self, *a, **k):
        pass


class _FakeMarket:
    db = _FakeDB()

    def resample_bars(self, daily, freq):
        return _series(n=60, freq=freq)


def _svc():
    svc = object.__new__(an.AnalysisService)
    svc.cfg = {'chan': {'freqs': ['1d', '1w']}}
    svc.market = _FakeMarket()
    return svc


def test_analyze_chan_signals_both_freqs(monkeypatch):
    import mystery.adapters.czsc_adapter as ca
    from mystery.core.models import ChanStructure

    calls = []

    class FakeAdapter:
        def analyze(self, series, with_signals=False):
            return ChanStructure(freq=series.freq, engine_ver='fake')

        def signal_flags(self, series):
            calls.append(series.freq)
            return ('', '顶背驰', True)

    monkeypatch.setattr(ca, 'CzscAdapter', FakeAdapter)
    out = _svc()._analyze_chan(_series(), with_signals=True)
    assert calls == ['1d', '1w']
    assert out['1d'].divergence == '顶背驰'
    assert out['1w'].divergence == '顶背驰'
    assert bc_resonance({k: v.to_dict() for k, v in out.items()}) == '日+周共振顶背驰'


def test_analyze_chan_no_signals_when_off(monkeypatch):
    import mystery.adapters.czsc_adapter as ca
    from mystery.core.models import ChanStructure

    calls = []

    class FakeAdapter:
        def analyze(self, series, with_signals=False):
            return ChanStructure(freq=series.freq, engine_ver='fake')

        def signal_flags(self, series):
            calls.append(series.freq)
            return ('', '顶背驰', True)

    monkeypatch.setattr(ca, 'CzscAdapter', FakeAdapter)
    out = _svc()._analyze_chan(_series(), with_signals=False)
    assert calls == []
    assert out['1d'].divergence == ''
