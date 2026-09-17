"""W47 外部 review 修复回归：turnover_20 有效根数门槛 + 缓存还原容错。

覆盖：
1. 近 20 根有效值 < 15 → turnover_20=None（chip_low_unknown 路径）
2. 有效值 >= 15 → 正常均值
3. 环境变量 MYSTERY_TURNOVER_20_MIN_VALID 可调
4. analysis_cache payload 含未知键 → _result_from_payload 不再 TypeError
5. 量比含当日口径钉死（rolling 窗口 = 前4根+当日，量比当日/5日均）
"""
import os

import pandas as pd  # noqa: F401  (环境一致性占位，核心用不到)

from mystery.core.models import Bar, BarSeries, MysteryBreakdown
from mystery.services.analyze import _avg_turnover_20, _result_from_payload


def _mk(turns):
    bars = [Bar(dt=f"2026-09-{i+1:02d}", open=10, high=10, low=10,
                close=10, volume=100, turnover=t)
            for i, t in enumerate(turns)]
    return BarSeries(symbol="600000.SH", freq="1d", bars=bars, source="db")


def test_low_valid_count_returns_none():
    # 20 根里只有 3 根有效换手（停牌/缺数场景）→ 宁缺毋假
    ts = [None] * 17 + [1.0, 2.0, 3.0]
    assert _avg_turnover_20(_mk(ts)) is None


def test_enough_valid_returns_mean():
    ts = [None] * 4 + [2.0] * 16
    assert _avg_turnover_20(_mk(ts)) == 2.0


def test_min_valid_env_override(monkeypatch):
    ts = [None] * 17 + [1.0, 2.0, 3.0]
    monkeypatch.setenv('MYSTERY_TURNOVER_20_MIN_VALID', '3')
    assert _avg_turnover_20(_mk(ts)) == 2.0
    monkeypatch.setenv('MYSTERY_TURNOVER_20_MIN_VALID', '0')  # 非法回落 1
    assert _avg_turnover_20(_mk(ts)) == 2.0


def test_payload_unknown_key_tolerated():
    # W46 前旧 payload 带未来新增键 → 还原必须成功（否则整池静默重算）
    payload = {
        'symbol': '600000.SH', 'score': 55.0,
        'mystery': {'signal': {'综合评分': 55.0}, 'future_field': {'x': 1}},
    }
    res = _result_from_payload(payload)
    assert res.score == 55.0
    assert isinstance(res.mystery, MysteryBreakdown)
    assert not hasattr(res.mystery, 'future_field')


def test_volume_ratio_includes_today():
    # 口径钉死：量比 = 当日量 / rolling(5)含当日均值（对齐金标旧仓）
    from mystery.core.indicators import calculate_volume_ratio
    df = pd.DataFrame({'成交量': [100, 100, 100, 100, 200]},
                      index=pd.date_range('2026-09-14', periods=5))
    out = calculate_volume_ratio(df)
    assert abs(out['量比'].iloc[-1] - 200 / 120) < 1e-9
