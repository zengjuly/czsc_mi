"""集成测试：阶段识别(stage) -> 扫描 payload(mystery) -> scan_signals.classify 消费链路。

锁定 P1-1 走查结论：analyze_stage 的结果必须真正进入 d['mystery']['stage']，
并被 classify 解析为 stage / breakout_quality / breakout_confirm / false_breakout，
供 Web「阶段识别+突破质量」展示与「回踩确认突破/假突破」信号过滤使用。
纯内存，不触发任何行情 IO。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mystery.core.models import MysteryBreakdown, AnalysisResult
from mystery.core.scan_signals import classify
from mystery.core import stage as S


def _build(stage_dict, signal=None, platform=None):
    mb = MysteryBreakdown()
    mb.stage = stage_dict
    if signal is not None:
        mb.signal = signal
    if platform is not None:
        mb.platform = platform
    return AnalysisResult(symbol="sh600519", name="测试",
                          trade_date="2026-08-28", mystery=mb)


def test_stage_flows_into_payload_and_classify():
    stage = {"stage": "拉升", "stage_confidence": 0.75,
             "breakout": {"has_breakout": True, "quality": "回踩确认"}}
    d = _build(stage).to_dict()
    assert d["mystery"]["stage"]["stage"] == "拉升"
    out = classify(d)
    assert out["stage"] == "拉升"
    assert out["breakout_quality"] == "回踩确认"
    assert out["has_breakout"] is True
    assert out["breakout_confirm"] is True
    assert out["false_breakout"] is False
    assert "回踩确认突破" in out["labels"]
    assert "拉升阶段" in out["labels"]


def test_false_breakout_flows():
    stage = {"stage": "派发", "breakout": {"has_breakout": True, "quality": "假突破"}}
    d = _build(stage).to_dict()
    out = classify(d)
    assert out["stage"] == "派发"
    assert out["breakout_quality"] == "假突破"
    assert out["false_breakout"] is True
    assert out["breakout_confirm"] is False
    assert "假突破回避" in out["labels"]


def test_unknown_stage_is_safe():
    d = _build({}).to_dict()
    out = classify(d)
    assert out["stage"] == "未知"
    assert out["breakout_quality"] == "无"
    assert out["breakout_confirm"] is False
    assert out["false_breakout"] is False


def test_real_analyze_stage_through_contract():
    n = 120
    close = np.linspace(10.0, 20.0, n)
    vol = np.linspace(1e6, 3e6, n)
    df = pd.DataFrame({
        "收盘价": close, "开盘价": close * 0.99,
        "最高价": close * 1.02, "最低价": close * 0.98,
        "成交量": vol,
    })
    stg = S.analyze_stage(df, stock_code="sh600519")
    assert isinstance(stg, dict) and "stage" in stg
    assert stg["stage"] in ("吸筹", "洗盘", "拉升", "派发", "震荡", "未知")
    d = _build(stg).to_dict()
    out = classify(d)
    assert out["stage"] == stg["stage"]
