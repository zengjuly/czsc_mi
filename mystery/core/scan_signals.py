"""mystery.core.scan_signals — 扫描三类信号分类（纯函数，只吃 to_dict()）。

口径对照（002.md W1-B）：
- true_resonance：直接用结果的 ``true_resonance``。
- vap_atr_break：映射 ``mystery.vap_atr`` / ``mystery.platform`` 已有突破字段，不重算 POC。
- chip_low：旧口径近 20 日均换手 < 2%；to_dict() 无该数 → False 并标
  ``chip_low_unknown=True``（禁止再拉 K 线）。
"""
from __future__ import annotations

from typing import Any, Dict

CHIP_LOW_TURNOVER = 2.0  # 旧口径：近20日均换手 < 2% 视为筹码低位


def classify(d: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {vap_atr_break, chip_low, true_resonance, labels, chip_low_unknown}。"""
    out: Dict[str, Any] = {
        "vap_atr_break": False,
        "chip_low": False,
        "true_resonance": bool(d.get("true_resonance")),
        "chip_low_unknown": False,
        "labels": [],
    }
    m = d.get("mystery", {}) or {}
    vap = m.get("vap_atr", {}) or {}
    plat = m.get("platform", {}) or {}

    brk = bool(vap.get("突破信号") or plat.get("突破信号"))
    out["vap_atr_break"] = brk
    if brk:
        out["labels"].append("VAP-ATR突破")

    # 筹码低位：近20日均换手（多路径读取，见 003.md：turnover_20 顶层优先，不再拉 K 线）
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
    if avg_turnover is None:
        out["chip_low"] = False
        out["chip_low_unknown"] = True
    else:
        try:
            out["chip_low"] = float(avg_turnover) < CHIP_LOW_TURNOVER
        except Exception:
            out["chip_low"] = False
            out["chip_low_unknown"] = True
        if out["chip_low"]:
            out["labels"].append("筹码低位共振")

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
