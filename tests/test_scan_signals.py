"""test_scan_signals — 三类信号分类（004.md 口径，伪造 to_dict()，不打行情）。

chip_low = 缩量(turnover_20<2) AND 低位(平台门 或 价格回撤门)。无 turnover_20 → unknown。
"""
from __future__ import annotations

from mystery.core.scan_signals import classify, filter_by_signal


def _fake(symbol="sh600519", true_resonance=False, vap_break=False,
          avg_turnover=None, platform_break=False, platform_status=None,
          price=None, high_120=None):
    """构造 AnalysisResult.to_dict() 形状的最小 dict。"""
    vap = {"突破信号": vap_break, "自适应周期": {"avg_turnover": avg_turnover}}
    plat = {"突破信号": platform_break}
    if platform_status is not None:
        plat["平台状态"] = platform_status
    return {
        "symbol": symbol,
        "name": "测试",
        "trade_date": "2026-08-28",
        "price": price,
        "high_120": high_120,
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


# ---------------- chip_low 新口径（004.md） ----------------
def test_chip_low_bottom_shrink():
    """底部缩量：turnover<2 且回撤≥15% → chip_low True。"""
    out = classify(_fake(avg_turnover=1.0, price=80, high_120=100))
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False
    assert out["chip_quiet"] is False
    assert "筹码低位共振" in out["labels"]


def test_chip_quiet_high_shrink():
    """蓝筹高位缩量：turnover<2 但回撤<15% 且平台未过 → quiet 而非 chip_low。"""
    out = classify(_fake(avg_turnover=1.0, price=95, high_120=100,
                         platform_status="已远离"))
    assert out["chip_low"] is False
    assert out["chip_low_unknown"] is False
    assert out["chip_quiet"] is True
    assert "筹码低位共振" not in out["labels"]


def test_chip_low_platform_only():
    """平台内缩量：无 high_120，但平台状态含「平台内」→ chip_low True。"""
    out = classify(_fake(avg_turnover=1.5, platform_status="平台内"))
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False


def test_horizontal_platform_not_low_gate():
    """「横盘整理」≠「平台内」：高位横盘缩量 → quiet 而非 chip_low（蓝筹过宽修复）。"""
    out = classify(_fake(avg_turnover=1.0, price=95, high_120=100,
                         platform_status="横盘整理"))
    assert out["chip_low"] is False
    assert out["chip_quiet"] is True


def test_horizontal_platform_with_retrace():
    """横盘整理但回撤≥15% → 回撤门过 → chip_low True。"""
    out = classify(_fake(avg_turnover=1.0, price=80, high_120=100,
                         platform_status="横盘整理"))
    assert out["chip_low"] is True
    assert out["chip_quiet"] is False


def test_chip_unknown_when_no_turnover():
    """无 turnover_20 → chip_low False + unknown True（禁止 unknown 当 True）。"""
    out = classify(_fake(avg_turnover=None))
    assert out["chip_low"] is False
    assert out["chip_low_unknown"] is True
    assert out["chip_quiet"] is False
    assert out["turnover_20"] is None
    assert out["price_pos"] is None


def test_chip_not_low_when_volume_up():
    """放量（≥2%）无论多低位都不进 chip_low。"""
    out = classify(_fake(avg_turnover=5.0, price=80, high_120=100,
                         platform_status="平台内"))
    assert out["chip_low"] is False
    assert out["chip_low_unknown"] is False
    assert out["chip_quiet"] is False


def test_price_pos_output():
    out = classify(_fake(avg_turnover=1.0, price=80, high_120=100))
    assert out["price_pos"] == 0.2


# ---------------- turnover_20 多路径（003.md） ----------------
def test_chip_low_from_top_level_turnover_20():
    d = _fake(avg_turnover=1.2, price=80, high_120=100)
    d["turnover_20"] = 1.5
    out = classify(d)
    assert out["turnover_20"] == 1.5
    assert out["chip_low"] is True


def test_chip_low_from_mystery_turnover_20():
    d = _fake(avg_turnover=None, price=80, high_120=100)
    d["mystery"]["turnover_20"] = 1.0
    out = classify(d)
    assert out["chip_low"] is True
    assert out["chip_low_unknown"] is False


def test_chip_low_from_vap_atr_top_level():
    d = _fake(avg_turnover=None, price=80, high_120=100)
    d["mystery"]["vap_atr"] = {"突破信号": False, "avg_turnover": 1.8}
    out = classify(d)
    assert out["chip_low"] is True


def test_top_level_turnover_20_takes_precedence():
    d = _fake(avg_turnover=None, price=80, high_120=100)
    d["turnover_20"] = 3.0
    d["mystery"]["turnover_20"] = 1.0
    out = classify(d)
    assert out["turnover_20"] == 3.0
    assert out["chip_low"] is False   # 顶层 ≥2% → 不进 chip_low


def test_no_signals_default():
    out = classify(_fake())
    assert out == {
        "vap_atr_break": False,
        "chip_low": False,
        "chip_low_unknown": True,
        "chip_quiet": False,
        "true_resonance": False,
        "turnover_20": None,
        "price_pos": None,
        "labels": [],
    }


def test_filter_by_signal():
    rows = [
        _fake("a", true_resonance=True),
        _fake("b", vap_break=True),
        _fake("c", avg_turnover=1.0, price=80, high_120=100),
        _fake("d"),
    ]
    tagged = [dict(r, **classify(r)) for r in rows]
    assert [r["symbol"] for r in filter_by_signal(tagged, "true_resonance")] == ["a"]
    assert [r["symbol"] for r in filter_by_signal(tagged, "vap_atr")] == ["b"]
    assert [r["symbol"] for r in filter_by_signal(tagged, "chip_low")] == ["c"]
    # unknown 不计入 chip_low 命中
    assert "d" not in [r["symbol"] for r in filter_by_signal(tagged, "chip_low")]
    assert len(filter_by_signal(tagged, "bogus")) == 4
