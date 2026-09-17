"""mystery.core.scorer — 综合评分。

一期（P1/P2）：只用 Mystery 原公式，与 stock_analyzer 1.22.30 兼容。
P4 公式已落地：MYSTERY_CHAN_ENABLED=1 且 MYSTERY_CHAN_SCORE=1 时
S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan（S_chan 缺省 50）。
0.10.0（010.md 阶段6C）：S_chan 换 6B 标定规则表（价格口径中枢位置 +
czsc 买卖点/背驰标签），仅「结构开 + 分开关」路径生效；分关路径与
rule_ver 常量 mystery-1.22.30-compat 不变。
生产默认 chan.score=false（混合分关），综合分 = Mystery 1.22.30；
与 analyze.py 的 chan_enabled()/chan_score_enabled() 两个开关一致。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .models import ChanStructure, MysteryBreakdown


def chan_score(chan: Optional[Dict[str, ChanStructure]]) -> float:
    """缠论分 S_chan（010.md 6B 标定规则表；只用 ChanStructure，
    core 不碰 czsc 对象；30 只快照标定，分布见 docs/010.md）：

    - 无日线结构：50（基准不变）
    - 日线末笔确认 up / down：+12 / −12
    - 收盘价 vs 末中枢（价格口径 zs_position）：above +8 / in +2 / below −8；
      老缓存缺该字段时回退旧时间口径 in_zs +5（与 1.22.30 行为兼容）
    - 周线末笔与日线同向 / 反向：+8 / −8
    - czsc 买卖点标签 bs_flag：一买/二买/三买 +10；一卖/二卖/三卖 −10
      （无标签 → 0，禁止在 core 猜）
    - czsc 背驰标签 divergence：顶背驰 −10 / 底背驰 +10（无标签 → 0）
    - 分数夹紧 [0, 100]
    """
    if not chan or '1d' not in chan:
        return 50.0
    c1 = chan['1d']
    s = 50.0
    if c1.last_bi_dir == 'up' and c1.last_bi_confirmed:
        s += 12
    elif c1.last_bi_dir == 'down' and c1.last_bi_confirmed:
        s -= 12
    pos = getattr(c1, 'zs_position', '')
    if pos == 'above':
        s += 8
    elif pos == 'in':
        s += 2
    elif pos == 'below':
        s -= 8
    elif c1.in_zs:
        # 老缓存（无 6A 字段）：回退时间口径，保持旧契约加分
        s += 5
    w = chan.get('1w')
    if w and c1.last_bi_dir and w.last_bi_dir:
        if w.last_bi_dir == c1.last_bi_dir:
            s += 8
        else:
            s -= 8
    bs = getattr(c1, 'bs_flag', '')
    if bs in ('一买', '二买', '三买'):
        s += 10
    elif bs in ('一卖', '二卖', '三卖'):
        s -= 10
    div = getattr(c1, 'divergence', '')
    # W47（外部 review P1#6）：精确匹配。divergence 由 czsc_adapter 归一为
    # "顶背驰"/"底背驰"/""，串匹配会在上游吐复合串（如"顶背驰_疑似"）时误计分。
    if div == '顶背驰':
        s -= 10
    elif div == '底背驰':
        s += 10
    return min(100.0, max(0.0, s))


def combine(breakdown: MysteryBreakdown,
            chan: Optional[Dict[str, ChanStructure]] = None,
            mix_enabled: bool = False) -> Tuple[Optional[float], str, bool]:
    """综合评分 + 操作建议 + 真三振。

    W47：参数名从 chan_enabled 改为 mix_enabled——它控制的是「混合分
    （0.7*Mystery+0.3*Chan）」，不是「chan 结构展示」。两开关语义不同
    （生产默认：结构开、混合分关），旧名极易误导成把结构开关直接传入。

    :return: (score, advice, true_resonance)
    """
    signal = breakdown.signal or {}
    s_mystery = signal.get('综合评分')
    advice = signal.get('操作建议', '')
    true_res = bool(signal.get('真三振', False))
    if not mix_enabled:
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
