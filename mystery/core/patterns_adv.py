"""mystery.core.patterns_adv — 高信噪比形态检测（G4 提升：展示 → 信号/轻加权）。

对齐 Mistery 理论（本地帖文已核对）：

1) 强势反包阳（见底判断两法之一，#见底 #反包阳 #止跌）
   规则（见底/止跌语境 + 放量确认）：
   - 昨日为阴线（收<开）；今日为阳线（收>开）
   - 今日开 ≤ 昨收（低开反包）且 今日收 > 昨开（吞没昨阴实体）
   - 放量确认：今量 > 昨量 * VOL_RATIO（默认 1.5）
   - 强势位置：处于近期低位（回撤≥低位门闩）或 站上 5 日线
   → 满足「吞没+放量+强势位置」记为 强势反包阳（信号=真）；仅基础吞没记为弱（不进信号）

2) 缺口三类（缺口理论：突破缺口买 / 持续缺口拿 / 衰竭缺口卖）
   定义（缺口理论原文）：今天开盘价直接跳过昨天价格区间 → 今日开 > 昨高（向上跳空）
   - 突破缺口：长期横盘（缺口前区间窄幅）后向上跳空
   - 持续缺口：上涨途中（站上 MA20、非极端拉升）再次向上跳空
   - 衰竭缺口：连续大涨（缺口前N日涨幅大）后再次大幅向上跳空
   附带：三日不补确认（缺口后 low 始终高于缺口位 ≥3 日）→ 突破缺口买点更强

3) 低位地量（资金结构 + 机构建仓前兆：长期缩量横盘 + 地量）
   - 低位：现价相对近120日最高回撤 ≥ 低位门闩（与 chip_low 同口径）
   - 量能压缩：近20日量能为区间地量（≤20日最低）且明显低于自身60日均量，
     且近期量能较长期缩量（MA20 < MA60）→ 长期缩量横盘
   → 低位 AND 量能压缩 = 低位地量（高信噪比底部信号）

纯函数、零 IO、不 import czsc / 数据客户端；直接吃 List[Bar]，无需 enrich_indicators。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---- 可调参数（集中管理；改动须升 models.RULE_VER）----
VOL_RATIO = 1.5                 # 反包阳放量确认倍数（今量 > 昨量*1.5）
LOW_RETRACE = 0.15             # 低位门闩：回撤 ≥15% 视为低位（同 scan_signals.CHIP_LOW_RETRACE）
CONSOLIDATION_RANGE = 0.12     # 横盘判定：缺口前区间振幅(高-低)/低 < 12%
RALLY_PCT = 0.35               # 连续大涨判定：缺口前20日涨幅 ≥35%
EXHAUST_GAP_PCT = 3.0          # 衰竭缺口：跳空幅度 ≥3%
GAP_MIN_PCT = 1.0              # 缺口最小幅度闸门：跳空幅度 <1% 视为噪声不计入（W41 修复过灵敏）
FILL_CONFIRM_DAYS = 3          # 三日不补确认

_GAP_EPS = 1e-6


def _bars(bars) -> List[Any]:
    """兼容 BarSeries 或 List[Bar]；返回 Bar 列表。"""
    if bars is None:
        return []
    if hasattr(bars, "bars"):
        return list(bars.bars)
    return list(bars)


def _ma_n(closes: List[float], n: int, idx: int) -> Optional[float]:
    if idx < n - 1 or len(closes) < n:
        return None
    return sum(closes[idx - n + 1: idx + 1]) / n


def detect_bullish_engulfing(bars) -> Dict[str, Any]:
    """强势反包阳检测。

    :return: {'hit' bool(强势), 'basic' bool(基础吞没), 'detail': {...}}
    """
    bs = _bars(bars)
    empty = {"hit": False, "basic": False,
             "detail": {"原因": "数据不足（需≥2根K线）"}}
    if len(bs) < 2:
        return empty
    y, t = bs[-2], bs[-1]
    # 阴阳判定
    bearish_y = y.close < y.open
    bullish_t = t.close > t.open
    if not (bearish_y and bullish_t):
        return {"hit": False, "basic": False,
                "detail": {"原因": "非「昨阴+今阳」组合"}}
    # 吞没：今开≤昨收（低开反包）且 今收>昨开（吞没昨阴实体）
    engulf = (t.open <= y.close + _GAP_EPS) and (t.close > y.open)
    if not engulf:
        return {"hit": False, "basic": False,
                "detail": {"原因": "未形成吞没（今开>昨收 或 今收≤昨开）"}}
    cover_high = t.close > y.high + _GAP_EPS          # 吞没昨最高价→更强
    vol_confirm = t.volume > y.volume * VOL_RATIO     # 放量确认
    # 强势位置：近期低位 或 站上5日线
    closes = [b.close for b in bs]
    ma5 = _ma_n(closes, 5, len(closes) - 1)
    above_ma5 = (ma5 is not None) and (t.close >= ma5 - _GAP_EPS)
    hi120 = max(closes[-120:]) if len(closes) >= 2 else t.close
    price_pos = (1 - t.close / hi120) if hi120 > 0 else 0.0
    low_pos = price_pos >= LOW_RETRACE
    strong_pos = low_pos or above_ma5
    hit = bool(vol_confirm and strong_pos)
    return {
        "hit": hit,
        "basic": True,
        "detail": {
            "昨阴": f"{y.open:.2f}→{y.close:.2f}",
            "今阳": f"{t.open:.2f}→{t.close:.2f}",
            "吞没最高价": cover_high,
            "放量确认": vol_confirm,
            "回撤%": round(price_pos * 100, 1),
            "站上MA5": above_ma5,
            "近期低位": low_pos,
            "强度": "强势" if hit else "弱（缺放量或强势位置）",
        },
    }


def detect_gap(daily_bars) -> Dict[str, Any]:
    """向上跳空缺口三类识别（突破/持续/衰竭）。

    :return: {'hit' bool, 'gap_up' bool, 'gap_type' str|None,
              'gap_magnitude_pct' float, 'unfilled_days' int, 'detail': {...}}
    """
    bs = _bars(daily_bars)
    null = {"hit": False, "gap_up": False, "gap_type": None,
            "gap_magnitude_pct": 0.0, "unfilled_days": 0,
            "detail": {"原因": "数据不足（需≥3根K线）"}}
    if len(bs) < 3:
        return null
    # 找最近一根「今日开 > 昨高」的向上跳空
    gap_idx = None
    for i in range(len(bs) - 1, 0, -1):
        if bs[i].open > bs[i - 1].high + _GAP_EPS:
            gap_idx = i
            break
    if gap_idx is None:
        return {"hit": False, "gap_up": False, "gap_type": None,
                "gap_magnitude_pct": 0.0, "unfilled_days": 0,
                "detail": {"原因": "近段无向上跳空缺口"}}

    g = bs[gap_idx]
    prev = bs[gap_idx - 1]
    gap_level = prev.high                       # 缺口位（昨高）
    gap_mag = (g.open - prev.high) / prev.high * 100.0 if prev.high > 0 else 0.0
    # W41 修复：最小幅度闸门——仅当「最近一次」跳空幅度达阈值才计入有效缺口，
    # 不向前回溯历史大缺口（否则陈旧跳空仍会触发信号）。过滤 <1% 微小噪声跳空，
    # 把 +4/±6 权重从「非选择型」(原 100% 命中) 收敛为「有区分度」(约 30%)。
    if gap_mag < GAP_MIN_PCT:
        return {"hit": False, "gap_up": False, "gap_type": None,
                "gap_magnitude_pct": round(gap_mag, 2), "unfilled_days": 0,
                "detail": {"原因": f"最近跳空幅度 {gap_mag:.2f}% < 闸门 {GAP_MIN_PCT}%（视为噪声）",
                           "缺口位": round(gap_level, 2)}}
    closes = [b.close for b in bs]

    # —— 缺口前语境（取缺口前 40 根，最少 10 根）——
    pre = bs[max(0, gap_idx - 40): gap_idx]
    pre_closes = [b.close for b in pre]
    pre_highs = [b.high for b in pre]
    pre_lows = [b.low for b in pre]
    rng = (max(pre_highs) - min(pre_lows)) / min(pre_lows) if pre_lows and min(pre_lows) > 0 else 999.0
    consolidation = rng < CONSOLIDATION_RANGE
    # 连续大涨：缺口前20日涨幅
    rally = 0.0
    if gap_idx >= 21:
        rally = (prev.close - closes[gap_idx - 21]) / closes[gap_idx - 21] if closes[gap_idx - 21] > 0 else 0.0
    ma20_prev = _ma_n(closes, 20, gap_idx - 1)
    uptrend = (ma20_prev is not None) and (prev.close > ma20_prev)

    if consolidation:
        gap_type = "突破"
    elif rally >= RALLY_PCT and gap_mag >= EXHAUST_GAP_PCT:
        gap_type = "衰竭"
    elif uptrend:
        gap_type = "持续"
    else:
        gap_type = "持续"

    # 三日不补：缺口后 low 始终 > 缺口位
    unfilled = 0
    for j in range(gap_idx, len(bs)):
        if bs[j].low > gap_level + _GAP_EPS:
            unfilled += 1
        else:
            break
    fill_confirm = unfilled >= FILL_CONFIRM_DAYS

    return {
        "hit": True,
        "gap_up": True,
        "gap_type": gap_type,
        "gap_magnitude_pct": round(gap_mag, 2),
        "unfilled_days": unfilled,
        "detail": {
            "缺口位": round(gap_level, 2),
            "类型": gap_type,
            "跳空幅度%": round(gap_mag, 2),
            "横盘前兆": consolidation,
            "前20日涨幅%": round(rally * 100, 1),
            "站上MA20": uptrend,
            "三日不补": fill_confirm,
            "未补天数": unfilled,
        },
    }


def detect_low_ground_volume(bars) -> Dict[str, Any]:
    """低位地量：低位（回撤≥门闩）+ 量能压缩（近20日地量且低于60日均量、长期缩量）。

    :return: {'hit' bool, '低位' bool, '量能压缩' bool, 'detail': {...}}
    """
    bs = _bars(bars)
    null = {"hit": False, "低位": False, "量能压缩": False,
            "detail": {"原因": "数据不足（需≥60根K线）"}}
    if len(bs) < 60:
        return null
    closes = [b.close for b in bs]
    vols = [b.volume for b in bs]
    price = closes[-1]
    hi120 = max(closes[-120:])
    price_pos = (1 - price / hi120) if hi120 > 0 else 0.0
    low = price_pos >= LOW_RETRACE

    vol_now = vols[-1]
    vol_ma20 = sum(vols[-20:]) / 20
    vol_ma60 = sum(vols[-60:]) / 60
    vol_min20 = min(vols[-20:])
    is_ground = vol_now <= vol_min20 + _GAP_EPS          # 近20日地量
    below_ma = vol_now < 0.5 * vol_ma60 if vol_ma60 > 0 else False
    shrinking = vol_ma20 < vol_ma60                       # 近期较长期缩量
    compression = bool(is_ground and below_ma and shrinking)

    return {
        "hit": bool(low and compression),
        "低位": low,
        "量能压缩": compression,
        "detail": {
            "回撤%": round(price_pos * 100, 1),
            "近20日地量": is_ground,
            "量/60日均量": round(vol_now / vol_ma60, 2) if vol_ma60 > 0 else None,
            "MA20<MA60(长期缩量)": shrinking,
        },
    }
