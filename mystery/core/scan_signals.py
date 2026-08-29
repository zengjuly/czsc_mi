"""mystery.core.scan_signals — 扫描三类信号分类（纯函数，只吃 to_dict()）。

口径（004.md，名实相符）：
- true_resonance：直接用结果的 ``true_resonance``。
- vap_atr_break：映射 ``mystery.vap_atr`` / ``mystery.platform`` 已有突破字段，不重算 POC。
- chip_low = 缩量(turnover_20<2) AND 低位(平台门 或 价格回撤门)，二者缺一不算。
  - 无 turnover_20 → chip_low=False + chip_low_unknown=True（禁止 unknown 当 True）。
  - 有换手但 <2% 且不满足低位 → chip_quiet=True（高位缩量，不进 chip_low 过滤）。
  - 换手 ≥2% → chip_low / chip_quiet 均 False。
  不 fetch_bars、不重算 VAP。
"""
from __future__ import annotations

from typing import Any, Dict

CHIP_LOW_TURNOVER = 2.0   # 近20日均换手 < 2% 视为缩量
CHIP_LOW_RETRACE = 0.15   # 现价相对近120日最高回撤 ≥15% 视为低位（回撤门闩）

_PLATFORM_LOW_HINTS = ('买横机会',)


def _platform_low(plat_status: Any) -> bool:
    """平台状态含「买横机会」（源码真实写入的低位语义句）→ 平台低位门过。

    005.md：只收代码真实写入的状态句，删过宽的「离场」「未突破」；
    对不上真实句就当这门未过（只用回撤门闩）。
    """
    s = str(plat_status or '')
    return any(k in s for k in _PLATFORM_LOW_HINTS)


def _price_pos(d: Dict[str, Any]) -> Any:
    """现价相对近120日最高回撤比例 1 - price/high_120；数据缺失返回 None。"""
    price, high_120 = d.get('price'), d.get('high_120')
    if price is None or high_120 is None:
        return None
    try:
        return round(1 - float(price) / float(high_120), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def classify(d: Dict[str, Any]) -> Dict[str, Any]:
    """返回稳定键：vap_atr_break / chip_low / chip_low_unknown / chip_quiet /
    true_resonance / turnover_20 / price_pos / labels。
    """
    out: Dict[str, Any] = {
        "vap_atr_break": False,
        "chip_low": False,
        "chip_low_unknown": False,
        "chip_quiet": False,
        "true_resonance": bool(d.get("true_resonance")),
        "turnover_20": None,
        "price_pos": None,
        "labels": [],
    }
    m = d.get("mystery", {}) or {}
    vap = m.get("vap_atr", {}) or {}
    plat = m.get("platform", {}) or {}

    brk = bool(vap.get("突破信号") or plat.get("突破信号"))
    out["vap_atr_break"] = brk
    if brk:
        out["labels"].append("VAP-ATR突破")

    # 均换手多路径：turnover_20 顶层 → mystery.turnover_20 → 自适应周期 → vap_atr.avg_turnover
    avg_turnover = None
    if d.get("turnover_20") is not None:
        avg_turnover = d.get("turnover_20")
    elif m.get("turnover_20") is not None:
        avg_turnover = m.get("turnover_20")
    else:
        cyc = vap.get("自适应周期") or {}
        if isinstance(cyc, dict) and cyc.get("avg_turnover") is not None:
            avg_turnover = cyc.get("avg_turnover")
        elif vap.get("avg_turnover") is not None:
            avg_turnover = vap.get("avg_turnover")
    out["turnover_20"] = avg_turnover

    price_pos = _price_pos(d)
    out["price_pos"] = price_pos

    if avg_turnover is None:
        out["chip_low"] = False
        out["chip_low_unknown"] = True
    else:
        try:
            t = float(avg_turnover)
        except (TypeError, ValueError):
            out["chip_low"] = False
            out["chip_low_unknown"] = True
        else:
            if t < CHIP_LOW_TURNOVER:
                low = _platform_low(plat.get("平台状态")) or (
                    price_pos is not None and price_pos >= CHIP_LOW_RETRACE)
                if low:
                    out["chip_low"] = True
                    out["labels"].append("筹码低位共振")
                else:
                    out["chip_quiet"] = True   # 高位缩量，不进 chip_low 过滤
            # t >= 2%：chip_low / chip_quiet 均保持 False

    if out["true_resonance"]:
        out["labels"].append("真三振")
    return out


def filter_by_signal(results: list, signal: str) -> list:
    """按信号名过滤扫描结果（signal ∈ vap_atr / chip_low / true_resonance）。"""
    key = {"vap_atr": "vap_atr_break", "chip_low": "chip_low",
           "true_resonance": "true_resonance"}.get(signal)
    if key is None:
        return results
    return [r for r in results if r.get(key)]
