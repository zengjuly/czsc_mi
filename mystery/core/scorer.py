"""mystery.core.scorer — 综合评分。

一期（P1/P2）：只用 Mystery 原公式，与 stock_analyzer 1.22.30 兼容。
P4 公式已落地：MYSTERY_CHAN_ENABLED=1 且 MYSTERY_CHAN_SCORE=1 时
S = 0.55*S_mystery + 0.25*S_resonance + 0.20*S_chan（S_chan 缺省 50）。
0.10.0（010.md 阶段6C）：S_chan 换 6B 标定规则表（价格口径中枢位置 +
czsc 买卖点/背驰标签），仅「结构开 + 分开关」路径生效；分关路径与
rule_ver 常量 mystery-1.22.30-compat 不变。
多周期方向因子（G1）由 mystery.core.multi_period 计算，经 combine(mpd_enabled)
接入最终综合分（月线基线权重 × 基准 + 周/日同向加分 − 逆势降权，夹 [0,100]）。
默认关（mpd_enabled=False）：保持与 1.22.30 金标完全可比；开启后叠加因子、
不触碰 1.22.30 量纲。与 analyze.py 的 chan_enabled()/chan_score_enabled()/
multi_period_factor_enabled()/pattern_weight_enabled() 开关一致。
env MYSTERY_MPD_SCORE=1 开启多周期；env MYSTERY_PATTERN_WEIGHT=1 开启形态轻加权。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .models import ChanStructure, MysteryBreakdown
from .multi_period import apply_factor

# ---- 形态轻加权（G4 提升：信号 → 轻加权；默认关，开启后叠加，不改动 1.22.30 量纲）----
PAT_W_ENGULF = 4.0         # 强势反包阳（买点）
PAT_W_GAP_BREAKOUT = 4.0   # 突破缺口（买：横盘后跳空）
PAT_W_GAP_CONT = 2.0       # 持续缺口（拿：上涨途中，温和加分）
PAT_W_LOWVOL = 4.0         # 低位地量（底部吸筹）
PAT_W_GAP_EXHAUST = -6.0   # 衰竭缺口（卖/减仓：降权）


def pattern_adjustment(sig: Optional[Dict[str, Any]]) -> float:
    """高信噪比形态对综合分的轻量调整（opt-in）。

    强势反包阳 / 突破缺口 / 低位地量 → 加分；衰竭缺口 → 降权（理论：衰竭缺口卖）。
    权重轻、仅作微调，不取代 1.22.30 主分；改动须升 models.RULE_VER。
    """
    if not sig:
        return 0.0
    adj = 0.0
    if sig.get("强势反包阳"):
        adj += PAT_W_ENGULF
    g = sig.get("缺口类型")
    if g == "突破":
        adj += PAT_W_GAP_BREAKOUT
    elif g == "持续":
        adj += PAT_W_GAP_CONT
    elif g == "衰竭":
        adj += PAT_W_GAP_EXHAUST
    if sig.get("低位地量"):
        adj += PAT_W_LOWVOL
    return adj


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
            mix_enabled: bool = False,
            mpd_enabled: bool = False,
            pattern_weight: bool = False) -> Tuple[Optional[float], str, bool]:
    """综合评分 + 操作建议 + 真三振。

    W47：参数名从 chan_enabled 改为 mix_enabled——它控制的是「混合分
    （0.7*Mystery+0.3*Chan）」，不是「chan 结构展示」。两开关语义不同
    （生产默认：结构开、混合分关），旧名极易误导成把结构开关直接传入。
    mpd_enabled：多周期方向因子（G1）是否接入综合分，默认关——保持与
    1.22.30 金标可比；开启后叠加因子、不改动 1.22.30 量纲本身。
    pattern_weight：高信噪比形态轻加权（G4）是否接入综合分，默认关；
    开启后对综合分做轻量加减（强势反包阳/突破缺口/低位地量+，衰竭缺口−），
    base<=0（年线否决）时不叠加、不复活否决股。

    :return: (score, advice, true_resonance)
    """
    signal = breakdown.signal or {}
    s_mystery = signal.get('综合评分')
    advice = signal.get('操作建议', '')
    true_res = bool(signal.get('真三振', False))
    if not mix_enabled:
        score = s_mystery
    else:
        # 年线滤网一票否决：未通过时混合分强制 0（避免 0.2*S_chan 把否决股拉成正分）
        if signal.get('年线滤网') is False:
            return 0.0, advice, true_res
        # P4 混合权重：0.55*Mystery + 0.25*共振 + 0.20*缠论
        s_m = float(s_mystery or 0)
        s_r = float(signal.get('共振评分') or 0)
        s_c = chan_score(chan)
        score = round(0.55 * s_m + 0.25 * s_r + 0.20 * s_c, 1)
    # 多周期方向因子（G1）接入综合分：月线基线 × 基准 + 周/日同向加分 − 逆势降权；
    # base<=0（年线滤网否决）或因子缺失时 apply_factor 原样返回，不复活否决股。
    if mpd_enabled:
        score = apply_factor(score, signal.get('多周期方向因子'))
    # 高信噪比形态轻加权（G4）：仅微调，base<=0（否决）时不叠加、不复活。
    if pattern_weight and score and score > 0:
        score = min(100.0, max(0.0, round(float(score) + pattern_adjustment(signal), 1)))
    return score, advice, true_res
