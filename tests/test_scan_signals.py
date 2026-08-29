"""test_scan_signals — 三类信号分类（002.md W1-B，伪造 to_dict()，不打行情）。"""
from __future__ import annotations

from mystery.core.scan_signals import classify, filter_by_signal


def _fake(symbol="sh600519", true_resonance=False, vap_break=False,
          avg_turnover=None, platform_break=False):
    """构造 AnalysisResult.to_dict() 形状的最小 dict。"""
    vap = {"突破信号": vap_break, "自适应周期": {"avg_turnover": avg_turnover}}
    plat = {"突破信号": platform_break}
    return {
        "symbol": symbol,
        "name": "测试",
        "trade_date": "2026-08-28",
        "price": 10.0,
        "score": 60.0,
        "advice": "观望",
        "true_resonance": true_resonance,
        "mystery": {"vap_atr": vap, "platform": plat},
    }


def test_true_resonance_passthrough():
    d = _fake(true_resonance=True)
    out = classify(d)
    assert out["true_resonance"] is True
    assert "真三振" in out["labels"]


def test_vap_atr_break_from_vap_atr():
    out = classify(_fake(vap_break=True))
    assert out["vap_atr_break"] is True
    assert "VAP-ATR突破" in out["labels"]


def test_vap_atr_break_from_platform():
    """vap_atr 缺突破字段时，映射 platform 的突破信号（不重算 POC）。"""
    out = classify(_fake(vap_break=False, platform_break=True))
    assert out["vap_atr_break"] is True


def test_chip_low_below_2():
    out = classify(_fake(avg_turnover=1.2))
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False
    assert "筹码低位共振" in out["labels"]


def test_chip_not_low_above_2():
    out = classify(_fake(avg_turnover=3.5))
    assert out["chip_low"] is False
    assert out["chip_low_unknown"] is False


def test_chip_unknown_when_no_turnover():
    """to_dict() 没有均换手 → False + chip_low_unknown（禁止再拉 K 线）。"""
    out = classify(_fake(avg_turnover=None))
    assert out["chip_low"] is False
    assert out["chip_low_unknown"] is True


def test_chip_low_from_top_level_turnover_20():
    """003.md 多路径①：顶层 turnover_20 扁平字段优先。"""
    d = _fake(avg_turnover=1.2)
    d["turnover_20"] = 1.5
    out = classify(d)
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False


def test_chip_low_from_mystery_turnover_20():
    """003.md 多路径②：mystery.turnover_20。"""
    d = _fake(avg_turnover=None)
    d["mystery"]["turnover_20"] = 1.0
    out = classify(d)
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False


def test_chip_low_from_vap_atr_top_level():
    """003.md 多路径③：vap_atr 顶层 avg_turnover（自适应周期缺失时）。"""
    d = _fake(avg_turnover=None)
    d["mystery"]["vap_atr"] = {"突破信号": False, "avg_turnover": 1.8}
    out = classify(d)
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False


def test_top_level_turnover_20_takes_precedence():
    """顶层 turnover_20 存在时，即使 mystery 里有更高值也以顶层为准。"""
    d = _fake(avg_turnover=None)
    d["turnover_20"] = 3.0
    d["mystery"]["turnover_20"] = 1.0
    out = classify(d)
    assert out["chip_low"] is False
    assert out["chip_low_unknown"] is False


def test_no_signals_default():
    out = classify(_fake())
    assert out == {
        "vap_atr_break": False,
        "chip_low": False,
        "true_resonance": False,
        "chip_low_unknown": True,
        "labels": [],
    }


def test_filter_by_signal():
    rows = [
        _fake("a", true_resonance=True),
        _fake("b", vap_break=True),
        _fake("c", avg_turnover=1.0),
        _fake("d"),
    ]
    tagged = [dict(r, **classify(r)) for r in rows]
    assert [r["symbol"] for r in filter_by_signal(tagged, "true_resonance")] == ["a"]
    assert [r["symbol"] for r in filter_by_signal(tagged, "vap_atr")] == ["b"]
    assert [r["symbol"] for r in filter_by_signal(tagged, "chip_low")] == ["c"]
    assert len(filter_by_signal(tagged, "bogus")) == 4
