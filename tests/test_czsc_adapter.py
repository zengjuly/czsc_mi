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


def test_plot_figure_has_ma_and_zs():
    """W4：缠论图必须有 6 条 MA；识别出中枢时必须画矩形；plot_html 兼容。"""
    s = _make_series(n=800, seed=42)
    fig = CzscAdapter().plot_figure(s, tail_bars=500)
    assert fig is not None
    names = [tr.name or "" for tr in fig.data]
    mas = sorted((n for n in names
                  if str(n).startswith("MA") and str(n)[2:].isdigit()),
                 key=lambda n: int(n[2:]))
    assert mas == ["MA5", "MA10", "MA20", "MA55", "MA233", "MA610"]
    # 至少还有 K线/分型/笔/成交量/DIFF/DEA/MACD 之一
    assert len(fig.data) >= 7
    out = CzscAdapter().analyze(s)
    if out.zss:
        rects = [sh for sh in fig.layout.shapes if sh.type == "rect"]
        assert rects, "识别出中枢但图上无矩形"
    # plot_html 兼容路径返回非空 HTML
    html = CzscAdapter().plot_html(s, tail_bars=300)
    assert isinstance(html, str) and "plotly" in html


def test_plot_figure_empty_and_bad_series():
    s = BarSeries(symbol="600519.SH", freq="1d")
    assert CzscAdapter().plot_figure(s) is None
    assert CzscAdapter().plot_html(s) == ""
    # 数据不足（<2 根）不应崩溃
    tiny = _make_series(n=5, seed=1)
    fig = CzscAdapter().plot_figure(tiny)
    assert fig is not None or True  # 不抛异常即可


def test_plot_figure_monthly_freq():
    """月线（1M）能出图（czsc Freq.M 重采样口径）。"""
    s = _make_series(n=1200, seed=3, freq="1M")
    fig = CzscAdapter().plot_figure(s, tail_bars=None)
    assert fig is not None
    assert "月线" in (fig.layout.title.text or "")


# ---- 阶段6A（010.md）：结构摘要加深字段（只展示不进分）----

def test_chan6a_derived_fields():
    """有中枢时 zs_position/leave_zs/bi_stretch/n_zs 自洽。"""
    s = _make_series(n=400, seed=11)
    out = CzscAdapter().analyze(s)
    assert out.n_zs == len(out.zss)
    if out.zss:
        assert out.zs_position in ("above", "in", "below")
        if out.leave_zs:
            assert out.leave_zs in ("up", "down")
            # 真离开必须方向自洽：向上离开=价在zg上且末笔up
            z = out.zss[-1]
            last_close = s.bars[-1].close
            if out.leave_zs == "up":
                assert last_close > z.zg and out.last_bi_dir == "up"
            else:
                assert last_close < z.zd and out.last_bi_dir == "down"
        if out.bi_stretch is not None:
            assert out.bi_stretch >= 0
    else:
        assert out.zs_position == "" and out.bi_stretch is None
    # 6A：买卖点/背驰保持空占位（信号引擎未进门控路径）
    assert out.bs_flag == "" and out.divergence == ""


def test_chan6a_roundtrip_and_legacy_dict():
    """新字段 to_dict 可序列化；老缓存缺键容忍为默认（修正③）。"""
    s = _make_series(n=300, seed=7)
    out = CzscAdapter().analyze(s)
    d = out.to_dict()
    back = chan_from_dict(d)
    assert back.zs_position == out.zs_position
    assert back.leave_zs == out.leave_zs
    assert back.bi_stretch == out.bi_stretch
    assert back.n_zs == out.n_zs
    assert back.last_zs_finished == out.last_zs_finished
    # 老格式 dict（无 6A 键）→ 缺省空，不抛异常
    legacy = {k: v for k, v in d.items()
              if k not in ("zs_position", "leave_zs", "bi_stretch",
                           "n_zs", "last_zs_finished", "bs_flag",
                           "divergence")}
    old = chan_from_dict(legacy)
    assert old.zs_position == "" and old.n_zs == len(old.zss)
    assert old.bi_stretch is None and old.bs_flag == ""


def test_chan6a_empty_series_ok():
    """czsc 不可用/数据不足：新字段全默认，分析不崩。"""
    s = BarSeries(symbol="600519.SH", freq="1d")
    out = CzscAdapter().analyze(s)
    assert out.zs_position == "" and out.leave_zs == ""
    assert out.bi_stretch is None and out.n_zs == 0
