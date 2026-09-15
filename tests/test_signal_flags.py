"""W30b（011.md）— signal_flags 空标签可观测 + 标签进分契约（mock，离线可测）。"""
import types

import numpy as np
import pandas as pd
import pytest

from mystery.adapters.czsc_adapter import CzscAdapter
from mystery.core.models import Bar, BarSeries

czsc = pytest.importorskip("czsc")


def _series(n=300, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = 10 * np.cumprod(1 + rng.normal(0.0008, 0.015, n))
    bars = []
    for i, d in enumerate(dates):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        bars.append(Bar(dt=str(d.date()), open=o, high=max(o, c) * 1.01,
                        low=min(o, c) * 0.99, close=c, volume=1e6,
                        amount=1e7))
    return BarSeries(symbol="600519.SH", freq="1d", adjust="qfq",
                     bars=bars, source="test")


def _patch_native(monkeypatch, behavior):
    """替换 czsc._native.call_signal：behavior(name) -> [Sig] 或抛异常。"""
    fake = types.SimpleNamespace(
        call_signal=lambda name, c, params=None: behavior(name))
    monkeypatch.setattr("czsc._native", fake, raising=False)
    # signal_flags 内是 `from czsc import _native as n`，需 patch 模块属性
    import czsc
    monkeypatch.setattr(czsc, "_native", fake)


class _Sig:
    def __init__(self, value):
        self.value = value


def test_signal_flags_no_labels_ok(monkeypatch):
    """无信号标签但函数正常返回 → signals_ok=True（可区分「无」与「坏」）。"""
    _patch_native(monkeypatch, lambda name: [_Sig("其他_5_20")])
    bs, div, ok = CzscAdapter().signal_flags(_series())
    assert bs == "" and div == "" and ok is True


def test_signal_flags_error_not_ok(monkeypatch):
    """czsc 信号抛异常（版本漂移）→ 空标签 + signals_ok=False（不再静默）。"""
    def boom(name):
        raise RuntimeError("simulated czsc drift")
    _patch_native(monkeypatch, boom)
    bs, div, ok = CzscAdapter().signal_flags(_series())
    assert bs == "" and div == "" and ok is False


def test_signal_flags_second_buy_bottom_bc(monkeypatch):
    """mock 二买 + 底背驰 → 标签正确返回（进分按 010 §9 表 +10/+10）。"""
    def behavior(name):
        if name == "cxt_first_buy_V221126":
            return [_Sig("一买任意_1")]        # 「一买任意」须被排除
        if name == "cxt_second_bs_V240524":
            return [_Sig("二买_5_20")]
        if name == "tas_macd_bc_V230803":
            return [_Sig("底背驰_1")]
        return [_Sig("其他_1")]
    _patch_native(monkeypatch, behavior)
    bs, div, ok = CzscAdapter().signal_flags(_series())
    assert bs == "二买" and div == "底背驰" and ok is True


def test_chan_score_matches_label_table():
    """空标签与无标签规则一致；二买+底背驰按表 +10/+10（权重外口径）。"""
    from mystery.core.scorer import chan_score
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from test_core_rules import _mk_chan

    base = chan_score(_mk_chan('up', zs_position='above',
                               weekly_dir='up'))
    empty = chan_score(_mk_chan('up', zs_position='above', weekly_dir='up',
                                bs_flag='', divergence=''))
    assert empty == base                       # 无标签 → 计 0
    two = chan_score(_mk_chan('up', zs_position='above', weekly_dir='up',
                              bs_flag='二买', divergence='底背驰'))
    assert two == min(100.0, base + 20)        # +10 +10
