"""test_czsc_adapter — BarSeries → ChanStructure 转换（mock K 线，断网可测）。"""
import numpy as np
import pandas as pd
import pytest

from mystery.adapters.czsc_adapter import CzscAdapter, chan_from_dict
from mystery.core.models import Bar, BarSeries

czsc = pytest.importorskip("czsc")


def _make_series(n: int = 300, seed: int = 7, freq: str = "1d",
                 with_nan: bool = False) -> BarSeries:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = 10 * np.cumprod(1 + rng.normal(0.0008, 0.015, n))
    bars = []
    for i, d in enumerate(dates):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        hi = max(o, c) * 1.01
        lo = min(o, c) * 0.99
        if with_nan and i == n // 2:
            hi = np.nan
        bars.append(Bar(dt=str(d.date()), open=o, high=hi, low=lo, close=c,
                        volume=1e6, amount=1e7))
    return BarSeries(symbol="600519.SH", freq=freq, adjust="qfq", bars=bars,
                     source="test")


def test_analyze_fields_complete():
    s = _make_series()
    out = CzscAdapter().analyze(s)
    assert out.engine == "czsc"
    assert out.engine_ver
    assert out.freq == "1d"
    assert out.n_fx >= 0
    assert isinstance(out.bis, list)
    assert isinstance(out.zss, list)
    for bi in out.bis:
        assert bi.direction in ("up", "down")
        assert bi.sdt and bi.edt
        assert bi.high >= bi.low
    for zs in out.zss:
        assert zs.zg >= zs.zd and zs.gg >= zs.dd
        assert zs.sdt and zs.edt
    assert out.last_bi_dir in ("", "up", "down")
    assert isinstance(out.last_bi_confirmed, bool)
    assert isinstance(out.in_zs, bool)


def test_analyze_rejects_nan():
    s = _make_series(with_nan=True)
    with pytest.raises(ValueError, match="NaN"):
        CzscAdapter().analyze(s)


def test_empty_series_ok():
    s = BarSeries(symbol="600519.SH", freq="1d")
    out = CzscAdapter().analyze(s)
    assert out.bis == [] and out.zss == []


def test_analyze_multi_weekly():
    s = _make_series(n=400)
    out = CzscAdapter().analyze_multi(s, ["1d", "1w"])
    assert set(out.keys()) == {"1d", "1w"}
    assert out["1w"].freq == "1w"
    assert out["1d"].freq == "1d"


def test_chan_roundtrip_dict():
    s = _make_series()
    out = CzscAdapter().analyze(s)
    d = out.to_dict()
    back = chan_from_dict(d)
    assert back.freq == out.freq
    assert back.n_fx == out.n_fx
    assert len(back.bis) == len(out.bis)
    assert len(back.zss) == len(out.zss)
    assert back.last_bi_dir == out.last_bi_dir
    assert back.in_zs == out.in_zs
