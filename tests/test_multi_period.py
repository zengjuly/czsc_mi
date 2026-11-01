"""多周期方向因子（G1）单元测试：纯函数、无需联网/行情数据。

覆盖：
- period_direction：多头/空头/震荡/数据不足判定。
- compute_multi_period_factor：月线基线、周/日同向加分、逆势降权、缺数据降级。
- apply_factor：基线×基准+加分−降权夹 [0,100]；base<=0 不复活否决股。
"""
from __future__ import annotations

import pandas as pd
import pytest

from mystery.core.multi_period import (
    apply_factor,
    compute_multi_period_factor,
    period_direction,
)


def _df(close):
    return pd.DataFrame({"收盘价": list(close)})


def _bull(n=30, start=100.0, step=1.0):
    return _df([start + i * step for i in range(n)])


def _bear(n=30, start=200.0, step=1.0):
    return _df([start - i * step for i in range(n)])


def _flat(n=30, v=100.0):
    return _df([v] * n)


def _short(n=10):
    return _df([100 + i for i in range(n)])


# ---------------- period_direction ----------------

def test_period_direction_bull():
    d = period_direction(_bull()["收盘价"], "1M")
    assert d["trend"] == "多头"
    assert d["dir"] == 1
    assert d["slope_pct"] > 0
    assert d["ma_bull"] is True


def test_period_direction_bear():
    d = period_direction(_bear()["收盘价"], "1M")
    assert d["trend"] == "空头"
    assert d["dir"] == -1
    assert d["slope_pct"] < 0


def test_period_direction_neutral():
    d = period_direction(_flat()["收盘价"], "1M")
    assert d["trend"] == "震荡"
    assert d["dir"] == 0


def test_period_direction_insufficient():
    d = period_direction(_short()["收盘价"], "1M")
    assert d["trend"] == "数据不足"
    assert d["dir"] == 0


def test_period_direction_none():
    assert period_direction(None, "1M")["trend"] == "数据不足"


# ---------------- compute_multi_period_factor ----------------

def test_factor_all_bull():
    """月/周/日 全线多头 → 基线1.15 + 同向加分12 + 逆势0。"""
    f = compute_multi_period_factor(_bull(), _bull(), _bull())
    assert f["月线方向"] == "多头"
    assert f["基线权重"] == 1.15
    assert f["同向共振加分"] == 12.0
    assert f["逆势降权"] == 0.0
    assert f["数据完整"] is True


def test_factor_bear_monthly_reversal():
    """月线空头向下，周/日 多头（逆大周期）→ 基线0.70 + 周/日同向+4 − 逆势18。"""
    f = compute_multi_period_factor(_bull(), _bull(), _bear())
    assert f["月线方向"] == "空头"
    assert f["基线权重"] == 0.70
    # (周,月)反, (日,月)反, (周,日)同  → bonus=4, penalty=18
    assert f["同向共振加分"] == 4.0
    assert f["逆势降权"] == 18.0


def test_factor_missing_monthly():
    """缺月线 → 中性基线1.0，仅周/日同向其判定。"""
    f = compute_multi_period_factor(_bull(), _bull(), None)
    assert f["基线权重"] == 1.0
    # 周/日同向 → +4；无月线对 → 无逆势
    assert f["同向共振加分"] == 4.0
    assert f["逆势降权"] == 0.0
    assert f["数据完整"] is False


def test_factor_all_missing():
    f = compute_multi_period_factor(None, None, None)
    assert f["基线权重"] == 1.0
    assert f["同向共振加分"] == 0.0
    assert f["逆势降权"] == 0.0


def test_factor_neutral_monthly_no_penalty():
    """月线震荡(0) 时，周/日 不论方向都不判逆势（避免误伤）。"""
    f = compute_multi_period_factor(_bull(), _bear(), _flat())
    # (周,月) 月=0 → 跳过; (日,月) 月=0 → 跳过; (周,日) 反 → -9
    assert f["逆势降权"] == 9.0
    assert f["基线权重"] == 1.0


# ---------------- apply_factor ----------------

def test_apply_factor_all_bull():
    f = compute_multi_period_factor(_bull(), _bull(), _bull())
    out = apply_factor(76.0, f)
    assert round(out, 1) == round(76.0 * 1.15 + 12.0, 1)  # 99.4


def test_apply_factor_reversal_downweight():
    f = compute_multi_period_factor(_bull(), _bull(), _bear())
    out = apply_factor(76.0, f)
    # 76*0.70 + 4 - 18 = 39.2
    assert round(out, 1) == 39.2


def test_apply_factor_zero_base_no_resurrect():
    f = compute_multi_period_factor(_bull(), _bull(), _bull())
    assert apply_factor(0.0, f) == 0.0


def test_apply_factor_clamp_upper():
    f = compute_multi_period_factor(_bull(), _bull(), _bull())
    assert apply_factor(100.0, f) <= 100.0


def test_apply_factor_no_factor():
    assert apply_factor(50.0, None) == 50.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
