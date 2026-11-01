"""mystery.core.multi_period — 多周期方向因子（G1 修复：补回时间周期维度）。

设计对齐 Mistery 理论「顺大势逆小势 / 三周期选股」：

- 月线方向（MA 多头排列 + 斜率）作为「大周期权重基线」：
    月线向上 → 基线 > 1（放大信号），月线向下 → 基线 < 1（收缩信号）。
- 周/日 与月线（及彼此）的同向度作为「共振加分」：
    每对同向 + MPD_BONUS_PAIR。
- 小周期逆大周期 → 「逆势降权」：
    每对反向 - MPD_PENALTY_PAIR。

接入点：
- pipeline.run_mystery 计算并挂入 bd.signal['多周期方向因子']（展示）。
- scorer.combine 在生成最终综合分时应用：
      final = clamp(base × 基线权重 + 同向共振加分 − 逆势降权, 0, 100)

权重常量集中于此；改动须升 models.RULE_VER（见 RULE_VER 注释）。
纯函数、零 IO、不 import czsc / 数据客户端。自行从收盘价计算 MA，
因此入参 DataFrame 可仅含中文 K 线列（无需 enrich_indicators 先行）。
"""
from __future__ import annotations

import math

import pandas as pd

# ---- 可调权重（集中管理；改动须升 RULE_VER）----
MPD_BONUS_PAIR = 4.0      # 每对同向（周/日 vs 月，周 vs 日）加分
MPD_PENALTY_PAIR = 9.0    # 每对小周期逆大周期降权

# 月线方向(排列) × 斜率符号 → 基线权重
MPD_BASELINE = {
    ("bull", "up"): 1.15,       # 月线多头 + 斜率向上：强放大
    ("bull", "flat"): 1.05,     # 月线多头 + 斜率走平
    ("bull", "down"): 1.00,     # 月线多头 + 斜率向下（警惕）
    ("neutral", "up"): 1.05,    # 月线震荡 + 斜率向上
    ("neutral", "flat"): 1.00,  # 月线震荡：中性基线
    ("neutral", "down"): 0.95,  # 月线震荡 + 斜率向下
    ("bear", "up"): 0.95,       # 月线空头 + 斜率向上（弱反弹）
    ("bear", "flat"): 0.85,     # 月线空头 + 斜率走平
    ("bear", "down"): 0.70,     # 月线空头 + 斜率向下：强收缩
}
MPD_NEUTRAL_BASELINE = 1.0   # 月线数据不足时的中性基线（不放大不收缩）

# 斜率判定阈值（%，绝对值超过即视为有方向）
MPD_SLOPE_THRESHOLD = 1.0

# 各周期斜率回看窗口（单位：K 线根数；月线看 6 月、周线看 8 周、日线看 20 日）
_MPD_SLOPE_WIN = {"1M": 6, "1w": 8, "1d": 20}


def _close_series(df) -> "Optional[pd.Series]":
    """从归一 DataFrame 取收盘价序列（兼容 '收盘价' / 'close'）。"""
    if df is None or not hasattr(df, "columns") or getattr(df, "empty", True):
        return None
    for col in ("收盘价", "close"):
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s) >= 20:
                return s
    return None


def period_direction(close: "Optional[pd.Series]", freq: str) -> dict:
    """单周期方向：MA 多头排列 + 斜率（%）。

    :return: {'trend': 多头/空头/震荡/数据不足, 'dir': +1/-1/0,
              'slope_pct': float, 'ma_bull': bool}
    """
    empty = {"trend": "数据不足", "dir": 0, "slope_pct": 0.0, "ma_bull": False}
    if close is None or len(close) < 20:
        return empty
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    m5, m10, m20 = float(ma5.iloc[-1]), float(ma10.iloc[-1]), float(ma20.iloc[-1])
    c = float(close.iloc[-1])
    if math.isnan(m20):
        return empty
    bull = (m5 > m10 > m20) and (c > m20)
    bear = (m5 < m10 < m20)
    trend = "多头" if bull else ("空头" if bear else "震荡")
    direction = 1 if bull else (-1 if bear else 0)

    win = _MPD_SLOPE_WIN.get(freq, 6)
    slope_pct = 0.0
    if len(ma20) > win and not math.isnan(ma20.iloc[-win - 1]) and ma20.iloc[-win - 1] != 0:
        slope_pct = (m20 - float(ma20.iloc[-win - 1])) / float(ma20.iloc[-win - 1]) * 100.0
    return {"trend": trend, "dir": direction, "slope_pct": round(slope_pct, 2),
            "ma_bull": bool(bull)}


def _trend_class(trend: str) -> str:
    return {"多头": "bull", "空头": "bear", "震荡": "neutral"}.get(trend, "neutral")


def compute_multi_period_factor(daily_df, weekly_df=None, monthly_df=None) -> dict:
    """计算多周期方向因子。

    :param daily_df/weekly_df/monthly_df: 归一 DataFrame（中文列，可仅含 K 线）。
    :return: 因子明细 dict（JSON 可序列化），直接挂入 bd.signal['多周期方向因子']。
    """
    cm = _close_series(monthly_df)
    cw = _close_series(weekly_df)
    cd = _close_series(daily_df)
    m = period_direction(cm, "1M") if cm is not None else None
    w = period_direction(cw, "1w") if cw is not None else None
    d = period_direction(cd, "1d") if cd is not None else None

    notes: list = []

    # —— 大周期权重基线（月线）——
    if m is not None and m["trend"] != "数据不足":
        sign = ("up" if m["slope_pct"] > MPD_SLOPE_THRESHOLD
                else ("down" if m["slope_pct"] < -MPD_SLOPE_THRESHOLD else "flat"))
        baseline = float(MPD_BASELINE[(_trend_class(m["trend"]), sign)])
        notes.append(f"月线{m['trend']}(斜率{m['slope_pct']:+.1f}%)→基线权重{baseline}")
    else:
        baseline = MPD_NEUTRAL_BASELINE
        notes.append("月线数据不足→基线权重1.0(中性)")

    # —— 同向共振加分 / 逆势降权 ——
    bonus = 0.0
    penalty = 0.0
    pairs = []
    if w is not None and m is not None:
        pairs.append(("周线", "月线", w["dir"], m["dir"]))
    if d is not None and m is not None:
        pairs.append(("日线", "月线", d["dir"], m["dir"]))
    if w is not None and d is not None:
        pairs.append(("周线", "日线", w["dir"], d["dir"]))
    for name_a, name_b, da, db in pairs:
        if da == 0 or db == 0:
            continue  # 任一为震荡(0)→不参与同向/逆势判定
        if da == db:
            bonus += MPD_BONUS_PAIR
            notes.append(f"{name_a}与{name_b}同向(+{MPD_BONUS_PAIR:.0f})")
        else:
            penalty += MPD_PENALTY_PAIR
            notes.append(f"{name_a}逆{name_b}大周期(-{MPD_PENALTY_PAIR:.0f})")

    return {
        "月线方向": m["trend"] if m is not None else "数据不足",
        "周线方向": w["trend"] if w is not None else "数据不足",
        "日线方向": d["trend"] if d is not None else "数据不足",
        "月线斜率%": m["slope_pct"] if m is not None else 0.0,
        "周线斜率%": w["slope_pct"] if w is not None else 0.0,
        "日线斜率%": d["slope_pct"] if d is not None else 0.0,
        "基线权重": round(baseline, 3),
        "同向共振加分": round(bonus, 1),
        "逆势降权": round(penalty, 1),
        "因子说明": notes,
        "数据完整": all(x is not None for x in (m, w, d)),
    }


def apply_factor(base_score: float, factor: dict) -> float:
    """将多周期方向因子应用到基准分。

    base<=0（如年线滤网一票否决）或因子缺失时原样返回，避免复活否决股。
    """
    if not factor or base_score <= 0:
        return base_score
    baseline = float(factor.get("基线权重", 1.0))
    bonus = float(factor.get("同向共振加分", 0.0))
    penalty = float(factor.get("逆势降权", 0.0))
    mod = base_score * baseline + bonus - penalty
    return min(100.0, max(0.0, round(mod, 1)))
