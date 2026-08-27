"""mystery.core.scorer — 综合评分。

一期（MYSTERY_CHAN_ENABLED=0）：只用 Mystery 原公式，与 stock_analyzer 1.22.30 兼容。
二期：S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan（S_chan 缺省 50）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .models import AnalysisResult, ChanStructure, MysteryBreakdown


def combine(breakdown: MysteryBreakdown, chan: Optional[Dict[str, ChanStructure]],
            chan_enabled: bool = False) -> Tuple[Optional[float], str, bool]:
    """综合评分 + 操作建议 + 真三振。

    :return: (score, advice, true_resonance)
    """
    if not chan_enabled:
        signal = breakdown.resonance.get("signal", {})
        score = signal.get("综合评分")
        advice = signal.get("操作建议", "")
        true_res = bool(breakdown.resonance.get("真三振", False))
        return score, advice, true_res
    raise NotImplementedError("P4: 小权重缠论分")
