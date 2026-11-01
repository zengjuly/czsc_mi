"""高信噪比形态检测（G4）单元测试：纯函数、无需联网/行情数据。

覆盖：
- detect_bullish_engulfing：强势反包阳（昨阴+今阳+吞没+放量+强势位置）。
- detect_gap：向上跳空缺口三类（突破/持续/衰竭）+ 三日不补。
- detect_low_ground_volume：低位（回撤≥门闩）+ 量能压缩（近20日地量+低于60日均量+长期缩量）。
- scan_signals.classify / filter_by_signal：新形态进入扫描信号维度。
- scorer.combine(pattern_weight)：形态轻加权（默认关）。
"""
from __future__ import annotations

from mystery.core.models import Bar
from mystery.core import patterns_adv as P
from mystery.core import scan_signals
from mystery.core import scorer
from mystery.core.models import MysteryBreakdown


def _bar(o, h, l, c, v=1000.0):
    return Bar(dt="2026-01-01", open=o, high=h, low=l, close=c, volume=v)


# ---------------- 强势反包阳 ----------------

def test_engulfing_strong():
    """昨阴(10→8) + 今阳低开反包(7.5→10.5，吞没昨开10) + 放量 + 站上MA5。"""
    bars = [_bar(9, 10, 8.5, 10) for _ in range(6)]   # 震荡高位
    bars[-2] = _bar(10, 10.2, 8.0, 8.0)               # 昨阴 10→8
    bars[-1] = _bar(7.5, 10.6, 7.4, 10.5, v=2000.0)   # 今阳 低开反包，放量，吞没昨开
    r = P.detect_bullish_engulfing(bars)
    assert r["basic"] is True
    assert r["hit"] is True
    assert r["detail"]["放量确认"] is True


def test_engulfing_no_volume():
    """缺放量确认 → 仅基础吞没，非强势（hit=False）。"""
    bars = [_bar(9, 10, 8.5, 10) for _ in range(6)]
    bars[-2] = _bar(10, 10.2, 8.0, 8.0)
    bars[-1] = _bar(7.5, 10.6, 7.4, 10.5, v=200.0)    # 缩量
    r = P.detect_bullish_engulfing(bars)
    assert r["basic"] is True
    assert r["hit"] is False


def test_engulfing_not_bearish_prev():
    """昨非阴线 → 不构成反包阳。"""
    bars = [_bar(9, 10, 8.5, 10) for _ in range(6)]
    bars[-2] = _bar(8, 8.5, 7.5, 9.0)                 # 昨阳
    bars[-1] = _bar(7.5, 9.5, 7.4, 9.2)
    assert P.detect_bullish_engulfing(bars)["basic"] is False


def test_engulfing_insufficient():
    assert P.detect_bullish_engulfing([_bar(1, 1, 1, 1)])["hit"] is False


# ---------------- 缺口三类 ----------------

def _flat_bars(n, base=10.0, step=0.0):
    out = []
    for i in range(n):
        p = base + i * step
        out.append(_bar(p, p + 0.2, p - 0.2, p))
    return out


def test_gap_breakout():
    """长期横盘后向上跳空 → 突破缺口。"""
    bars = _flat_bars(40, base=10.0, step=0.0)        # 横盘
    bars[-2] = _bar(10, 10.2, 9.8, 10.0)
    bars[-1] = _bar(10.6, 11.0, 10.5, 10.9)           # 今开10.6 > 昨高10.2 → 跳空
    r = P.detect_gap(bars)
    assert r["gap_up"] is True
    assert r["gap_type"] == "突破"


def test_gap_exhaustion():
    """连续大涨后大幅跳空 → 衰竭缺口。"""
    bars = [Bar(dt="2026-01-01", open=10 + i * 0.5, high=10 + i * 0.5 + 0.1,
                low=10 + i * 0.5 - 0.1, close=10 + i * 0.5) for i in range(40)]
    bars[-2] = _bar(29.5, 29.7, 29.3, 29.5)
    bars[-1] = _bar(31.0, 31.5, 30.9, 31.3)           # 跳空幅度大 + 前20日已涨~10→>35%
    r = P.detect_gap(bars)
    assert r["gap_up"] is True
    assert r["gap_type"] == "衰竭"


def test_gap_continuation():
    """温和上涨途中跳空（非横盘、非极端大涨）→ 持续缺口。"""
    bars = [Bar(dt="2026-01-01", open=10 + i * 0.2, high=10 + i * 0.2 + 0.1,
                low=10 + i * 0.2 - 0.1, close=10 + i * 0.2) for i in range(40)]
    bars[-2] = _bar(17.5, 17.7, 17.3, 17.5)
    bars[-1] = _bar(18.0, 18.4, 17.9, 18.3)           # 温和跳空
    r = P.detect_gap(bars)
    assert r["gap_up"] is True
    assert r["gap_type"] == "持续"


def test_gap_none():
    bars = _flat_bars(10, base=10.0, step=0.1)
    assert P.detect_gap(bars)["gap_up"] is False


# ---------------- 低位地量 ----------------

def test_low_ground_volume_hit():
    """长期缩量横盘 + 近120日深跌 → 低位地量。"""
    import math
    bars = []
    # 前期高位后深跌到低位横盘，量能持续萎缩
    price = 20.0
    for i in range(60):
        # 价格低位横盘 ~10
        p = 10.0 + 0.02 * math.sin(i / 5)
        # 量能：前高后低（长期缩量），末根创地量
        v = 500.0 if i < 20 else (300.0 if i < 40 else (100.0 if i < 59 else 80.0))
        bars.append(_bar(p, p + 0.1, p - 0.1, p, v=v))
    # 抬高 high_120：在序列前追加一段高价（用更高 close 的历史）
    hist = [_bar(20 - j * 0.01, 20 - j * 0.01 + 0.1, 20 - j * 0.01 - 0.1, 20 - j * 0.01)
            for j in range(70, 0, -1)]
    bars = hist + bars
    r = P.detect_low_ground_volume(bars)
    assert r["低位"] is True
    assert r["量能压缩"] is True
    assert r["hit"] is True


def test_low_ground_volume_no_low():
    """高位放量的票 → 非低位地量。"""
    bars = [_bar(50 + 0.01 * i, 50.1 + 0.01 * i, 49.9 + 0.01 * i, 50 + 0.01 * i,
                 v=2000.0) for i in range(80)]
    r = P.detect_low_ground_volume(bars)
    assert r["hit"] is False


# ---------------- scan_signals 集成 ----------------

def _result_with_signals(sig: dict) -> dict:
    return {"mystery": {"signal": sig}, "true_resonance": False}


def test_scan_classify_new_signals():
    d = _result_with_signals({
        "强势反包阳": True, "向上跳空": True, "缺口类型": "突破", "低位地量": True,
    })
    out = scan_signals.classify(d)
    assert out["bull_engulf"] is True
    assert out["gap_breakout"] is True
    assert out["gap_continuation"] is False
    assert out["gap_exhaustion"] is False
    assert out["low_ground_volume"] is True
    assert "强势反包阳" in out["labels"]
    assert "突破缺口" in out["labels"]
    assert "低位地量" in out["labels"]


def test_scan_classify_exhaustion_label():
    d = _result_with_signals({"向上跳空": True, "缺口类型": "衰竭"})
    out = scan_signals.classify(d)
    assert out["gap_exhaustion"] is True
    assert "衰竭缺口" in out["labels"]


def test_scan_filter_by_signal():
    res = [
        scan_signals.classify(_result_with_signals({"强势反包阳": True, "缺口类型": "持续", "低位地量": False})),
        scan_signals.classify(_result_with_signals({"强势反包阳": False, "缺口类型": None, "低位地量": True})),
    ]
    assert len(scan_signals.filter_by_signal(res, "bull_engulf")) == 1
    assert len(scan_signals.filter_by_signal(res, "low_ground_volume")) == 1
    assert len(scan_signals.filter_by_signal(res, "gap_continuation")) == 1
    assert len(scan_signals.filter_by_signal(res, "gap_breakout")) == 0


# ---------------- scorer 轻加权 ----------------

def _bd(sig: dict) -> MysteryBreakdown:
    bd = MysteryBreakdown()
    bd.signal = dict(sig)
    return bd


def test_combine_pattern_weight_off_by_default():
    bd = _bd({"综合评分": 50.0, "操作建议": "x", "强势反包阳": True,
              "缺口类型": "突破", "低位地量": True})
    s, _, _ = scorer.combine(bd, None, mix_enabled=False)        # pattern_weight 默认关
    assert s == 50.0


def test_combine_pattern_weight_on_bull():
    bd = _bd({"综合评分": 50.0, "操作建议": "x", "强势反包阳": True,
              "缺口类型": "突破", "低位地量": True})
    s, _, _ = scorer.combine(bd, None, mix_enabled=False, pattern_weight=True)
    # 50 + 4(反包) + 4(突破) + 4(地量) = 62
    assert s == 62.0


def test_combine_pattern_weight_exhaustion_down():
    bd = _bd({"综合评分": 50.0, "操作建议": "x", "强势反包阳": False,
              "缺口类型": "衰竭", "低位地量": False})
    s, _, _ = scorer.combine(bd, None, mix_enabled=False, pattern_weight=True)
    assert s == 44.0        # 50 - 6


def test_combine_pattern_weight_no_resurrect_veto():
    bd = _bd({"综合评分": 0.0, "操作建议": "x", "强势反包阳": True,
              "缺口类型": "突破", "低位地量": True})
    s, _, _ = scorer.combine(bd, None, mix_enabled=False, pattern_weight=True)
    assert s == 0.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
