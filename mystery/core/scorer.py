"""mystery.core.scorer — 综合评分。

一期（P1/P2）：只用 Mystery 原公式，与 stock_analyzer 1.22.30 兼容。
MYSTERY_CHAN_ENABLED=1 仅启用缠论结构展示（AnalysisResult.chan），不进评分。
P4（待用户确认）：S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan（S_chan 缺省 50）。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .models import ChanStructure, MysteryBreakdown


def combine(breakdown: MysteryBreakdown,
            chan: Optional[Dict[str, ChanStructure]] = None,
            chan_enabled: bool = False) -> Tuple[Optional[float], str, bool]:
    """综合评分 + 操作建议 + 真三振。

    :return: (score, advice, true_resonance)
    P1/P2：score 恒为 Mystery 原公式（chan 只展示）。
    """
    signal = breakdown.signal or {}
    score = signal.get('综合评分')
    advice = signal.get('操作建议', '')
    true_res = bool(signal.get('真三振', False))
    # P4（待确认）：混合权重公式
    # if chan_enabled:
    #     s_mystery = float(score or 0)
    #     s_resonance = float(signal.get('共振评分') or 0)
    #     s_chan = 50.0
    #     score = round(0.55 * s_mystery + 0.25 * s_resonance + 0.20 * s_chan, 1)
    return score, advice, true_res
