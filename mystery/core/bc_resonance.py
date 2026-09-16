"""mystery.core.bc_resonance — 日/周线背驰共振标签（W34，纯函数零 IO）。

口径（20260524 版《趋势交易论》第二章【用缠论的背驰理论寻找不同级别的
卖点】：「多级别联动更精准——共振背驰信号是极佳转折点判断信号」）：

- 输入 = chan dict 里的日线/周线背驰标签（ChanStructure.divergence，
  由 adapter 的 tas_macd_bc 生成；本模块不碰 czsc、不取数）。
- 日线+周线同为顶背驰 → "日+周共振顶背驰"；同为底背驰 →
  "日+周共振底背驰"；单级别 → "日线顶背驰"/"周线底背驰" 等；无 → ""。
- 只展示，不进综合分（scorer 不 import 本模块）。
"""
from __future__ import annotations

from typing import Any, Dict


def _cn(div: str) -> str:
    """背驰标签归一（czsc 值含 '顶背驰'/'底背驰' 子串）。"""
    if "顶背驰" in (div or ""):
        return "顶"
    if "底背驰" in (div or ""):
        return "底"
    return ""


def bc_resonance(chan: Dict[str, Any]) -> str:
    """chan dict（freq → ChanStructure|to_dict）→ 共振展示标签。

    兼容对象与 dict 两种形态（reports 吃 to_dict，services 吃对象）。
    """
    def _div(freq: str) -> str:
        cs = (chan or {}).get(freq)
        if not cs:
            return ""
        if isinstance(cs, dict):
            return cs.get("divergence") or ""
        return getattr(cs, "divergence", "") or ""

    d1, dw = _cn(_div("1d")), _cn(_div("1w"))
    if d1 and dw:
        if d1 == dw:
            return f"日+周共振{d1}背驰"
        # 方向相反：不称共振，双列避免信息丢失
        return f"日线{d1}背驰 · 周线{dw}背驰"
    if d1:
        return f"日线{d1}背驰"
    if dw:
        return f"周线{dw}背驰"
    return ""
