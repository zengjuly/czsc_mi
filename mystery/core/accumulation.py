"""W86：主力建仓 / 吸筹研判（core 纯函数，零 IO、只读、不进评分）。

统一给「关注的指数 / 行业板块 / 个股」输出同一套吸筹研判 dict，供日报
Excel/HTML、Web 扫描表与个股页只读展示。

判据来自原论 ``docs/mistery20260524.md``（【主力的吸筹方式】【小阳建仓】
【低位诱空】【筹码结构/集中度/筹码峰】【所有的底…资金筑底】）与系统既有
口径（``core/scan_signals.py`` 的 chip_low、``core/stage.py`` 的阶段识别、
``core/turnover_tag.py`` 的换手分档）。四层证据链：

1. **位置**：现价相对近 120 根高点回撤 ≥15%（CHIP_LOW_RETRACE=0.15），
   或贴近 120 日低点（≤1.1×low120）。
2. **量能**：近 5 日均量 / 前 20 日均量 = vol_trend；缩量沉淀（<0.90）
   或温和放量（1.10~1.50）为吸筹痕迹；放量滞涨/价跌量增判为派发。
3. **筹码/阶段**：个股读 ``stage`` + ``chip_low`` + ``turnover_20``；
   指数/板块无换手，走纯价格阶段（横盘 + 低波动）。
4. **排除**：高位放量滞涨、价跌量增 → 派发；明确拉升 → 已启动（非吸筹）。

设计约束（与 W82/W83/W84 一致）：
- core 零 IO：只吃调用方传入的 K 线序列 / ``to_dict()`` 结果，不碰库、网络；
- 不改综合分 / 信号 / ``RULE_VER``，结论仅展示；
- 缺数据一律 ``level='未知'``，绝不把 unknown 当吸筹、绝不伪造换手。

吸筹区间按原论是**观察区间而非买点**，``conclusion`` 中会明确提示。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# 与 core/scan_signals.py 完全一致的低位回撤门闩（单一来源，不另立口径）
LOW_RETRACE = 0.15
# 近 5 日均量 / 前 20 日均量 的吸筹量能带
VOL_SHRINK = 0.90
VOL_MILD_EXPAND = 1.10
VOL_HEAVY = 1.50
# 阶段置信：横盘（|20日涨跌| ≤5%）+ 低波动（20日振幅 <20%）
FLAT_BAND = 0.05
AMP_LOW = 0.20

_LEVEL_CN = {
    "strong": "强吸筹", "medium": "疑似吸筹", "weak": "观察",
    "none": "非吸筹", "unknown": "数据不足",
}


def _rows(bars) -> List[Dict[str, Any]]:
    """把 BarSeries / List[Bar] / List[dict] / DataFrame 归一成 dict 列表。

    只取 OHLCV + 日期，零副作用；无法识别的结构返回空列表。
    """
    if bars is None:
        return []
    seq = getattr(bars, "bars", None)
    if seq is None:
        seq = bars
    # pandas.DataFrame
    if hasattr(seq, "to_dict") and hasattr(seq, "columns"):
        try:
            return seq.to_dict("records")
        except Exception:  # noqa: BLE001
            return []
    out: List[Dict[str, Any]] = []
    try:
        for b in seq:
            if isinstance(b, dict):
                out.append(b)
            else:
                out.append({
                    "dt": getattr(b, "dt", None),
                    "date": getattr(b, "dt", None),
                    "open": getattr(b, "open", None),
                    "high": getattr(b, "high", None),
                    "low": getattr(b, "low", None),
                    "close": getattr(b, "close", None),
                    "volume": getattr(b, "volume", None),
                    "turnover": getattr(b, "turnover", None),
                })
    except TypeError:
        return []
    return out


def _series(rows: Sequence[Dict[str, Any]], *keys: str) -> List[float]:
    """取某一数值列的有限序列（None/非数跳过，保持顺序）。"""
    vals: List[float] = []
    for r in rows:
        for k in keys:
            v = r.get(k) if isinstance(r, dict) else None
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f and f > 0:  # 排除 NaN 与非正（价格/量）
                vals.append(f)
                break
    return vals


def _dates(rows: Sequence[Dict[str, Any]]) -> List[str]:
    out = []
    for r in rows:
        v = r.get("trade_date") or r.get("date") or r.get("dt")
        out.append(str(v)[:10] if v is not None else "")
    return out


def _pct(a: float, b: float) -> Optional[float]:
    try:
        return (a - b) / b
    except (TypeError, ZeroDivisionError):
        return None


def _grade(*, low: bool, flat: bool, vol_state: str, amp_low: bool,
           stage: str, chip_low: bool, turnover: Optional[float],
           distrib: bool, rally: bool,
           above_year: Optional[bool] = None) -> str:
    """四层证据打分 → strong / medium / weak / none。

    W85 护栏：跌破年线（``above_year=False``）时，即便证据分够 strong 也
    降级为 medium——原论「严格大盘腿」与【所有的底…资金筑底】都强调年线
    是重要滤网，跌破年线的低位横盘只能算「疑似吸筹 / 观察区间」。
    """
    if distrib or rally:
        return "none"
    score = 0
    if low:
        score += 30
    if flat:
        score += 15
    if vol_state in ("缩量沉淀", "温和放量"):
        score += 25
    elif vol_state == "极度缩量":
        score += 12
    if amp_low:
        score += 8
    if stage == "吸筹":
        score += 20
    elif stage == "打压吸筹":
        score += 12
    elif stage == "洗盘":
        score += 8
    if chip_low:
        score += 20
    if turnover is not None:
        if turnover < 2:
            score += 8
        elif 3 <= turnover <= 5:
            score += 8
    # 「强吸筹」必须同时具备 低位 + 横盘 + 有效量能，且不能跌破年线
    strong_ok = (low and flat and vol_state in ("缩量沉淀", "温和放量")
                 and above_year is not False)
    if score >= 75 and strong_ok:
        return "strong"
    if score >= 75 and low and flat and above_year is False:
        return "medium"           # 年线下降级
    if score >= 45:
        return "medium"
    if score >= 20:
        return "weak"
    return "none"


def analyze_accumulation(bars, *, period: str = "daily",
                         lookback: int = 120,
                         stage: Optional[str] = None,
                         chip_low: Optional[bool] = None,
                         turnover_20: Optional[float] = None,
                         name: str = "") -> Dict[str, Any]:
    """基于 K 线的吸筹研判（指数 / 板块 / 个股通用的价格+量能口径）。

    :param bars: BarSeries / List[Bar] / List[dict] / DataFrame（升序）。
    :param stage: 外部已算好的阶段（如 ``core.stage.classify_stage`` 结果），
                   不重算；None 时按纯价格判据推断。
    :param chip_low / turnover_20: 个股特有筹码证据，指数/板块传 None。
    :returns: 可 JSON 序列化的研判 dict，缺数据 ``level='unknown'``。
    """
    base: Dict[str, Any] = {
        "name": name, "period": period, "level": "unknown",
        "level_cn": _LEVEL_CN["unknown"], "score": 0,
        "stage": stage or "未知", "chip_low": bool(chip_low),
        "date": "", "close": None, "retrace_pct": None,
        "near_low": False, "above_year": None, "vol_trend": None,
        "vol_state": "未知", "chg_20d": None, "amp20": None,
        "turnover_20": turnover_20,
        "evidence": [], "conclusion": "数据不足，无法研判", "has_data": False,
    }
    rows = _rows(bars)
    if len(rows) < 30:
        return base
    closes = _series(rows, "close", "收盘价")
    vols = _series(rows, "volume", "成交量")
    highs = _series(rows, "high", "最高价")
    lows = _series(rows, "low", "最低价")
    if len(closes) < 30:
        return base
    dates = _dates(rows)
    win = min(len(closes), max(30, int(lookback or 120)))
    c = closes[-win:]
    price = closes[-1]
    hi = max(c)
    lo = min(c)
    # 回撤比例 = 1 - price/近120高（0~1，越大越低位）。
    # 位置判据用「区间相对位置」而非 0.9*hi / 1.1*lo 硬阈值：后者在横盘
    # 序列（hi≈lo≈price）会恒为真，把横盘误判为高位或低位。
    retrace = round(1 - price / hi, 4) if hi > 0 else None
    span = hi - lo
    pos = ((price - lo) / span) if span > 1e-9 else None
    near_top = bool(pos is not None and pos >= 0.85)
    near_low = bool(pos is not None and pos <= 0.20)
    low = bool(retrace is not None and retrace >= LOW_RETRACE) or near_low
    # 年线（250 日）位置：与 W84 / stage.py 同口径；不足 250 根为 None。
    above_year: Optional[bool] = None
    if len(closes) >= 250:
        ma250 = sum(closes[-250:]) / 250
        if ma250 > 0:
            above_year = bool(price > ma250)

    t20 = _pct(price, closes[-21]) if len(closes) >= 21 else None
    flat = bool(t20 is not None and abs(t20) <= FLAT_BAND)
    chg20 = round(t20 * 100, 2) if t20 is not None else None

    vol_trend = None
    vol_state = "未知"
    if len(vols) >= 25:
        v_recent = sum(vols[-5:]) / 5
        v_prev = sum(vols[-25:-5]) / 20
        if v_prev > 0:
            vol_trend = round(v_recent / v_prev, 3)
            if vol_trend < 0.5:
                vol_state = "极度缩量"
            elif vol_trend < VOL_SHRINK:
                vol_state = "缩量沉淀"
            elif vol_trend <= VOL_MILD_EXPAND:
                vol_state = "常量"
            elif vol_trend <= VOL_HEAVY:
                vol_state = "温和放量"
            else:
                vol_state = "明显放量"
    vol_up = bool(vol_trend is not None and vol_trend > VOL_MILD_EXPAND)

    amp20 = None
    if len(closes) >= 20 and len(highs) >= 20 and len(lows) >= 20:
        hh, ll = max(highs[-20:]), min(lows[-20:])
        avg = sum(closes[-20:]) / 20
        if avg > 0:
            amp20 = round((hh - ll) / avg, 4)
    amp_low = bool(amp20 is not None and amp20 < AMP_LOW)

    # 阶段：优先外部注入（个股/板块已有 stage），否则纯价格推断。
    # 关键：派发（高位出货）必须同时满足「高位」；低位放量下跌是原论
    # 【打压吸筹 / 低位诱空】的手法，不能误判为派发。与 stage.py 的区别
    # 在于 stage.py 的价跌量增分支服务个股且配合 POC/趋势，这里服务指数/
    # 板块（无换手/筹码峰），故显式加 high-ground 前提，避免系统性误判。
    stg = stage or "未知"
    if stg == "未知":
        if near_top and vol_up and (t20 is not None and t20 < 0.05):
            stg = "派发"
        elif near_top and (t20 is not None and t20 < -0.03) and vol_up:
            stg = "派发"
        elif t20 is not None and t20 > 0.08:
            stg = "拉升"
        elif flat and vol_up and low:
            stg = "吸筹"
        elif low and (t20 is not None and t20 < -0.03) and vol_up:
            stg = "打压吸筹"        # 低位放量下跌：诱空手法，非派发
        elif flat and amp_low:
            stg = "震荡"

    distrib = stg == "派发"
    rally = stg in ("拉升", "派发")
    level = _grade(low=low, flat=flat, vol_state=vol_state, amp_low=amp_low,
                   stage=stg, chip_low=bool(chip_low), turnover=turnover_20,
                   distrib=distrib, rally=rally, above_year=above_year)

    ev: List[str] = []
    if low:
        ev.append(f"低位：较近{win}根高点回撤{(retrace or 0) * 100:.1f}%")
    elif near_top:
        ev.append("高位：贴近区间上沿")
    if flat:
        ev.append(f"横盘：20日涨跌{chg20:+.1f}%")
    if vol_state in ("缩量沉淀", "极度缩量", "温和放量", "明显放量", "常量"):
        ev.append(f"{vol_state}：量比{vol_trend}")
    if amp_low:
        ev.append(f"低波动：20日振幅{amp20 * 100:.1f}%")
    if stg and stg != "未知":
        ev.append(f"阶段：{stg}")
    if chip_low:
        ev.append("筹码低位共振（缩量+低位）")
    if turnover_20 is not None:
        if turnover_20 < 2:
            ev.append(f"换手{turnover_20:.2f}%：缩量沉淀")
        elif 3 <= turnover_20 <= 5:
            ev.append(f"换手{turnover_20:.2f}%：吸筹区")
    if above_year is False:
        ev.append("⚠ 跌破年线：按严格大盘腿降级，仅作观察")
    elif above_year is True:
        ev.append("站上年线")
    if distrib:
        ev.append("⚠ 派发特征：高位放量滞涨 / 价跌量增，排除吸筹")
    if stg == "打压吸筹":
        ev.append("低位放量下跌：疑似打压吸筹/低位诱空，需看后续缩量企稳")

    if level == "none":
        concl = "非吸筹" + ("（疑似派发，注意回避）" if distrib else
                          "（已启动或证据不足）")
    else:
        concl = (f"{_LEVEL_CN[level]}：低位+量能+阶段证据成立；"
                 "按原论属观察区间，非买点，需等放量突破与回踩确认")
        if above_year is False:
            concl += "（当前跌破年线，已降级）"

    base.update({
        "level": level, "level_cn": _LEVEL_CN[level],
        "score": 0, "stage": stg, "date": dates[-1] if dates else "",
        "close": round(price, 2), "retrace_pct": (
            round(retrace * 100, 2) if retrace is not None else None),
        "near_low": near_low, "above_year": above_year,
        "vol_trend": vol_trend, "vol_state": vol_state,
        "chg_20d": chg20, "amp20": amp20, "turnover_20": turnover_20,
        "evidence": ev, "conclusion": concl, "has_data": True,
    })
    return base


def accumulation_from_result(d: Dict[str, Any], *, name: str = "") -> Dict[str, Any]:
    """从个股 ``to_dict()`` / scan payload **只读**推导吸筹研判，不取数、不重算。

    优先复用既有字段：``mystery.stage.stage`` / ``chip_low`` /
    ``turnover_20`` / ``price`` / ``high_120`` / ``mystery.technical``；
    历史 job 缺字段时退化为纯字段判据，绝不把 unknown 当 True。
    """
    m = d.get("mystery") or {}
    stg = (m.get("stage") or {}).get("stage") or "未知"
    chip_low = bool(d.get("chip_low")) and not bool(d.get("chip_low_unknown"))
    turn = d.get("turnover_20")
    try:
        turn_f = float(turn) if turn is not None else None
    except (TypeError, ValueError):
        turn_f = None
    price, hi120 = d.get("price"), d.get("high_120")
    try:
        retrace = round(1 - float(price) / float(hi120), 4) \
            if price is not None and hi120 else None
    except (TypeError, ValueError, ZeroDivisionError):
        retrace = None
    if chip_low is None:
        chip_low = False
    low = bool(retrace is not None and retrace >= LOW_RETRACE) or chip_low
    # 无 K 线序列：用已有字段做证据打分
    from_none = (m.get("signal") or {})
    # 年线滤网（mystery.signal.年线滤网，basic_filter 已判定）→ above_year。
    # 缺字段保持 None，不把「未知」当成跌破年线。
    yl = from_none.get("年线滤网")
    above_year: Optional[bool] = bool(yl) if isinstance(yl, bool) else None
    flat = bool(stg in ("吸筹", "震荡"))
    amp_low = bool(stg == "吸筹")
    distrib = stg == "派发"
    rally = stg in ("拉升", "派发")
    level = _grade(low=low, flat=flat, vol_state="常量", amp_low=amp_low,
                   stage=stg, chip_low=chip_low, turnover=turn_f,
                   distrib=distrib, rally=rally, above_year=above_year)
    ev: List[str] = []
    if low:
        ev.append(f"低位：回撤{(retrace or 0) * 100:.1f}%" if retrace is not None
                  else "低位：筹码低位共振")
    if chip_low:
        ev.append("筹码低位（缩量+低位双门闩成立）")
    if d.get("chip_quiet"):
        ev.append("⚠ 高位缩量（chip_quiet），不计吸筹")
    if d.get("chip_low_unknown"):
        ev.append("⚠ 换手数据缺失，禁止判定低换手")
    if turn_f is not None:
        ev.append(f"近20日均换手{turn_f:.2f}%")
    if stg != "未知":
        ev.append(f"阶段：{stg}")
    if above_year is False:
        ev.append("⚠ 跌破年线：按严格大盘腿降级，仅作观察")
    elif above_year is True:
        ev.append("站上年线")
    if from_none.get("低位地量"):
        ev.append("低位地量（量能压缩）")
    if from_none.get("强势反包阳"):
        ev.append("强势反包阳（打压吸筹后收复）")
    if distrib:
        ev.append("⚠ 派发特征，排除吸筹")
    concl = ("非吸筹（疑似派发，注意回避）" if distrib else
             f"{_LEVEL_CN[level]}：按原论属观察区间，非买点，需等放量突破确认"
             if level != "none" else "非吸筹（已启动或证据不足）")
    if above_year is False and level != "none":
        concl += "（当前跌破年线，已降级）"
    return {
        "name": name or d.get("name") or "", "period": "result",
        "level": level, "level_cn": _LEVEL_CN[level], "score": 0,
        "stage": stg, "chip_low": chip_low, "date": d.get("trade_date") or "",
        "close": price, "retrace_pct": (
            round(retrace * 100, 2) if retrace is not None else None),
        "near_low": low, "above_year": above_year,
        "vol_trend": None, "vol_state": "未知",
        "chg_20d": None, "amp20": None, "turnover_20": turn_f,
        "evidence": ev, "conclusion": concl, "has_data": bool(
            stg != "未知" or chip_low or turn_f is not None or retrace is not None),
    }


def accumulation_label(acc: Optional[Dict[str, Any]]) -> str:
    """展示用短标签；旧 payload 无字段显示 '—'。"""
    if not acc:
        return "—"
    return str(acc.get("level_cn") or "—")


def accumulation_summary_line(items: List[Dict[str, Any]],
                              label: str = "吸筹研判") -> str:
    """一行摘要（飞书/QA 行）：只列强吸筹/疑似吸筹，缺数据跳过。"""
    parts = []
    for d in items:
        acc = d.get("accumulation") or {}
        if acc.get("level") in ("strong", "medium"):
            parts.append(f"{d.get('name', '?')}({acc.get('level_cn')})")
    return f"[{label}] " + "、".join(parts) if parts else f"[{label}] 暂无强吸筹信号"


__all__ = [
    "LOW_RETRACE", "VOL_SHRINK", "VOL_MILD_EXPAND", "VOL_HEAVY",
    "analyze_accumulation", "accumulation_from_result",
    "accumulation_label", "accumulation_summary_line",
]
