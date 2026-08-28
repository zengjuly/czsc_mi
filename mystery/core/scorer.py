"""mystery.core.scorer — 综合评分。

一期（P1/P2）：只用 Mystery 原公式，与 stock_analyzer 1.22.30 兼容。
P4（2026-08-28 用户确认开启）：MYSTERY_CHAN_ENABLED=1 时
S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan（S_chan 缺省 50）。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .models import ChanStructure, MysteryBreakdown


def chan_score(chan: Optional[Dict[str, ChanStructure]]) -> float:
    """缠论分（可解释规则，权重 0.20，仅用 ChanStructure，core 不碰 czsc 对象）：

    - 无日线结构：50
    - 日线末笔 up 且已确认：+10；down 且已确认：-10
    - 日线当前在中枢内：+5
    - 有周线且周线末笔与日线同向：+8；反向：-8
    - 分数夹紧 [0, 100]
    """
    if not chan or '1d' not in chan:
        return 50.0
    c1 = chan['1d']
    s = 50.0
    if c1.last_bi_dir == 'up' and c1.last_bi_confirmed:
        s += 10
    elif c1.last_bi_dir == 'down' and c1.last_bi_confirmed:
        s -= 10
    if c1.in_zs:
        s += 5
    w = chan.get('1w')
    if w and c1.last_bi_dir and w.last_bi_dir:
        if w.last_bi_dir == c1.last_bi_dir:
            s += 8
        else:
            s -= 8
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
