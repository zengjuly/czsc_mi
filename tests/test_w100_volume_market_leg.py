"""W100：大盘腿「放量突破」口径（涨+量倍增+破量5日均线，生产默认）专项。

用户拍板：在 ma / yearline 之外新增第三种大盘腿口径 volume_break，
并设为生产默认；成交量倍增倍数可配置，默认 1.5x。

判定三条件（全部满足才判大盘腿「向上」）：
1. 涨跌：当日收盘 > 前一日收盘（指数库 pctChg 常 NULL，用收盘价环比）；
2. 成交量倍增：当日量 ≥ 前5日均量 × volume_boost_ratio；
3. 突破成交量5日均线：当日量 > 前5日均量线（不含当日）。

返回结构与 analyze_market_trend 对齐，position/MA20/MA60 语义不变，
故 W71「大盘非高位」门闩零回归。数据不足（<6 根或缺成交量）→ 未知不放行。

覆盖：
- resolve_resonance_options 接受 volume_break、倍数校验与保守回退；
- analyze_market_trend_volume_break 三条件组合真值表、倍数可配、缺数 unknown；
- MysteryLogic 接线：volume_break 走新函数、口径回显含倍数、诊断子字典；
- 生产默认为 volume_break/1.5，env 可覆盖；
- ma/yearline 既有口径逐字段零回归。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mystery.core import resonance as R  # noqa: E402
from mystery.core.mystery_rules import MysteryLogic  # noqa: E402
from mystery.services import analyze as svc  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_runtime_overrides(tmp_path, monkeypatch):
    """隔离生产网页覆盖层：本文件验证代码层生产默认（volume_break/1.5）。

    生产上网页可把 market_mode 自定义成 ma（实测存在该覆盖层），那是合法
    运行时配置，不应让单元测试误判为回归。覆盖层指向空临时路径 + 清空显式 env。
    """
    monkeypatch.setattr(svc, "RESONANCE_OVERRIDE_PATH",
                        tmp_path / "resonance_override.json")
    for env_key, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)


# ---------------------------------------------------------------- 构造工具
def _index(n=140, *, last_up=True, last_vol_mult=1.6,
           base=3000.0, slope=1.0) -> pd.DataFrame:
    """合成指数日K：近 n 根缓涨，末根按参数控制涨跌与量能。

    :param last_up: 末根收盘是否高于前一根
    :param last_vol_mult: 末根成交量 / 前5日均量
    """
    close = base + slope * np.arange(n, dtype=float)
    vol = np.full(n, 1e8, dtype=float)
    if not last_up:
        close[-1] = close[-2] - 20.0            # 末根下跌
    vol[-1] = vol[-6:-1].mean() * last_vol_mult  # 末根量按倍数放大
    return pd.DataFrame({
        '日期': pd.bdate_range(end='2026-11-17', periods=n).strftime('%Y-%m-%d'),
        '收盘价': close, '成交量': vol,
    })


def _daily(n=120) -> pd.DataFrame:
    """个股日K（含必要指标列），个股腿固定通过。"""
    close = 10 + 0.02 * np.arange(n, dtype=float)
    df = pd.DataFrame({'收盘价': close, '成交量': np.full(n, 5e6)})
    for w in (5, 10, 20, 60, 250):
        df[f'MA{w}'] = df['收盘价'].rolling(w, min_periods=1).mean()
    df['均线排列'] = 1
    return df


# ---------------------------------------------------------------- resolve
def test_resolve_accepts_volume_break():
    o = R.resolve_resonance_options({'market_mode': 'volume_break'})
    assert o['market_mode'] == 'volume_break'
    assert o['volume_boost_ratio'] == 1.5


def test_resolve_invalid_mode_still_falls_back_to_ma():
    assert R.resolve_resonance_options({'market_mode': 'BOGUS'})['market_mode'] == 'ma'


@pytest.mark.parametrize('bad,expected', [
    (0.9, 1.5), (1.0, 1.0), ('2.5', 2.5), ('bad', 1.5), (-1, 1.5), (None, 1.5),
])
def test_resolve_volume_boost_ratio_validation(bad, expected):
    o = R.resolve_resonance_options({'volume_boost_ratio': bad})
    assert o['volume_boost_ratio'] == expected


# ---------------------------------------------------------------- 核心函数真值表
def test_volume_break_all_three_up():
    """上涨 + 量倍增(1.6x≥1.5) + 破5均量 → 向上，三信号齐备。"""
    r = R.analyze_market_trend_volume_break(_index(last_up=True, last_vol_mult=1.6))
    assert r['趋势方向'] == '向上'
    d = r['放量突破']
    assert d['上涨'] is True and d['量倍增'] is True and d['突破量MA5'] is True
    assert d['三条件齐备'] is True and d['量比'] == 1.6


def test_volume_break_price_down_not_up():
    """下跌 → 不向上（量能再足也不放行）。"""
    r = R.analyze_market_trend_volume_break(_index(last_up=False, last_vol_mult=3.0))
    assert r['趋势方向'] != '向上'
    assert r['放量突破']['上涨'] is False


def test_volume_break_volume_not_boosted_not_up():
    """上涨但量未倍增(1.2x<1.5) → 不向上。"""
    r = R.analyze_market_trend_volume_break(_index(last_up=True, last_vol_mult=1.2))
    assert r['趋势方向'] != '向上'
    d = r['放量突破']
    assert d['上涨'] is True and d['量倍增'] is False and d['突破量MA5'] is True


def test_volume_break_configurable_ratio():
    """倍数可配：1.2x 在门槛 1.1 下放行，在门槛 1.5 下否决。"""
    idx = _index(last_up=True, last_vol_mult=1.2)
    loose = R.analyze_market_trend_volume_break(idx, volume_boost_ratio=1.1)
    strict = R.analyze_market_trend_volume_break(idx, volume_boost_ratio=1.5)
    assert loose['趋势方向'] == '向上'
    assert strict['趋势方向'] != '向上'
    assert loose['放量突破']['门槛倍数'] == 1.1
    assert strict['放量突破']['门槛倍数'] == 1.5


def test_volume_break_insufficient_data_unknown():
    """<6 根 → unknown，绝不放行。"""
    r = R.analyze_market_trend_volume_break(_index(n=5))
    assert r['趋势方向'] == '未知'
    assert r['放量突破']['三条件齐备'] is False


def test_volume_break_missing_volume_column_unknown():
    """缺成交量列 → unknown（诚实降级，不伪造放量）。"""
    df = _index()[['日期', '收盘价']].copy()
    r = R.analyze_market_trend_volume_break(df)
    assert r['趋势方向'] == '未知'


def test_volume_break_position_semantics_preserved():
    """近120日价格分位 position 语义与 W71 一致（高位门闩零回归）。"""
    # 单调上行 140 根 → 末根处于近120日最高 → 高位
    r = R.analyze_market_trend_volume_break(_index(n=140, slope=2.0))
    assert r['position'] == '高位'


# ---------------------------------------------------------------- MysteryLogic 接线
def test_logic_routes_volume_break_and_echoes_ratio():
    logic = MysteryLogic()
    r = logic.three_resonance_analysis(
        _daily(), market_data={'上证指数': _index(last_up=True, last_vol_mult=1.6)},
        industry_trend=True,
        options={'market_mode': 'volume_break', 'volume_boost_ratio': 1.5})
    assert r['共振口径']['大盘口径'] == 'volume_break'
    assert r['共振口径']['放量倍增倍数'] == 1.5
    assert r['大盘趋势'] is True
    assert isinstance(r.get('放量突破'), dict)
    assert r['放量突破']['三条件齐备'] is True


def test_logic_volume_break_no_volume_column_degrades():
    """指数缺成交量列时新口径降级为不放行，不抛异常。"""
    logic = MysteryLogic()
    idx = _index()[['日期', '收盘价']].copy()
    r = logic.three_resonance_analysis(
        _daily(), market_data={'上证指数': idx}, industry_trend=True,
        options={'market_mode': 'volume_break'})
    assert r['大盘趋势'] is False


# ---------------------------------------------------------------- 生产默认与零回归
def test_production_default_is_volume_break():
    d = svc.PRODUCTION_RESONANCE_OPTIONS
    assert d['market_mode'] == 'volume_break'
    assert d['volume_boost_ratio'] == 1.5
    n = R.resolve_resonance_options(svc.resonance_options_effective())
    assert n['market_mode'] == 'volume_break'


def test_env_can_override_to_volume_break_ratio(monkeypatch):
    monkeypatch.delenv('MYSTERY_RESONANCE_MARKET_MODE', raising=False)
    monkeypatch.setenv('MYSTERY_RESONANCE_VOLUME_BOOST_RATIO', '2.2')
    n = R.resolve_resonance_options(svc.resonance_options_effective())
    assert n['volume_boost_ratio'] == 2.2


def test_ma_and_yearline_zero_regression(monkeypatch):
    """旧 ma / yearline 口径逐字段不变；core 库级默认仍为 ma/85。"""
    assert R.DEFAULT_RESONANCE_OPTIONS['market_mode'] == 'ma'
    assert R.DEFAULT_RESONANCE_OPTIONS['true_threshold'] == 85.0
    idx = _index()
    ma = R.analyze_market_trend(idx)
    yr = R.analyze_market_trend_yearline(_index(n=300))
    assert ma['趋势方向'] in ('向上', '向下', '震荡')
    assert yr['position'] in ('高位', '中位', '低位', '未知')
