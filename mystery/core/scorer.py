"""mystery.core.scorer — 综合评分。

一期（P1/P2）：只用 Mystery 原公式，与 stock_analyzer 1.22.30 兼容。
P4（2026-08-28 用户确认开启）：MYSTERY_CHAN_ENABLED=1 时
S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan（S_chan 缺省 50）。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .models import ChanStructure, MysteryBreakdown


def chan_score(chan: Optional[Dict[str, ChanStructure]]) -> float:
    """缠论分：缺省 50；有 1d 结构时按最新笔方向 ±10、中枢内 +5。"""
    if not chan or '1d' not in chan:
        return 50.0
    c = chan['1d']
    s = 50.0
    if c.last_bi_dir == 'up':
        s += 10
    elif c.last_bi_dir == 'down':
        s -= 10
    if c.in_zs:
        s += 5
    return min(100.0, max(0.0, s))


def combine(breakdown: MysteryBreakdown,
            chan: Optional[Dict[str, ChanStructure]] = None,
            chan_enabled: bool = False) -> Tuple[Optional[float], str, bool]:
    """综合评分 + 操作建议 + 真三振。

    :return: (score, advice, true_resonance)
    """
    signal = breakdown.signal or {}
    s_mystery = signal.get('综合评分')
    advice = signal.get('操作建议', '')
    true_res = bool(signal.get('真三振', False))
    if not chan_enabled:
        return s_mystery, advice, true_res
    # 年线滤网一票否决：未通过时混合分强制 0（避免 0.2*S_chan 把否决股拉成正分）
    if signal.get('年线滤网') is False:
        return 0.0, advice, true_res
    # P4 混合权重：0.55*Mystery + 0.25*共振 + 0.20*缠论
    s_m = float(s_mystery or 0)
    s_r = float(signal.get('共振评分') or 0)
    s_c = chan_score(chan)
    score = round(0.55 * s_m + 0.25 * s_r + 0.20 * s_c, 1)
    return score, advice, true_res
