# -*- coding: utf-8 -*-
"""W32b（013.md）：换手分档 + 趋势/情绪风格标签 —— 只读纯函数，零 IO。

铁律：
- 不进综合分、不改排序、不动 chip_low 的 2%/低位门；
- 阈值只来自原论叙述（docs/mistery20260524.md【关于换手率】等）；
- 无有效 turn 一律 unknown，禁止编造分档、禁止显示「非吸筹」；
- 报表/Web 只准调用本模块对 to_dict 已有字段打标签（不重算规则）。
"""
from typing import Any, Dict, Optional

# 分档键（unknown/absorb/inflow/heavy/flee/other）→ 中文展示
TAG_CN = {
    'unknown': '未知',
    'absorb': '吸筹区(3-5)',
    'inflow': '流入(8-15)',
    'heavy': '高换手(≥25)',
    'flee': '极端换手(≥70)',
    'other': '常规',
}

STYLE_CN = {
    'unknown': '未知',
    'sentiment': '情绪',
    'trend': '趋势',
    'mixed': '混合',
}


def turnover_tag(turn) -> str:
    """近端 20 日均换手(%) → 分档键。None/NaN/≤0 或无法解析 → unknown。"""
    if turn is None:
        return 'unknown'
    try:
        v = float(turn)
    except (TypeError, ValueError):
        return 'unknown'
    if v != v or v <= 0:          # NaN 或非法值
        return 'unknown'
    if v >= 70:
        return 'flee'
    if v >= 25:
        return 'heavy'
    if 8 <= v <= 15:
        return 'inflow'
    if 3 <= v <= 5:
        return 'absorb'
    return 'other'


def style_tag(turn, yearline_pass: Optional[bool],
              main_bull: Optional[bool] = None) -> str:
    """趋势/情绪风格（只读，不排序）：
    情绪 = 近端高换手(≥15)且非年线滤网主路径；
    趋势 = 年线滤网过 或 主升信号；
    混合 = 两条件都沾；未知 = 无 turn 且无滤网结论。
    """
    t = turnover_tag(turn)
    high_turn = t in ('heavy', 'flee') or (
        turn is not None and t != 'unknown' and _ge(turn, 15))
    trend_side = bool(yearline_pass) or bool(main_bull)
    if t == 'unknown' and yearline_pass is None and main_bull is None:
        return 'unknown'
    if high_turn and trend_side:
        return 'mixed'
    if high_turn:
        return 'sentiment'
    if trend_side:
        return 'trend'
    return 'unknown'


def _ge(turn, threshold: float) -> bool:
    try:
        return float(turn) >= threshold
    except (TypeError, ValueError):
        return False


def tags_from_result(d: Dict[str, Any]) -> Dict[str, str]:
    """从 to_dict() 结果提取两列展示值（缺字段 → 未知，不报错）。"""
    sig = (d.get('mystery') or {}).get('signal') or {}
    yl = sig.get('年线滤网')
    mb = sig.get('主升浪信号')
    t = turnover_tag(d.get('turnover_20'))
    s = style_tag(d.get('turnover_20'),
                  yl if isinstance(yl, bool) else None,
                  mb if isinstance(mb, bool) else None)
    return {'turnover_tag': TAG_CN[t], 'style_tag': STYLE_CN[s]}
