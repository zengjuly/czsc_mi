"""mystery.core.stage — 主升阶段识别与平台/VAP-ATR 突破质量。

阶段识别（吸筹—洗盘—拉升—派发）：量价趋势（价趋/量趋/振幅）+ 筹码峰位移
（复用 VAP-ATR 的 POC 序列——即成交量加权的成本密集峰）的相对位移。

突破质量（理论强调"回踩不破再进"）：基于 VAP-ATR 全序列的 is_breakout 与
上轨支撑，把突破分为 回踩确认（突破后回踩上轨不破企稳）/ 假突破（回落跌破
上轨支撑）/ 直接突破（刚突破或站上未回踩）。

纯函数，零 IO。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .platform import (calculate_adaptive_lookback,
                       calculate_adaptive_vap_atr)

_STAGE_UNKNOWN = "未知"
_STAGE_SHAKE = "洗盘"
_STAGE_ACCUM = "吸筹"
_STAGE_RALLY = "拉升"
_STAGE_DISTRIB = "派发"
_STAGE_CONSOL = "震荡"

# 突破质量取值
_BO_NONE = "无"
_BO_DIRECT = "直接突破"
_BO_PULLBACK = "回踩确认"
_BO_FALSE = "假突破"

# 回踩/假突破阈值（相对突破日上轨）
_PULLBACK_SUPPORT_RATIO = 0.985   # 收盘价跌破 上轨*该比例 → 假突破
_PULLBACK_NEAR_RATIO = 1.02       # 期间最低价 <= 上轨*该比例 → 出现过回踩动作
_BREAKOUT_LOOKBACK = 60           # 仅看近 60 根内的突破


def _r(x: Any, nd: int = 2) -> Optional[float]:
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _pct(a: Any, b: Any) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        if pd.isna(a) or pd.isna(b) or b == 0:
            return None
        return float((a - b) / b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def compute_vap_series(data: pd.DataFrame, stock_code: str = "",
                      n: Optional[int] = None, atr_m: Optional[int] = None,
                      k: Optional[float] = None) -> Optional[pd.DataFrame]:
    """返回 VAP-ATR 全序列（含 close / platform_upper / poc / is_breakout）。

    取近 120 根以降低开销并聚焦近期；数据不足 30 根返回 None。
    """
    if data is None or len(data) < 30:
        return None
    work = data.tail(120) if len(data) > 120 else data
    try:
        digits = "".join(ch for ch in str(stock_code) if ch.isdigit())[:6]
        market_type = ("ChiNext_STAR" if digits.startswith(("300", "301", "688"))
                       else "MainBoard")
        info = calculate_adaptive_lookback(work)
        if n is None:
            n = info.get("adaptive_n") or 30
        if atr_m is None:
            atr_m = int(np.clip(round(n / 4), 10, 14))
        if k is None:
            at = info.get("avg_turnover")
            k = 2.2 if (at and at >= 10) else (1.8 if (at and at >= 3) else 1.5)
        df = calculate_adaptive_vap_atr(work, n=n, atr_m=atr_m, k=k,
                                       market_type=market_type,
                                       latest_only=False)
        close_col = "收盘价" if "收盘价" in df.columns else "close"
        out = pd.DataFrame({
            "close": pd.to_numeric(df[close_col], errors="coerce").values,
            "platform_upper": (pd.to_numeric(df["platform_upper"], errors="coerce").values
                               if "platform_upper" in df else np.nan),
            "poc": (pd.to_numeric(df["poc"], errors="coerce").values
                    if "poc" in df else np.nan),
            "is_breakout": (df["is_breakout"].fillna(False).values
                            if "is_breakout" in df else False),
        })
        return out
    except Exception:
        return None


def classify_stage(data: pd.DataFrame, stock_code: str = "",
                   *, poc_series: Optional[List[float]] = None,
                   n: Optional[int] = None) -> Dict[str, Any]:
    """阶段识别：吸筹 / 洗盘 / 拉升 / 派发 / 震荡 / 未知。

    量价（价趋/量趋/振幅/高低位）+ 筹码峰位移（POC 相对位移）。
    测试可直传 poc_series 跳过 VAP 计算。
    """
    if data is None or len(data) < 30:
        return {"stage": _STAGE_UNKNOWN, "stage_confidence": 0.0,
                "metrics": {}, "detail": ["数据不足30日"]}
    close_col = "收盘价" if "收盘价" in data.columns else "close"
    vol_col = "成交量" if "成交量" in data.columns else "volume"
    closes = pd.to_numeric(data[close_col], errors="coerce")
    vols = (pd.to_numeric(data[vol_col], errors="coerce")
            if vol_col in data.columns else None)
    nb = len(closes)
    price_now = closes.iloc[-1]

    t20 = _pct(closes.iloc[-1], closes.iloc[-21]) if nb >= 21 else None
    t5 = _pct(closes.iloc[-1], closes.iloc[-6]) if nb >= 6 else None
    t60 = _pct(closes.iloc[-1], closes.iloc[-61]) if nb >= 61 else None

    hi120 = closes.tail(120).max()
    lo120 = closes.tail(120).min()
    near_top = (pd.notna(hi120) and price_now is not None
                and price_now >= 0.9 * hi120)
    near_bottom = (pd.notna(lo120) and price_now is not None
                   and price_now <= 1.1 * lo120)

    vol_trend = None
    if vols is not None and nb >= 25:
        v_recent = vols.tail(5).mean()
        v_prev = vols.iloc[-25:-5].mean()
        vol_trend = _r(v_recent / v_prev, 3) if (pd.notna(v_prev) and v_prev > 0) else None

    amp20 = None
    if nb >= 20:
        hh = (data["最高价"].tail(20).max() if "最高价" in data.columns
              else closes.tail(20).max())
        ll = (data["最低价"].tail(20).min() if "最低价" in data.columns
              else closes.tail(20).min())
        avg = closes.tail(20).mean()
        if pd.notna(avg) and avg > 0:
            amp20 = _r((hh - ll) / avg, 4)

    if poc_series is not None:
        poc = pd.Series(poc_series, dtype="float64")
    else:
        vs = compute_vap_series(data, stock_code=stock_code, n=n)
        poc = vs["poc"] if vs is not None else None

    poc20 = None
    poc60 = None
    if poc is not None and len(poc) >= 1 and pd.notna(poc.iloc[-1]):
        if len(poc) >= 21 and pd.notna(poc.iloc[-21]):
            poc20 = _pct(poc.iloc[-1], poc.iloc[-21])
        if len(poc) >= 61 and pd.notna(poc.iloc[-61]):
            poc60 = _pct(poc.iloc[-1], poc.iloc[-61])

    metrics = {
        "price_trend_20": _r(t20, 4),
        "price_trend_5": _r(t5, 4),
        "price_trend_60": _r(t60, 4),
        "vol_trend": vol_trend,
        "amplitude_20": amp20,
        "poc_now": _r(poc.iloc[-1]) if (poc is not None and len(poc) >= 1
                                         and pd.notna(poc.iloc[-1])) else None,
        "poc_slope_20": _r(poc20, 4),
        "poc_slope_60": _r(poc60, 4),
        "near_top": bool(near_top),
        "near_bottom": bool(near_bottom),
    }

    stage, detail, conf = _decide_stage(
        t20, t5, t60, vol_trend, amp20, poc20, poc60, near_top, near_bottom)
    return {"stage": stage, "stage_confidence": conf,
            "metrics": metrics, "detail": detail}


def _decide_stage(t20, t5, t60, vol_trend, amp20, poc20, poc60,
                  near_top, near_bottom):
    up = (t20 is not None and t20 > 0.08)
    down = (t20 is not None and t20 < -0.05)
    flat = (t20 is not None and abs(t20) <= 0.05)
    vol_up = (vol_trend is not None and vol_trend > 1.10)
    vol_down = (vol_trend is not None and vol_trend < 0.90)

    # 1) 派发：高位（非低位）放量滞涨 / 价跌量增
    if near_top and (not near_bottom) and vol_up and (t20 is not None and t20 < 0.05):
        return (_STAGE_DISTRIB,
                ["高位放量但价格滞涨：筹码峰随放量上移而价未跟，疑似派发"], 0.70)
    if t20 is not None and t20 < -0.03 and vol_up:
        return (_STAGE_DISTRIB, ["价跌量增：高位出货特征"], 0.70)

    # 2) 洗盘：中期向上 + 近期缩量回调 + 筹码峰不塌
    if (t20 is not None and t20 > 0.03) and (t5 is not None and t5 < -0.01) \
            and vol_down and (poc20 is None or poc20 > -0.02):
        return (_STAGE_SHAKE,
                ["上升趋势中缩量回调：清洗浮筹，筹码峰稳定"], 0.70)

    # 3) 拉升：明确向上 + 量能配合/中性 + 筹码成本中心上移
    if up and (vol_trend is None or vol_trend >= 0.9) \
            and (poc20 is None or poc20 > 0.02):
        return (_STAGE_RALLY, ["价升量随、筹码成本中心上移：主升段"], 0.75)

    # 4) 吸筹：低位/低波动横盘 + 温和放量
    if flat and vol_up and (near_bottom or (poc20 is not None and poc20 > 0)) \
            and (amp20 is None or amp20 < 0.20):
        return (_STAGE_ACCUM,
                ["低位/低波动横盘且温和放量：筹码沉淀吸筹"], 0.70)

    # 5) 震荡（窄幅）
    if flat and (amp20 is not None and amp20 < 0.15):
        return (_STAGE_CONSOL, ["窄幅横盘"], 0.60)
    return (_STAGE_CONSOL, ["无明显阶段特征"], 0.50)


def _eval_breakout(vap_df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """基于 VAP-ATR 全序列判定突破质量。"""
    if vap_df is None or len(vap_df) < 2:
        return {"has_breakout": False, "quality": _BO_NONE,
                "detail": ["无突破信号"], "support": None,
                "breakout_idx": None}
    bo = np.asarray(vap_df["is_breakout"].fillna(False).values, dtype=bool)
    lo = max(0, len(bo) - _BREAKOUT_LOOKBACK)
    idxs = [i for i in range(lo, len(bo)) if bo[i]]
    if not idxs:
        return {"has_breakout": False, "quality": _BO_NONE,
                "detail": ["近期无 VAP-ATR 突破"], "support": None,
                "breakout_idx": None}
    bi = idxs[-1]
    support = vap_df["platform_upper"].iloc[bi]
    current = vap_df["close"].iloc[-1]
    post = vap_df.iloc[bi + 1:]
    if len(post) == 0:
        return {"has_breakout": True, "quality": _BO_DIRECT,
                "detail": ["刚突破上轨，尚未回踩确认"], "support": _r(support),
                "breakout_idx": int(bi)}
    min_close_post = post["close"].min()
    false_break = (pd.notna(current) and pd.notna(support)
                   and current < support * _PULLBACK_SUPPORT_RATIO) or \
                  (pd.notna(min_close_post) and pd.notna(support)
                   and min_close_post < support * _PULLBACK_SUPPORT_RATIO)
    if false_break:
        return {"has_breakout": True, "quality": _BO_FALSE,
                "detail": ["突破后回落跌破上轨支撑，疑似假突破"],
                "support": _r(support), "breakout_idx": int(bi)}
    pullback = (pd.notna(min_close_post) and pd.notna(support)
                and min_close_post <= support * _PULLBACK_NEAR_RATIO)
    held = (pd.notna(current) and pd.notna(support)
            and current >= support * _PULLBACK_SUPPORT_RATIO)
    if pullback and held:
        return {"has_breakout": True, "quality": _BO_PULLBACK,
                "detail": ["突破后回踩上轨不破并企稳，回踩确认可进"],
                "support": _r(support), "breakout_idx": int(bi)}
    return {"has_breakout": True, "quality": _BO_DIRECT,
            "detail": ["突破后维持上轨上方，待回踩确认"], "support": _r(support),
            "breakout_idx": int(bi)}


def breakout_quality(data: pd.DataFrame, stock_code: str = "",
                    n: Optional[int] = None) -> Dict[str, Any]:
    """便捷封装：吃日 K 算 VAP 序列后判定突破质量。"""
    return _eval_breakout(compute_vap_series(data, stock_code=stock_code, n=n))


def analyze_stage(data: pd.DataFrame, stock_code: str = "",
                 n: Optional[int] = None) -> Dict[str, Any]:
    """阶段识别 + 突破质量 合并输出（单次 VAP 计算）。"""
    vs = compute_vap_series(data, stock_code=stock_code, n=n)
    poc_list = (vs["poc"].tolist() if vs is not None else None)
    st = classify_stage(data, stock_code=stock_code, n=n, poc_series=poc_list)
    bq = _eval_breakout(vs)
    return {**st, "breakout": bq}
