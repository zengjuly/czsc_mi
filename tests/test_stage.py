"""mystery.core.stage — 阶段识别（吸筹/洗盘/拉升/派发）+ 突破质量回归。

- classify_stage 用合成 OHLCV + 直传 poc_series（控制筹码峰位移）做确定性断言。
- _eval_breakout 用构造的 VAP-ATR 序列断言 三类突破质量。
- analyze_stage 端到端（合成日K，走真实 VAP 计算）冒烟。
- scan_signals.classify 从 mystery.stage 抽取 stage / breakout 标签。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mystery.core import stage as S
from mystery.core.scan_signals import classify


def _df(close, vol) -> pd.DataFrame:
    close = np.asarray(close, float)
    return pd.DataFrame({
        "收盘价": close,
        "开盘价": close,
        "最高价": close * 1.01,
        "最低价": close * 0.99,
        "成交量": np.asarray(vol, float),
    })


def _ramp(n, a, b):
    return list(np.linspace(a, b, n))


# ---------------- 阶段识别 ----------------

def test_stage_rally():
    close = _ramp(60, 10.0, 14.0)
    vol = _ramp(60, 1000, 1400)
    poc = _ramp(60, 10.0, 12.0)          # 筹码峰上移
    r = S.classify_stage(_df(close, vol), poc_series=poc)
    assert r["stage"] == "拉升", r
    assert r["metrics"]["poc_slope_20"] is not None


def test_stage_accumulation():
    close = [10.0] * 60                  # 低位横盘
    vol = _ramp(60, 1000, 2000)          # 温和放量（末端显著高于前期）
    poc = _ramp(60, 10.0, 10.2)          # 筹码沉淀
    r = S.classify_stage(_df(close, vol), poc_series=poc)
    assert r["stage"] == "吸筹", r


def test_stage_shake():
    # 中期上行 + 末端缩量回调 + 筹码峰稳定
    close = np.empty(60)
    close[:40] = np.linspace(10.0, 10.5, 40)   # close[39]=10.5
    close[40:55] = np.linspace(10.5, 12.4, 15)  # close[54]=12.4
    close[55:] = np.linspace(12.4, 11.9, 5)     # close[59]=11.9
    vol = np.concatenate([np.linspace(1000, 1300, 55), np.full(5, 700.0)])
    poc = _ramp(60, 10.3, 11.0)                # 筹码峰基本稳定
    r = S.classify_stage(_df(close, vol), poc_series=poc)
    assert r["stage"] == "洗盘", r


def test_stage_distribution():
    # 高位滞涨放量 + 筹码峰随放量上移
    close = np.concatenate([np.linspace(10.0, 12.5, 40), [12.5] * 20])
    vol = _ramp(60, 1000, 2000)                # 放量
    poc = _ramp(60, 10.0, 12.5)                # 筹码峰上移
    r = S.classify_stage(_df(close, vol), poc_series=poc)
    assert r["stage"] == "派发", r


def test_stage_consolidation():
    close = [10.0] * 60
    vol = [1000.0] * 60
    poc = [10.0] * 60
    r = S.classify_stage(_df(close, vol), poc_series=poc)
    assert r["stage"] in ("震荡", "吸筹"), r


# ---------------- 突破质量 ----------------

def test_breakout_direct():
    vap = pd.DataFrame({
        "close": list(range(10, 20)) + [25],
        "platform_upper": [19] * 10 + [20],
        "is_breakout": [False] * 10 + [True],
    })
    bq = S._eval_breakout(vap)
    assert bq["quality"] == "直接突破", bq
    assert bq["has_breakout"] is True


def test_breakout_pullback_confirm():
    # 突破后回踩上轨不破并企稳
    vap = pd.DataFrame({
        "close": [10, 11, 12, 13, 14, 20, 16, 15.5, 15.2, 16, 17],
        "platform_upper": [15] * 11,
        "is_breakout": [False, False, False, False, True] + [False] * 6,
    })
    bq = S._eval_breakout(vap)
    assert bq["quality"] == "回踩确认", bq


def test_breakout_false():
    # 突破后回落跌破上轨支撑
    vap = pd.DataFrame({
        "close": [10, 11, 12, 13, 14, 20, 16, 14, 13, 12],
        "platform_upper": [15] * 10,
        "is_breakout": [False, False, False, False, True] + [False] * 5,
    })
    bq = S._eval_breakout(vap)
    assert bq["quality"] == "假突破", bq


def test_breakout_none():
    vap = pd.DataFrame({
        "close": list(range(10, 21)),
        "platform_upper": [25] * 11,
        "is_breakout": [False] * 11,
    })
    bq = S._eval_breakout(vap)
    assert bq["quality"] == "无", bq
    assert bq["has_breakout"] is False


# ---------------- 端到端 + classify 接线 ----------------

def test_analyze_stage_end_to_end():
    # 合成上升趋势日K，走真实 VAP 计算不应抛异常
    close = np.linspace(10.0, 13.0, 90) + np.random.default_rng(7).normal(0, 0.02, 90)
    close = np.maximum(close, 1.0)
    df = _df(close, _ramp(90, 1000, 1400))
    r = S.analyze_stage(df, stock_code="sh.600519")
    assert "stage" in r and "breakout" in r
    assert r["stage"] in ("吸筹", "洗盘", "拉升", "派发", "震荡", "未知")
    assert r["breakout"]["quality"] in ("无", "直接突破", "回踩确认", "假突破")


def test_classify_reads_stage():
    d = {
        "true_resonance": False,
        "mystery": {
            "stage": {
                "stage": "拉升",
                "stage_confidence": 0.75,
                "metrics": {},
                "detail": [],
                "breakout": {"has_breakout": True, "quality": "回踩确认",
                             "detail": [], "support": 20.0, "breakout_idx": 5},
            }
        },
    }
    out = classify(d)
    assert out["stage"] == "拉升"
    assert out["breakout_quality"] == "回踩确认"
    assert out["breakout_confirm"] is True
    assert out["has_breakout"] is True
    assert "拉升阶段" in out["labels"]
    assert "回踩确认突破" in out["labels"]


def test_classify_stage_false_breakout_label():
    d = {
        "true_resonance": False,
        "mystery": {"stage": {"stage": "派发", "breakout": {
            "has_breakout": True, "quality": "假突破", "detail": [],
            "support": None, "breakout_idx": 3}}},
    }
    out = classify(d)
    assert out["stage"] == "派发"
    assert out["false_breakout"] is True
    assert "假突破回避" in out["labels"]
