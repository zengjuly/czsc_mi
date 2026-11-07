"""mystery.core.wave3 — 三浪主升策略扫描（纯规则、纯数据、零 IO）。

定位（与 AGENTS.md 分层的关系）
--------------------------------
本模块是**独立的交易过滤与风控计算**，不是第二套计分：

- 不产出、不修改 ``AnalysisResult.score``，不复用 Mystery 1.22.30 计分规则，
  不读取数据库、不联网、``import czsc`` 一律禁止；
- 输入只有归一 K 线记录（``BarSeries.to_dict()['bars']`` 或等价 dict 列表），
  输出是纯 dict（JSON 可序列化），供 CLI / 报表 / Web 展示层消费；
- 唯一计分入口 ``analyze_one_stock()`` 保持不变。本策略与它**并行、互不覆盖**。

实现内容
--------
1. ``assess_positions``：日线 / 周线 / 月线 **位置**评估（趋势方向 + 价格区间
   分位 + 均线位置 + 斜率），用于「只做上升趋势」与「顺大势」判断。
2. ``detect_wave_structure``：第一浪上涨 / 第二浪回调 / 第三浪放量突破的
   **可回测**分段极值识别（明确参数、明确否决理由）。
3. ``iron_rules``：三浪主升九条铁律逐条判定（硬否决 / 软偏好分开）。
4. ``workflow``：最终交易流程十步状态机，给出当前推进到哪一步与下一步动作。
5. ``position_size``：仓位 = 账户总资金 × 单笔风险比例 ÷ (买入价 − 止损价)，
   含 A 股整手、无止损 / 除零 / 单票仓位上限保护。
6. ``correlation_matrix`` / ``portfolio_risk``：多标的高度相关时的**组合总风险**
   （相关性加权合成 + 风险预算缩放），而非各仓位风险的简单相加。
7. ``evaluate`` / ``evaluate_portfolio``：单票与组合层编排。

策略原文与免责声明见 ``docs/W68_三浪主升策略扫描.md``；本模块常量即为该文档的
可执行口径。**艾略特波浪划分具有主观性，以下条件只是交易过滤规则，不代表
第三浪必然出现；实际应用必须经过历史回测和模拟交易验证。**
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 免责声明 / 策略原文（展示层必须原样带出）
# ---------------------------------------------------------------------------

DISCLAIMER = (
    "注意：艾略特波浪划分具有主观性，以上条件是交易过滤规则，"
    "不代表第三浪必然出现。实际应用必须经过历史回测和模拟交易验证。"
)

IRON_RULES: Tuple[Tuple[str, str], ...] = (
    ("只做上升趋势，不做下降趋势。", "hard"),
    ("只做有明确第一浪结构的标的。", "hard"),
    ("第二浪必须出现合理的调整结构。", "hard"),
    ("第二浪调整过程中最好缩量。", "soft"),
    ("第三浪必须有明确的向上突破信号。", "hard"),
    ("突破最好伴随成交量放大。", "soft"),
    ("没有明确止损位置，坚决不买。", "hard"),
    ("错过最佳买点，不盲目追高。", "hard"),
    ("只做符合交易系统的机会，不为了交易而交易。", "hard"),
)

WORKFLOW_STEPS: Tuple[str, ...] = (
    "市场环境筛选",
    "寻找底部反转标的",
    "确认第一浪上涨",
    "等待第二浪回调",
    "观察缩量企稳",
    "等待突破确认",
    "分批买入第三浪",
    "设置保护性止损",
    "跟随趋势持有",
    "根据趋势衰竭信号分批止盈",
)

MOTTO = "第一浪看趋势，第二浪等机会，第三浪赚利润。不需要每天交易，也不需要抓住所有行情。"

# ---------------------------------------------------------------------------
# 可调参数（集中管理；调整即等于调整策略口径，须同步文档与回测）
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, float] = {
    "LOOKBACK": 120,          # 三浪结构识别回看日 K 根数
    "MIN_BARS": 60,           # 少于此根数判数据不足
    "W1_MIN_GAIN": 0.15,      # 第一浪最小涨幅（相对起点最低价）
    "W1_MIN_BARS": 5,         # 第一浪最少 K 线根数
    "W1_UP_RATIO": 0.50,      # 第一浪内收阳根数占比下限（结构明确性）
    "W2_RETRACE_MIN": 0.30,   # 第二浪最小回撤（占第一浪幅度）
    "W2_RETRACE_MAX": 0.80,   # 第二浪最大回撤（过深=破位）
    "W2_MIN_BARS": 5,         # 第二浪最少调整根数
    "W2_VOL_SHRINK": 0.85,    # 第二浪均量 ≤ 第一浪均量 × 该系数 = 缩量
    "W3_VOL_EXPAND": 1.50,    # 突破量 ≥ 第二浪均量 × 该系数 = 放量
    "BREAK_TOL": 0.0,         # 突破容差（0 = 收盘必须站上第一浪高点）
    "CHASE_MAX": 0.05,        # 相对第一浪高点追高幅度上限
    "CHASE_MAX_BARS": 5,      # 突破后超过 N 根才介入亦视为错过最佳买点
    "STOP_BUFFER": 0.02,      # 止损位 = 第二浪低点下方缓冲
    "POSITION_LOOKBACK": 60,  # 价格位置分位回看根数
    "LOT_SIZE": 100,          # A 股一手 100 股
}

HIGH_CORR = 0.70              # |ρ| ≥ 该值视为高度相关
MAX_PORTFOLIO_RISK_PCT = 0.06  # 组合总风险占权益上限，超出按 scale_down 缩放
MIN_CORR_OVERLAP = 20         # 计算相关系数所需最少重叠收益样本

_SLOPE_WIN = {"1M": 6, "1w": 8, "1d": 20}


# ---------------------------------------------------------------------------
# 基础工具（纯 Python，不依赖 pandas / numpy，便于离线构造数据测试）
# ---------------------------------------------------------------------------

def _f(v: Any) -> Optional[float]:
    """安全转 float：None / 非数值 / NaN / Inf 一律返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _mean(xs: Sequence[float]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _normalize_bars(bars: Any) -> List[Dict[str, Any]]:
    """归一 K 线为按时间升序的 dict 列表。

    兼容：dict（中/英文列）、带属性的对象（``Bar`` dataclass）、
    ``BarSeries.to_dict()['bars']``。无效 OHLC（<=0 或缺失）整根丢弃。
    """
    if bars is None:
        return []
    if isinstance(bars, dict) and "bars" in bars:
        bars = bars.get("bars")
    if not isinstance(bars, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for b in bars:
        if isinstance(b, dict):
            def _get(*keys):
                return next((b[k] for k in keys if k in b), None)
        else:
            def _get(*keys):
                return next((getattr(b, k) for k in keys
                             if hasattr(b, k)), None)
        dt = _get("dt", "date", "日期")
        o, h, l, c = _f(_get("open", "开盘价")), _f(_get("high", "最高价")), \
            _f(_get("low", "最低价")), _f(_get("close", "收盘价"))
        vol = _f(_get("volume", "成交量")) or 0.0
        if None in (o, h, l, c) or min(o, h, l, c) <= 0 or h < l:
            continue
        out.append({"dt": str(dt)[:10] if dt is not None else "",
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": max(0.0, vol)})
    return out


def _ma(closes: Sequence[float], n: int, idx: int) -> Optional[float]:
    """closes[:idx+1] 的 n 日均线；样本不足返回 None。"""
    if idx + 1 < n or n <= 0:
        return None
    return sum(closes[idx + 1 - n: idx + 1]) / n


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / abs(b) * 100.0


# ---------------------------------------------------------------------------
# 1. 日线 / 周线 / 月线 位置评估
# ---------------------------------------------------------------------------

def _period_position(bars: Any, name: str, freq: str) -> Dict[str, Any]:
    """单周期位置：趋势方向 + 价格区间分位 + 均线位置 + MA20 斜率。"""
    rows = _normalize_bars(bars)
    base: Dict[str, Any] = {
        "周期": name, "freq": freq, "根数": len(rows), "数据不足": True,
        "趋势": "数据不足", "方向": 0, "斜率%": None,
        "收盘价": None, "MA5": None, "MA10": None, "MA20": None, "MA60": None,
        "站上MA20": None, "站上MA60": None,
        "区间最高": None, "区间最低": None, "位置分位": None, "位置": "数据不足",
    }
    if len(rows) < 20:
        return base
    closes = [r["close"] for r in rows]
    c = closes[-1]
    last = len(closes) - 1
    m5, m10, m20 = _ma(closes, 5, last), _ma(closes, 10, last), _ma(closes, 20, last)
    m60 = _ma(closes, 60, last)
    if m20 is None:
        return base
    bull = bool(m5 is not None and m10 is not None and m5 > m10 > m20 and c > m20)
    bear = bool(m5 is not None and m10 is not None and m5 < m10 < m20)
    trend = "多头" if bull else ("空头" if bear else "震荡")
    direction = 1 if bull else (-1 if bear else 0)

    win = _SLOPE_WIN.get(freq, 6)
    prev = _ma(closes, 20, len(closes) - 1 - win)
    slope = _pct(m20, prev) if prev else None

    seg = rows[-min(len(rows), int(DEFAULTS["POSITION_LOOKBACK"])):]
    hi = max(r["high"] for r in seg)
    lo = min(r["low"] for r in seg)
    pos = (c - lo) / (hi - lo) if hi > lo else None
    if pos is None:
        zone = "数据不足"
    elif pos >= 0.80:
        zone = "高位"
    elif pos >= 0.30:
        zone = "中位"
    else:
        zone = "低位"

    base.update({
        "数据不足": False, "趋势": trend, "方向": direction,
        "斜率%": round(slope, 2) if slope is not None else None,
        "收盘价": round(c, 4),
        "MA5": round(m5, 4) if m5 else None,
        "MA10": round(m10, 4) if m10 else None,
        "MA20": round(m20, 4), "MA60": round(m60, 4) if m60 else None,
        "站上MA20": bool(c > m20), "站上MA60": bool(c > m60) if m60 else None,
        "区间最高": round(hi, 4), "区间最低": round(lo, 4),
        "位置分位": round(pos, 4) if pos is not None else None,
        "位置": zone,
    })
    return base


def assess_positions(daily: Any, weekly: Any = None,
                     monthly: Any = None) -> Dict[str, Any]:
    """评估日线 / 周线 / 月线位置与顺势程度。

    ``趋势允许`` = 已判定周期中无空头，且日线或周线至少其一多头，且有效周期
    ≥ 2（对应铁律一「只做上升趋势，不做下降趋势」）。缺周期时保守降级，
    不把「数据不足」当成顺势放行。
    """
    m = _period_position(monthly, "月线", "1M")
    w = _period_position(weekly, "周线", "1w")
    d = _period_position(daily, "日线", "1d")
    known = [x for x in (m, w, d) if not x["数据不足"]]
    no_bear = all(x["方向"] >= 0 for x in known)
    up_ok = d["方向"] == 1 or w["方向"] == 1
    trend_ok = bool(len(known) >= 2 and no_bear and up_ok)
    same_up = sum(1 for x in (m, w, d) if x["方向"] == 1)
    same_down = sum(1 for x in (m, w, d) if x["方向"] == -1)
    if same_up == 3:
        resonance = "三周期同向向上"
    elif same_up >= 2 and same_down == 0:
        resonance = "部分同向向上"
    elif same_down >= 1:
        resonance = "存在逆势周期"
    else:
        resonance = "方向不一致"
    return {
        "月线": m, "周线": w, "日线": d,
        "数据完整": len(known) == 3,
        "有效周期数": len(known),
        "向上周期数": same_up,
        "共振": resonance,
        "趋势允许": trend_ok,
        "位置摘要": (f"月线{m['位置']}/周线{w['位置']}/日线{d['位置']}"),
        "说明": ("已判定周期无空头且日线或周线多头" if trend_ok
                 else "存在空头周期、有效周期不足或缺少多头确认"),
    }


# ---------------------------------------------------------------------------
# 2. 三浪结构识别（可回测的分段极值算法）
# ---------------------------------------------------------------------------

def _empty_wave(phase: str, reason: str, n: int) -> Dict[str, Any]:
    return {
        "phase": phase, "reason": reason, "根数": n,
        "wave1": {"ok": False, "start_idx": None, "end_idx": None,
                  "low": None, "high": None, "gain_pct": None,
                  "bars": 0, "up_ratio": None},
        "wave2": {"exists": False, "end_idx": None, "low": None,
                  "retrace": None, "bars": 0, "structure_ok": False,
                  "volume_shrink": False, "vol_ratio": None,
                  "stabilized": False},
        "wave3": {"breakout": False, "break_idx": None, "volume_expand": False,
                  "vol_ratio": None, "bars_since_break": None},
        "entry_price": None, "stop_price": None, "stop_valid": False,
        "chase_risk": False, "chase_pct": None, "exhausted": False,
        "ma20": None,
    }


def detect_wave_structure(bars: Any, params: Optional[Dict] = None) -> Dict[str, Any]:
    """识别最近窗口内的第一浪 / 第二浪 / 第三浪结构。

    算法（顺序约束 i0 < i1 < i2 < i3，全部参数见 ``DEFAULTS``）：

    - i0：窗口前半段最低价（第一浪起点 L0）；
    - i1：i0 之后、且为二浪/突破留足根数区间内的最高价（第一浪高点 H1），
      要求 ``H1/L0-1 >= W1_MIN_GAIN``、段长 ≥ W1_MIN_BARS、
      段内收阳占比 ≥ W1_UP_RATIO（「明确的第一浪结构」）；
    - i2：i1 之后最低价（第二浪低点 L2），要求回撤
      ``(H1-L2)/(H1-L0)`` 落在 [W2_RETRACE_MIN, W2_RETRACE_MAX] 且 L2 > L0
      （「合理的调整结构」，不破一浪起点）；
    - 缩量 / 企稳：二浪段均量 ≤ 一浪段均量 × W2_VOL_SHRINK；
      i2 之后不再创新低；
    - i3：i2 之后首个收盘 ≥ H1 的 K 线（「明确的向上突破信号」），
      突破近 3 根最大量 ≥ 二浪段均量 × W3_VOL_EXPAND（「放量」）；
    - 止损 = L2 × (1 − STOP_BUFFER)；追高 = 相对 H1 涨幅 > CHASE_MAX
      或突破后已过 CHASE_MAX_BARS 根。

    该划分是**交易过滤规则**，不是对艾略特波浪的客观断言（见 DISCLAIMER）。
    """
    p = {**DEFAULTS, **(params or {})}
    rows = _normalize_bars(bars)
    if len(rows) < int(p["MIN_BARS"]):
        return _empty_wave("数据不足", f"有效日K仅{len(rows)}根，不足{int(p['MIN_BARS'])}根", len(rows))

    w = rows[-min(len(rows), int(p["LOOKBACK"])):]
    n = len(w)
    out = _empty_wave("无第一浪结构", "窗口内未找到满足涨幅与结构要求的第一浪", n)
    closes = [r["close"] for r in w]
    lows = [r["low"] for r in w]
    highs = [r["high"] for r in w]
    vols = [r["volume"] for r in w]

    # 趋势衰竭信号独立于三浪结构分支计算：任一分支提前返回也不会丢失。
    ma20_last = _ma(closes, 20, n - 1)
    out["ma20"] = round(ma20_last or 0.0, 4)
    out["exhausted"] = bool(ma20_last is not None and closes[-1] < ma20_last)

    # ---- i0：前半段最低 ----
    i0_end = max(1, int(n * 0.50))
    i0 = min(range(0, i0_end), key=lambda i: lows[i], default=None)
    if i0 is None:
        return out
    L0 = lows[i0]

    # ---- i1：区间内最高，且满足涨幅 / 段长 / 收阳占比 ----
    i1_start = i0 + int(p["W1_MIN_BARS"])
    i1_end = n - int(p["W2_MIN_BARS"]) - 1   # 至少给二浪留根数 + 1 根突破
    if i1_end <= i1_start:
        out["reason"] = "窗口尾部过短，无法容纳完整的浪2/浪3结构"
        return out
    i1 = max(range(i1_start, i1_end + 1), key=lambda i: highs[i], default=None)
    if i1 is None:
        return out
    H1 = highs[i1]
    gain = H1 / L0 - 1 if L0 > 0 else 0.0
    seg1 = closes[i0 + 1: i1 + 1]
    up_ratio = (sum(1 for k in range(1, len(seg1)) if seg1[k] > seg1[k - 1])
                / max(1, len(seg1) - 1))
    out["wave1"] = {
        "ok": False, "start_idx": i0, "end_idx": i1,
        "low": round(L0, 4), "high": round(H1, 4),
        "gain_pct": round(gain * 100, 2), "bars": i1 - i0,
        "up_ratio": round(up_ratio, 3),
    }
    w1_ok = bool(gain >= float(p["W1_MIN_GAIN"])
                 and (i1 - i0) >= int(p["W1_MIN_BARS"])
                 and up_ratio >= float(p["W1_UP_RATIO"]))
    out["wave1"]["ok"] = w1_ok
    if not w1_ok:
        out["reason"] = (f"第一浪涨幅{gain * 100:.1f}%/段长{i1 - i0}根/"
                         f"收阳占比{up_ratio:.2f} 未达标")
        return out
    out["phase"] = "第一浪确认"
    vol1 = _mean(vols[i0 + 1: i1 + 1]) or 0.0

    # ---- i2：i1 之后最低，回撤合理且不破 L0 ----
    i2_start = i1 + int(p["W2_MIN_BARS"])
    if i2_start >= n:
        out["phase"] = "第二浪调整中"
        out["reason"] = "第一浪后K线不足，第二浪尚未展开"
        out["wave2"]["exists"] = False
        return out
    i2 = min(range(i2_start, n), key=lambda i: lows[i], default=None)
    if i2 is None:
        return out
    L2 = lows[i2]
    retr = (H1 - L2) / (H1 - L0) if H1 > L0 else None
    out["wave2"] = {
        "exists": True, "end_idx": i2, "low": round(L2, 4),
        "retrace": round(retr, 4) if retr is not None else None,
        "bars": i2 - i1, "structure_ok": False,
        "volume_shrink": False, "vol_ratio": None, "stabilized": False,
    }
    structure_ok = bool(
        retr is not None
        and float(p["W2_RETRACE_MIN"]) <= retr <= float(p["W2_RETRACE_MAX"])
        and L2 > L0
        and (i2 - i1) >= int(p["W2_MIN_BARS"]))
    out["wave2"]["structure_ok"] = structure_ok
    vol2 = _mean(vols[i1 + 1: i2 + 1]) or 0.0
    vol_ratio2 = (vol2 / vol1) if vol1 > 0 else None
    shrink_ok = bool(vol_ratio2 is not None
                     and vol_ratio2 <= float(p["W2_VOL_SHRINK"]))
    out["wave2"]["vol_ratio"] = round(vol_ratio2, 3) if vol_ratio2 is not None else None
    out["wave2"]["volume_shrink"] = shrink_ok
    if not structure_ok:
        out["phase"] = "第二浪调整不合格"
        out["reason"] = (f"第二浪回撤{retr:.2f}（要求{p['W2_RETRACE_MIN']}~"
                         f"{p['W2_RETRACE_MAX']}）或跌破一浪起点") if retr else "回撤不可算"
        return out

    # 企稳：i2 之后不再创新低
    tail_lows = lows[i2 + 1:]
    stabilized = bool(not tail_lows or min(tail_lows) >= L2 * 0.99)
    out["wave2"]["stabilized"] = stabilized
    out["phase"] = "等待突破确认"

    # ---- i3：突破 H1 ----
    break_level = H1 * (1 - float(p["BREAK_TOL"]))
    i3 = next((j for j in range(i2 + 1, n) if closes[j] >= break_level), None)
    entry = closes[i3] if i3 is not None else closes[-1]
    out["entry_price"] = round(entry, 4)
    stop = L2 * (1 - float(p["STOP_BUFFER"]))
    out["stop_price"] = round(stop, 4)
    out["stop_valid"] = bool(stop > 0 and entry > stop)
    # 跌破保护性止损亦视为衰竭（与 MA20 判定取或）
    out["exhausted"] = bool(out["exhausted"] or closes[-1] < stop)
    if i3 is None:
        out["reason"] = "第二浪缩量企稳，尚未出现收盘站上第一浪高点的突破"
        return out

    vol_break = max(vols[max(i2 + 1, i3 - 2): i3 + 1]) if vols else 0.0
    vol_ratio3 = (vol_break / vol2) if vol2 > 0 else None
    expand_ok = bool(vol_ratio3 is not None
                     and vol_ratio3 >= float(p["W3_VOL_EXPAND"]))
    bars_since = (n - 1) - i3
    chase_pct = (entry - H1) / H1 if H1 > 0 else 0.0
    chase_risk = bool(chase_pct > float(p["CHASE_MAX"])
                      or bars_since > int(p["CHASE_MAX_BARS"]))
    out["wave3"] = {
        "breakout": True, "break_idx": i3, "volume_expand": expand_ok,
        "vol_ratio": round(vol_ratio3, 3) if vol_ratio3 is not None else None,
        "bars_since_break": bars_since,
    }
    out["chase_pct"] = round(chase_pct * 100, 2)
    out["chase_risk"] = chase_risk
    out["phase"] = "第三浪启动"
    out["reason"] = "收盘突破第一浪高点" + ("且放量" if expand_ok else "但未放量")
    return out


# ---------------------------------------------------------------------------
# 3. 九条铁律
# ---------------------------------------------------------------------------

def iron_rules(a: Dict[str, Any], strict: bool = False) -> Dict[str, Any]:
    """九条铁律逐条判定。

    原文第 4/6 条带「最好」，默认按 **soft 偏好**（影响 ``rule_quality``，
    不否决）；``strict=True`` 时升级为硬否决。第 9 条为元规则：前八条硬项
    全过且确有突破信号才算「符合交易系统的机会」。
    """
    pos = a.get("positions", {}) or {}
    wave = a.get("wave", {}) or {}
    w1, w2, w3 = wave.get("wave1", {}), wave.get("wave2", {}), wave.get("wave3", {})
    sizing = a.get("sizing", {}) or {}
    _e, _s = wave.get("entry_price"), wave.get("stop_price")
    _per_share = (round(_e - _s, 4)
                  if isinstance(_e, (int, float)) and isinstance(_s, (int, float))
                  else None)
    checks: List[Tuple[str, str, bool, str]] = [
        (IRON_RULES[0][0], "hard", bool(pos.get("趋势允许")),
         "已判定周期无空头且日线/周线至少其一多头"),
        (IRON_RULES[1][0], "hard", bool(w1.get("ok")),
         f"第一浪涨幅{w1.get('gain_pct')}%/段长{w1.get('bars')}根"),
        (IRON_RULES[2][0], "hard", bool(w2.get("structure_ok")),
         f"第二浪回撤{w2.get('retrace')}，不破一浪起点"),
        (IRON_RULES[3][0], "soft" if not strict else "hard",
         bool(w2.get("volume_shrink")),
         f"二浪/一浪均量比{w2.get('vol_ratio')}"),
        (IRON_RULES[4][0], "hard", bool(w3.get("breakout")),
         "收盘站上第一浪高点"),
        (IRON_RULES[5][0], "soft" if not strict else "hard",
         bool(w3.get("volume_expand")),
         f"突破/二浪量比{w3.get('vol_ratio')}"),
        (IRON_RULES[6][0], "hard",
         bool(wave.get("stop_valid") and sizing.get("valid")),
         f"止损{wave.get('stop_price')}，单股风险{_per_share}"),
        (IRON_RULES[7][0], "hard", not bool(wave.get("chase_risk")),
         f"相对一浪高点{wave.get('chase_pct')}%"),
    ]
    hard_ok = all(passed for _, sev, passed, _ in checks if sev == "hard")
    checks.append((IRON_RULES[8][0], "hard",
                   bool(hard_ok and w3.get("breakout")),
                   "前八条硬项全过且确有突破信号"))

    rules = [{"no": i + 1, "text": text, "severity": sev,
              "passed": bool(passed), "reason": reason}
             for i, (text, sev, passed, reason) in enumerate(checks)]
    failed_hard = [r["no"] for r in rules if r["severity"] == "hard" and not r["passed"]]
    soft_total = sum(1 for r in rules if r["severity"] == "soft")
    soft_pass = sum(1 for r in rules if r["severity"] == "soft" and r["passed"])
    hard_total = sum(1 for r in rules if r["severity"] == "hard")
    hard_pass = hard_total - len(failed_hard)
    quality = round((hard_pass + soft_pass) / max(1, hard_total + soft_total) * 100, 1)
    return {
        "rules": rules, "hard_pass": hard_pass, "hard_total": hard_total,
        "soft_pass": soft_pass, "soft_total": soft_total,
        "failed_hard": failed_hard, "all_pass": not failed_hard,
        "rule_quality": quality,
        "strict": bool(strict),
        "vetoes": [r["text"] for r in rules
                   if r["severity"] == "hard" and not r["passed"]],
    }


# ---------------------------------------------------------------------------
# 4. 十步流程状态机
# ---------------------------------------------------------------------------

def workflow(a: Dict[str, Any]) -> Dict[str, Any]:
    """返回当前推进到第几步、每步状态与下一步动作。"""
    pos = a.get("positions", {}) or {}
    wave = a.get("wave", {}) or {}
    rules = a.get("rules", {}) or {}
    w1, w2, w3 = wave.get("wave1", {}), wave.get("wave2", {}), wave.get("wave3", {})
    # 第 10 步「趋势衰竭分批止盈」只在其已建仓（第三浪突破）后才有意义；
    # 二浪回调中跌破 MA20 属正常调整，不得直接跳到止盈阶段。
    exhausted = bool(w3.get("breakout") and wave.get("exhausted"))
    flags = [
        bool(pos.get("趋势允许")),
        bool(w1.get("ok")) and pos.get("月线", {}).get("方向", -1) != -1,
        bool(w1.get("ok")),
        bool(w2.get("exists")),
        bool(w2.get("volume_shrink") and w2.get("stabilized")),
        bool(w3.get("breakout")),
        bool(w3.get("breakout") and rules.get("all_pass")),
        bool(wave.get("stop_valid")),
        bool(w3.get("breakout") and not exhausted),
        exhausted,
    ]
    if exhausted:
        stage = 10
    elif flags[5] and flags[6]:
        stage = 9
    else:
        stage = next((i + 1 for i, ok in enumerate(flags[:8]) if not ok), 1)

    steps = []
    for i, name in enumerate(WORKFLOW_STEPS):
        no = i + 1
        status = "已过" if no < stage else ("当前" if no == stage else "未到")
        steps.append({"no": no, "name": name, "status": status,
                      "passed": bool(flags[i])})
    next_action = {
        1: "先做市场环境筛选：月线/周线转多后再看个股",
        2: "在底部反转候选中寻找有明确第一浪结构的标的",
        3: "确认第一浪涨幅、段长与收阳占比达标",
        4: "等待第二浪回调，不追第一浪高点",
        5: "观察回调缩量与企稳（不创新低）",
        6: "等待收盘突破第一浪高点且放量确认",
        7: "按仓位公式分批买入第三浪",
        8: "以第二浪低点下方设保护性止损",
        9: "趋势未衰竭则跟随持有，不频繁进出",
        10: "出现趋势衰竭信号后分批止盈",
    }.get(stage, "等待")
    return {
        "stage": stage, "stage_name": WORKFLOW_STEPS[stage - 1],
        "steps": steps, "next_action": next_action,
        "note": MOTTO,
    }


# ---------------------------------------------------------------------------
# 5. 仓位计算
# ---------------------------------------------------------------------------

def position_size(equity: float, risk_pct: float, entry_price: Optional[float],
                  stop_price: Optional[float], lot_size: int = 100,
                  max_position_pct: Optional[float] = None) -> Dict[str, Any]:
    """仓位数量 = 账户总资金 × 单笔风险比例 ÷ (买入价格 − 止损价格)。

    按 A 股一手向下取整；无明确止损（``entry <= stop``）、除零、非正资金或
    风险比例越界一律返回 ``shares=0 / valid=False`` 并给出原因，绝不给出
    一个「凑出来」的仓位（铁律七）。``max_position_pct`` 为单票市值占权益
    上限，命中后按市值再截断一次。
    """
    eq, rp = _f(equity), _f(risk_pct)
    entry, stop = _f(entry_price), _f(stop_price)
    lot = int(lot_size) if lot_size and int(lot_size) > 0 else 1
    out: Dict[str, Any] = {
        "equity": eq, "risk_pct": rp, "entry_price": entry,
        "stop_price": stop, "risk_budget": None, "per_share_risk": None,
        "shares": 0, "lots": 0, "risk_amount": 0.0,
        "position_value": 0.0, "position_pct": 0.0,
        "stop_pct": None, "lot_size": lot, "capped": False,
        "valid": False, "reason": "",
    }
    if eq is None or eq <= 0:
        out["reason"] = "账户总资金必须为正"
        return out
    if rp is None or rp <= 0 or rp > 1:
        out["reason"] = "单笔风险比例须落在 (0, 1]"
        return out
    if entry is None or stop is None or entry <= 0 or stop <= 0:
        out["reason"] = "买入价/止损价缺失或非正"
        return out
    per = entry - stop
    out["per_share_risk"] = round(per, 6)
    out["stop_pct"] = round(per / entry * 100, 3)
    if per <= 0:
        out["reason"] = "无明确止损位置（买入价未高于止损价），坚决不买"
        return out
    budget = eq * rp
    out["risk_budget"] = round(budget, 2)
    raw = budget / per
    shares = int(raw // lot) * lot
    if max_position_pct is not None:
        cap = _f(max_position_pct)
        if cap is not None and 0 < cap <= 1:
            cap_shares = int((eq * cap / entry) // lot) * lot
            if cap_shares < shares:
                shares = cap_shares
                out["capped"] = True
    if shares <= 0:
        out["reason"] = "风险预算不足一手，不建仓"
        return out
    value = shares * entry
    out.update({
        "shares": shares, "lots": shares // lot,
        "risk_amount": round(shares * per, 2),
        "position_value": round(value, 2),
        "position_pct": round(value / eq * 100, 2),
        "valid": True,
        "reason": ("仓位公式成立，已按整手取整" +
                   ("并按单票上限截断" if out["capped"] else "")),
    })
    return out


# ---------------------------------------------------------------------------
# 6. 组合总风险（相关性）
# ---------------------------------------------------------------------------

def pearson(xs: Sequence[Optional[float]],
            ys: Sequence[Optional[float]],
            min_overlap: int = MIN_CORR_OVERLAP) -> Optional[float]:
    """两序列 Pearson 相关系数；样本不足 / 零方差返回 None。"""
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if _f(x) is not None and _f(y) is not None]
    if len(pairs) < min_overlap:
        return None
    n = len(pairs)
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sxx = sum((a - mx) ** 2 for a, _ in pairs)
    syy = sum((b - my) ** 2 for _, b in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def correlation_matrix(returns_by_symbol: Dict[str, Any],
                       min_overlap: int = MIN_CORR_OVERLAP) -> Dict[str, Any]:
    """由各标的日收益序列计算相关系数矩阵。

    ``returns_by_symbol`` 值可为 ``{date: ret}`` 或 ``[(date, ret), ...]``；
    重叠样本不足或零方差的对按 0 处理并记入 ``assumed_pairs``（诚实标注，
    不把「未知」伪装成「不相关」）。
    """
    series: Dict[str, Dict[str, float]] = {}
    for sym, raw in (returns_by_symbol or {}).items():
        if isinstance(raw, dict):
            series[str(sym)] = {str(k): float(v) for k, v in raw.items()
                               if _f(v) is not None}
        else:
            series[str(sym)] = {str(k): float(v) for k, v in (raw or [])
                               if _f(v) is not None}
    syms = sorted(series)
    matrix: Dict[str, Dict[str, float]] = {a: {} for a in syms}
    assumed: List[List[str]] = []
    for i, a in enumerate(syms):
        matrix[a][a] = 1.0
        for b in syms[i + 1:]:
            keys = sorted(set(series[a]) & set(series[b]))
            rho = pearson([series[a][k] for k in keys],
                          [series[b][k] for k in keys],
                          min_overlap=min_overlap)
            if rho is None:
                rho = 0.0
                assumed.append([a, b])
            matrix[a][b] = round(rho, 4)
            matrix[b][a] = round(rho, 4)
    return {"symbols": syms, "matrix": matrix, "assumed_pairs": assumed,
            "min_overlap": min_overlap}


def _rho_of(corr: Any, a: str, b: str) -> Tuple[float, bool]:
    """取 ρ(a,b)；缺数据按 0 处理并标记 assumed。"""
    if a == b:
        return 1.0, False
    matrix = (corr or {}).get("matrix") if isinstance(corr, dict) else None
    if matrix is None and isinstance(corr, dict):
        matrix = corr
    if isinstance(matrix, dict):
        row = matrix.get(a) or {}
        v = _f(row.get(b))
        if v is None:
            return 0.0, True
        return max(-1.0, min(1.0, v)), False
    return 0.0, True


def portfolio_risk(risks: Dict[str, float], corr: Any = None,
                   equity: Optional[float] = None,
                   max_total_risk_pct: float = MAX_PORTFOLIO_RISK_PCT,
                   high_corr: float = HIGH_CORR) -> Dict[str, Any]:
    """组合总风险：把每笔风险敞口按相关系数合成，而非简单相加。

    ``R = sqrt( Σ_i Σ_j r_i · r_j · ρ_ij )``（ρ_ii=1）。完全不相关时
    R=sqrt(Σr²)，完全正相关时 R=Σr —— 高度相关标的同持时总风险显著上升。
    当组合总风险占权益超过 ``max_total_risk_pct`` 时给出 ``scale_down``
    缩放系数，供上游等比下调各笔仓位。
    """
    items = [(str(k), max(0.0, _f(v) or 0.0)) for k, v in (risks or {}).items()]
    items = [(k, v) for k, v in items if v > 0]
    eq = _f(equity)
    out: Dict[str, Any] = {
        "count": len(items), "sum_risk": round(sum(v for _, v in items), 2),
        "portfolio_risk": 0.0, "diversification_ratio": None,
        "total_risk_pct": None, "scale_down": 1.0,
        "high_corr_pairs": [], "assumed_pairs": [],
        "matrix_psd": True, "risk_by_symbol": {k: v for k, v in items},
        "max_total_risk_pct": max_total_risk_pct, "high_corr": high_corr,
        "note": "",
    }
    if not items:
        out["note"] = "无有效持仓风险敞口"
        return out
    total = 0.0
    high_pairs: List[Dict[str, Any]] = []
    assumed_pairs: List[List[str]] = []
    for i, (a, ra) in enumerate(items):
        for j, (b, rb) in enumerate(items):
            rho, assumed = _rho_of(corr, a, b)
            if assumed and i < j:
                assumed_pairs.append([a, b])
            total += ra * rb * rho
            if i < j and abs(rho) >= high_corr:
                high_pairs.append({"a": a, "b": b, "rho": round(rho, 4)})
    if total < 0:  # 非半正定矩阵（负相关过强）→ 截断为 0 并诚实标注
        out["matrix_psd"] = False
        total = 0.0
    R = math.sqrt(total)
    s = sum(v for _, v in items)
    out["portfolio_risk"] = round(R, 2)
    out["diversification_ratio"] = round(R / s, 4) if s > 0 else None
    out["high_corr_pairs"] = high_pairs
    out["assumed_pairs"] = assumed_pairs
    if eq is not None and eq > 0:
        out["total_risk_pct"] = round(R / eq * 100, 3)
        cap = max_total_risk_pct * eq
        out["scale_down"] = round(min(1.0, cap / R), 4) if R > 0 else 1.0
        out["note"] = (f"组合总风险 {R:.2f} 元（占权益 {out['total_risk_pct']}%）"
                       + (f"，超出上限 {max_total_risk_pct * 100:.1f}% → 仓位建议 "
                          f"×{out['scale_down']}" if R > cap else "，未超风险预算"))
    else:
        out["note"] = f"组合总风险 {R:.2f} 元（未提供权益，未做风险预算校验）"
    if high_pairs:
        out["note"] += f"；存在 {len(high_pairs)} 对高度相关标的，需下调总仓位"
    if not out["matrix_psd"]:
        out["note"] += "；相关系数矩阵非半正定，合成风险已截断为 0，请复核数据"
    return out


# ---------------------------------------------------------------------------
# 7. 单票 / 组合编排
# ---------------------------------------------------------------------------

def evaluate(daily: Any, weekly: Any = None, monthly: Any = None, *,
             equity: float, risk_pct: float = 0.01,
             symbol: str = "", name: str = "",
             params: Optional[Dict] = None, strict: bool = False,
             lot_size: int = 100,
             max_position_pct: Optional[float] = None) -> Dict[str, Any]:
    """单票三浪主升评估：位置 → 结构 → 铁律 → 流程 → 仓位。

    返回纯 dict（JSON 可序列化）。``verdict`` 为
    ``候选 / 观察 / 回避`` 三态；不做任何综合评分。
    """
    positions = assess_positions(daily, weekly, monthly)
    wave = detect_wave_structure(daily, params)
    sizing = position_size(equity, risk_pct, wave.get("entry_price"),
                           wave.get("stop_price"), lot_size=lot_size,
                           max_position_pct=max_position_pct)
    a: Dict[str, Any] = {
        "symbol": symbol, "name": name or "未知",
        "positions": positions, "wave": wave,
        "sizing": sizing,
    }
    a["rules"] = iron_rules(a, strict=strict)
    a["workflow"] = workflow(a)
    if not positions.get("数据完整") and positions.get("有效周期数", 0) < 2:
        verdict = "回避"
        verdict_reason = "多周期K线不足，无法完成位置评估"
    elif a["rules"]["all_pass"] and sizing["valid"]:
        verdict = "候选"
        verdict_reason = "九条铁律硬项全过，可按仓位公式分批建仓"
    elif wave.get("phase") in ("第二浪调整不合格", "无第一浪结构", "数据不足"):
        verdict = "回避"
        verdict_reason = wave.get("reason", "结构不合格")
    else:
        verdict = "观察"
        verdict_reason = "结构尚未走完或铁律未全过，等待高质量机会"
    a["verdict"] = verdict
    a["verdict_reason"] = verdict_reason
    a["disclaimer"] = DISCLAIMER
    return a


def evaluate_portfolio(items: List[Dict[str, Any]], *, equity: float,
                       risk_pct: float = 0.01,
                       returns_by_symbol: Optional[Dict[str, Any]] = None,
                       corr: Any = None, strict: bool = False,
                       lot_size: int = 100,
                       max_position_pct: Optional[float] = None,
                       max_total_risk_pct: float = MAX_PORTFOLIO_RISK_PCT,
                       high_corr: float = HIGH_CORR) -> Dict[str, Any]:
    """组合层编排：逐票评估 → 筛候选 → 相关性 → 组合总风险 → 等比缩放仓位。

    ``items`` 每项为 ``{'symbol','name','daily','weekly','monthly'}``。
    只对 ``verdict == '候选'`` 的标的占用风险预算；``scale_down`` 后按整手
    重算实际仓位与风险额，避免「多只高相关标的各自合规、合计超风险」。
    """
    evaluated: List[Dict[str, Any]] = []
    for it in items or []:
        a = evaluate(it.get("daily"), it.get("weekly"), it.get("monthly"),
                     equity=equity, risk_pct=risk_pct,
                     symbol=str(it.get("symbol") or ""),
                     name=str(it.get("name") or ""),
                     params=it.get("params"), strict=strict,
                     lot_size=lot_size,
                     max_position_pct=max_position_pct)
        evaluated.append(a)
    candidates = [a for a in evaluated if a["verdict"] == "候选"]

    corr_info = corr
    if returns_by_symbol:
        corr_info = correlation_matrix({a["symbol"]: returns_by_symbol[a["symbol"]]
                                       for a in candidates
                                       if a["symbol"] in returns_by_symbol})
    risks = {a["symbol"]: a["sizing"]["risk_amount"] for a in candidates}
    risk = portfolio_risk(risks, corr_info, equity=equity,
                          max_total_risk_pct=max_total_risk_pct,
                          high_corr=high_corr)
    scale = float(risk.get("scale_down") or 1.0)
    lot = int(lot_size) if lot_size and int(lot_size) > 0 else 100
    for a in evaluated:
        s = a["sizing"]
        if a["verdict"] == "候选" and s["shares"] > 0:
            adjusted = int((s["shares"] * scale) // lot) * lot
            per = s["per_share_risk"] or 0.0
            s["suggested_shares"] = adjusted
            s["suggested_lots"] = adjusted // lot
            s["suggested_risk_amount"] = round(adjusted * per, 2)
            s["scale_down"] = scale
        else:
            s["suggested_shares"] = 0
            s["suggested_lots"] = 0
            s["suggested_risk_amount"] = 0.0
            s["scale_down"] = scale
    adjusted_total = sum(a["sizing"]["suggested_risk_amount"] for a in candidates)
    return {
        "account": {"equity": equity, "risk_pct": risk_pct,
                    "lot_size": lot, "strict": bool(strict),
                    "max_position_pct": max_position_pct},
        "scanned": len(evaluated), "candidates": len(candidates),
        "results": evaluated,
        "correlation": corr_info,
        "portfolio": {**risk, "adjusted_sum_risk": round(adjusted_total, 2)},
        "workflow_steps": list(WORKFLOW_STEPS),
        "iron_rules": [{"no": i + 1, "text": t, "severity": s}
                       for i, (t, s) in enumerate(IRON_RULES)],
        "motto": MOTTO,
        "disclaimer": DISCLAIMER,
    }
