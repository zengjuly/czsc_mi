"""mystery.core.turnover — 换手率派生纯函数（006.md 阶段 2 / W22）。

口径（与 mystery_rules 近20日均换手 <2% 等阈值一致，百分数 %）：
    turn_t = volume_t(股) / float_shares(股) × 100

边界：
- volume 单位「手」（tdx .day）→ 先 ×100 转股（unit='lot'）。
- 仅 0 < turn < 80 判为有效；越界丢弃并记 QA 计数，绝不伪造。
- auction_turnover_pct（竞价换手）不得用作全天 turn。
- legacy/official 已有 turn 的行绝不覆盖（本模块纯计算，不含写库判断）。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

# 换手合理区间（%）：日换手理论可近 100%（极端新股），>80 视为数据错误
TURN_MIN, TURN_MAX = 0.0, 80.0


def derived_turn(volume: Optional[float], float_shares: Optional[float],
                 unit: str = 'share') -> Optional[float]:
    """单根 K 线派生换手率（%）。无效输入返回 None（unknown 保持 unknown）。"""
    if volume is None or float_shares is None:
        return None
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return None
    try:
        s = float(float_shares)
    except (TypeError, ValueError):
        return None
    if v <= 0 or s <= 0:
        return None
    if unit == 'lot':          # tdx .day 成交量单位=手
        v *= 100.0
    turn = v / s * 100.0
    if TURN_MIN < turn < TURN_MAX:
        return round(turn, 4)
    return None                # 越界 → 丢弃（QA 统计由调用方记）


def estimate_float_shares(float_market_cap: Optional[float],
                          last_price: Optional[float]) -> Optional[float]:
    """股本推算：float_shares = float_market_cap / last_price（股）。"""
    try:
        cap = float(float_market_cap)
        px = float(last_price)
    except (TypeError, ValueError):
        return None
    if cap <= 0 or px <= 0:
        return None
    return cap / px


def shares_changed_enough(old: Optional[float], new: Optional[float],
                          threshold: float = 0.03) -> bool:
    """跳变校验：新推算股本相对旧值变化 >threshold（默认 3%）才覆盖。"""
    if old is None:
        return new is not None and new > 0
    if new is None or old <= 0:
        return False
    return abs(new - old) / old > threshold


def ffill_gaps(series: List[Tuple[str, Optional[float]]],
               max_gap: int = 5) -> Tuple[List[Tuple[str, Optional[float]]],
                                          List[str]]:
    """近端中间洞填充：连续缺失 ≤max_gap 日用前一个有效值填充。

    series: [(date, turn), ...] 按日期升序。
    返回 (填充后序列, 被填充的日期列表)。开头的洞（无前值）不填；
    超过 max_gap 的连续洞保持 NULL。
    """
    out = list(series)
    filled: List[str] = []
    i = 0
    # 跳过开头无前值的洞
    while i < len(out) and out[i][1] is None:
        i += 1
    while i < len(out):
        if out[i][1] is not None:
            i += 1
            continue
        j = i
        while j < len(out) and out[j][1] is None:
            j += 1
        gap = j - i
        if gap <= max_gap:
            prev = out[i - 1][1]
            for k in range(i, j):
                out[k] = (out[k][0], prev)
                filled.append(out[k][0])
        i = j
    return out, filled
