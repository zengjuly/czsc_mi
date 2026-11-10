"""W82：典型 K 线组合形态识别（纯函数，零 IO）。

与既有模块的分工
----------------
- ``core.patterns``：头肩/双重/三角形等**大级别图表形态**（吃 DataFrame）。
- ``core.patterns_adv``：强势反包阳/缺口/低位地量等**高信噪比信号**（进轻加权）。
- 本模块：**典型 K 线组合形态**（多方炮、仙人指路、早晨之星…），
  日 K / 周 K **分别独立分析**，结果**仅展示**、不进综合分、不改任何信号。
  与 W69 三浪一致：service 层把结论挂到 ``AnalysisResult.to_dict()``，
  Excel / HTML / Web 只读，报告生成器不取数、不重算。

设计约束
--------
- 纯函数、零 IO：只吃 Bar 列表（``open/high/low/close/volume/dt``），
  不 import czsc / 数据客户端 / 配置模块；配置由 service 层注入。
- 判据全部用**相对量**（实体/影线/振幅/量比/均线相对位置），
  不依赖绝对价格与复权口径，日 K 与周 K 用同一套规则。
- 同一形态在不同位置可给出不同结论（例：长上影小实体在上涨初期是
  「仙人指路」、在高位是「射击之星」），故位置判据内置在检测函数里。
- 输出严格 JSON 可序列化（date 统一 ``str(dt)[:10]``）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

_EPS = 1e-9

# ---------------------------------------------------------------------------
# 形态注册表：code / 名称 / 方向 / 最少 K 线数 / 说明 / 检测函数
# ---------------------------------------------------------------------------
PATTERN_SPECS: Dict[str, Dict[str, Any]] = {
    "duofangpao": {
        "name": "多方炮", "direction": "看涨", "min_bars": 3,
        "desc": "两阳夹一阴：第三根阳线收盘超过第一根、且放量，短线上攻组合",
    },
    "xianrenzhilu": {
        "name": "仙人指路", "direction": "看涨", "min_bars": 21,
        "desc": "上涨初期带长上影的小实体试盘线，主力向上试探抛压",
    },
    "zaochenzhixing": {
        "name": "早晨之星", "direction": "看涨", "min_bars": 3,
        "desc": "大阴 + 小实体星线 + 大阳深入阴线实体过半，底部反转",
    },
    "huanghunzhixing": {
        "name": "黄昏之星", "direction": "看跌", "min_bars": 3,
        "desc": "大阳 + 小实体星线 + 大阴深入阳线实体过半，顶部反转",
    },
    "hongsanbing": {
        "name": "红三兵", "direction": "看涨", "min_bars": 3,
        "desc": "三连阳且收盘逐根抬高、开盘落在前一根实体内，稳步推升",
    },
    "sanzhiwuya": {
        "name": "三只乌鸦", "direction": "看跌", "min_bars": 3,
        "desc": "三连阴且收盘逐根降低，高位滞涨转弱",
    },
    "chuitouxian": {
        "name": "锤头线", "direction": "看涨", "min_bars": 11,
        "desc": "下跌末端长下影小实体，下探被承接的止跌信号",
    },
    "shejizhixing": {
        "name": "射击之星", "direction": "看跌", "min_bars": 21,
        "desc": "高位长上影小实体，上攻受阻的见顶警示",
    },
    "kanzhangtunmo": {
        "name": "看涨吞没", "direction": "看涨", "min_bars": 2,
        "desc": "后一根阳线实体完全包住前一根阴线实体（纯形态，不要求放量）",
    },
    "kandietunmo": {
        "name": "看跌吞没", "direction": "看跌", "min_bars": 2,
        "desc": "后一根阴线实体完全包住前一根阳线实体",
    },
    # ---- W83：多方炮最值得重点研究的三种形态 + 蓄势待确认态 ----
    # 四种均在 detail 内输出统一键「重点观察」，把该形态的核心验证点
    # 量化成结论文本（缺口支撑 / 调整缩量 / 回调低点抬高 / 下周一能否确认）。
    "gap_duofangpao": {
        "name": "跳空多方炮", "direction": "看涨", "min_bars": 6,
        "group": "多方炮变体",
        "desc": "首阳跳空突破 + 阴线回踩 + 再阳创新高；重点观察突破缺口是否得到支撑",
    },
    "liangyang_liangyin": {
        "name": "两阳夹两阴", "direction": "看涨", "min_bars": 5,
        "group": "多方炮变体",
        "desc": "阳-阴-阴-阳四根组合；重点观察调整期间是否缩量",
    },
    "diedie_duofangpao": {
        "name": "叠叠多方炮", "direction": "看涨", "min_bars": 7,
        "group": "多方炮变体",
        "desc": "两组以上阳-调整-阳叠加推升；重点观察每次回调低点是否抬高",
    },
    "duofangpao_pending": {
        "name": "多方炮蓄势(待确认)", "direction": "中性", "min_bars": 8,
        "group": "多方炮变体",
        "desc": "一阳后连续两天缩量震荡小阴小阳；重点观察下周一能否放量阳线确认",
    },
}

PATTERN_CODES: tuple = tuple(PATTERN_SPECS.keys())

# W83：多方炮变体分组（供网页配置按组展示 / 报告排序，不改评分）
PATTERN_GROUPS: Dict[str, List[str]] = {
    "多方炮变体": ["gap_duofangpao", "liangyang_liangyin",
                  "diedie_duofangpao", "duofangpao_pending"],
}


# ---------------------------------------------------------------------------
# Bar 适配与基础量
# ---------------------------------------------------------------------------
def _bars(bars) -> List[Any]:
    """兼容 BarSeries（含 .bars）或 List[Bar] / tuple。"""
    if bars is None:
        return []
    if hasattr(bars, "bars"):
        return list(bars.bars)
    return list(bars)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _o(b) -> float:
    return _num(getattr(b, "open", 0))


def _h(b) -> float:
    return _num(getattr(b, "high", 0))


def _l(b) -> float:
    return _num(getattr(b, "low", 0))


def _c(b) -> float:
    return _num(getattr(b, "close", 0))


def _v(b) -> float:
    return _num(getattr(b, "volume", 0))


def _date(b) -> str:
    return str(getattr(b, "dt", ""))[:10]


def _body(b) -> float:
    return abs(_c(b) - _o(b))


def _amp(b) -> float:
    return max(_h(b) - _l(b), _EPS)


def _up_shadow(b) -> float:
    return max(_h(b) - max(_o(b), _c(b)), 0.0)


def _dn_shadow(b) -> float:
    return max(min(_o(b), _c(b)) - _l(b), 0.0)


def _yang(b) -> bool:
    return _c(b) > _o(b) + _EPS


def _yin(b) -> bool:
    return _c(b) < _o(b) - _EPS


def _ma(bs: Sequence, n: int, idx: int) -> Optional[float]:
    """idx（含）往前 n 根收盘均价；不足返回 None。"""
    if idx + 1 < n or n <= 0:
        return None
    return sum(_c(bs[j]) for j in range(idx - n + 1, idx + 1)) / n


def _avg_body(bs: Sequence, n: int, idx: int) -> float:
    if idx + 1 < n or n <= 0:
        return 0.0
    return sum(_body(bs[j]) for j in range(idx - n + 1, idx + 1)) / n


def _pct_chg(bs: Sequence, n: int, idx: int) -> float:
    """近 n 根涨幅(%)，基准为 idx-n 的收盘。"""
    j = idx - n
    if j < 0 or _c(bs[j]) <= 0:
        return 0.0
    return (_c(bs[idx]) / _c(bs[j]) - 1.0) * 100.0


def _high_pos(bs: Sequence, n: int, idx: int) -> float:
    """现价在近 n 根最高/最低区间中的位置（0=最低, 1=最高）。"""
    lo = min(_l(bs[j]) for j in range(max(0, idx - n + 1), idx + 1))
    hi = max(_h(bs[j]) for j in range(max(0, idx - n + 1), idx + 1))
    if hi - lo <= _EPS:
        return 0.5
    return (_c(bs[idx]) - lo) / (hi - lo)


def _vol_ratio(bs: Sequence, idx: int, n: int = 5) -> float:
    """今量 / 前 n 日均量（不含当日）；无数据返回 0。"""
    j0 = max(0, idx - n)
    prev = bs[j0:idx]
    if not prev:
        return 0.0
    avg = sum(_v(x) for x in prev) / len(prev)
    return _v(bs[idx]) / avg if avg > 0 else 0.0


# ---------------------------------------------------------------------------
# 各形态检测：签名 (bs, i) -> Optional[detail dict]；i 为该形态最后一根 K 线
# ---------------------------------------------------------------------------
def _p_duofangpao(bs, i):
    a, b, c = bs[i - 2], bs[i - 1], bs[i]
    if not (_yang(a) and _yin(b) and _yang(c)):
        return None
    if _c(c) <= _c(a):                       # 第三阳必须创组合新高
        return None
    if _body(c) < _body(b):                  # 阳包阴
        return None
    if _l(b) < _o(a) - _body(a) * 0.5:      # 中间阴线不能砸穿第一阳实体太多
        return None
    vr = _v(c) / _v(b) if _v(b) > 0 else 0.0
    if vr < 1.0:                             # 第三根需放量
        return None
    return {"三根": f"{_o(a):.2f}/{_c(a):.2f} → {_o(b):.2f}/{_c(b):.2f}"
                    f" → {_o(c):.2f}/{_c(c):.2f}",
            "量比(三/二)": round(vr, 2), "收盘创新高": True}


def _long_upper_small_body(b) -> bool:
    """长上影 + 小实体 + 短下影（仙人指路 / 射击之星共用形态）。"""
    body, amp = _body(b), _amp(b)
    up, dn = _up_shadow(b), _dn_shadow(b)
    return (up >= 2.0 * body and up >= 0.55 * amp
            and body <= 0.35 * amp and dn <= up * 0.35 + _EPS)


def _p_xianrenzhilu(bs, i):
    b = bs[i]
    if not _long_upper_small_body(b) or not _yang(b):
        return None
    chg20 = _pct_chg(bs, 20, i)
    pos60 = _high_pos(bs, 60, i)
    ma20 = _ma(bs, 20, i)
    # 上涨初期/中途：近 20 日温和上涨、未到近 60 日高位、站上 MA20。
    # 与「射击之星」严格互斥（高位长上影归射击之星，不在此重复报多）。
    if not (0.0 < chg20 < 15.0 and pos60 < 0.85
            and ma20 is not None and _c(b) >= ma20):
        return None
    return {"上影/实体": round(_up_shadow(b) / max(_body(b), _EPS), 2),
            "近20日涨幅%": round(chg20, 1), "近60日位置": round(pos60, 2),
            "量比(前5日)": round(_vol_ratio(bs, i), 2),
            "站上MA20": True}


def _p_shejizhixing(bs, i):
    b = bs[i]
    if not _long_upper_small_body(b):
        return None
    chg20 = _pct_chg(bs, 20, i)
    pos60 = _high_pos(bs, 60, i)
    # 高位：近 20 日明显拉升 或 处于近 60 日高位区（与仙人指路互斥）
    if not (chg20 >= 15.0 or pos60 >= 0.85):
        return None
    return {"上影/实体": round(_up_shadow(b) / max(_body(b), _EPS), 2),
            "近20日涨幅%": round(chg20, 1), "近60日位置": round(pos60, 2),
            "量比(前5日)": round(_vol_ratio(bs, i), 2)}


def _p_zaochenzhixing(bs, i):
    a, b, c = bs[i - 2], bs[i - 1], bs[i]
    ref = _avg_body(bs, 10, i - 2)
    if not (_yin(a) and _body(a) >= max(ref, _EPS) * 0.9):
        return None
    if _body(b) > _body(a) * 0.45:           # 星线小实体
        return None
    if _c(b) > _c(a) + _EPS:                 # 星线重心下移（跳空/低开）
        return None
    if not (_yang(c) and _c(c) >= (_o(a) + _c(a)) / 2):  # 深入阴线实体过半
        return None
    mid = (_o(a) + _c(a)) / 2
    return {"深入阴线实体%": round((_c(c) - mid) / max(_body(a), _EPS) * 100, 1),
            "星线实体": round(_body(b), 2), "大阴实体": round(_body(a), 2)}


def _p_huanghunzhixing(bs, i):
    a, b, c = bs[i - 2], bs[i - 1], bs[i]
    ref = _avg_body(bs, 10, i - 2)
    if not (_yang(a) and _body(a) >= max(ref, _EPS) * 0.9):
        return None
    if _body(b) > _body(a) * 0.45:
        return None
    if _c(b) < _c(a) - _EPS:                 # 星线重心上移
        return None
    if not (_yin(c) and _c(c) <= (_o(a) + _c(a)) / 2):
        return None
    mid = (_o(a) + _c(a)) / 2
    return {"深入阳线实体%": round((mid - _c(c)) / max(_body(a), _EPS) * 100, 1),
            "星线实体": round(_body(b), 2), "大阳实体": round(_body(a), 2)}


def _p_hongsanbing(bs, i):
    a, b, c = bs[i - 2], bs[i - 1], bs[i]
    if not (_yang(a) and _yang(b) and _yang(c)):
        return None
    if not (_c(a) < _c(b) < _c(c)):
        return None
    # 开盘落在前一根实体区间内（稳步推升，非跳空暴涨）
    if not (_o(b) >= _o(a) and _o(b) <= _c(a) + _EPS):
        return None
    if not (_o(c) >= _o(b) and _o(c) <= _c(b) + _EPS):
        return None
    if _body(c) > _body(a) * 2.5:            # 排除末根异常放大
        return None
    return {"三连阳收盘": f"{_c(a):.2f} → {_c(b):.2f} → {_c(c):.2f}",
            "实体比(末/首)": round(_body(c) / max(_body(a), _EPS), 2)}


def _p_sanzhiwuya(bs, i):
    a, b, c = bs[i - 2], bs[i - 1], bs[i]
    if not (_yin(a) and _yin(b) and _yin(c)):
        return None
    if not (_c(a) > _c(b) > _c(c)):
        return None
    return {"三连阴收盘": f"{_c(a):.2f} → {_c(b):.2f} → {_c(c):.2f}",
            "近20日涨幅%": round(_pct_chg(bs, 20, i), 1)}


def _p_chuitouxian(bs, i):
    b = bs[i]
    body, amp = _body(b), _amp(b)
    up, dn = _up_shadow(b), _dn_shadow(b)
    if not (dn >= 2.0 * body and dn >= 0.55 * amp
            and body <= 0.35 * amp and up <= dn * 0.35 + _EPS):
        return None
    ma5 = _ma(bs, 5, i)
    pos60 = _high_pos(bs, 60, i)
    # 下跌末端：收盘在 MA5 之下 或 处于近 60 日低位
    if not ((ma5 is not None and _c(b) < ma5) or pos60 <= 0.25):
        return None
    return {"下影/实体": round(dn / max(body, _EPS), 2),
            "近60日位置": round(pos60, 2), "收盘低于MA5": ma5 is not None and _c(b) < ma5}


def _p_kanzhangtunmo(bs, i):
    y, t = bs[i - 1], bs[i]
    if not (_yin(y) and _yang(t)):
        return None
    if not (_o(t) <= _c(y) + _EPS and _c(t) >= _o(y) - _EPS):
        return None
    return {"昨阴": f"{_o(y):.2f}→{_c(y):.2f}",
            "今阳": f"{_o(t):.2f}→{_c(t):.2f}",
            "量比(前5日)": round(_vol_ratio(bs, i), 2)}


def _p_kandietunmo(bs, i):
    y, t = bs[i - 1], bs[i]
    if not (_yang(y) and _yin(t)):
        return None
    if not (_o(t) >= _c(y) - _EPS and _c(t) <= _o(y) + _EPS):
        return None
    return {"昨阳": f"{_o(y):.2f}→{_c(y):.2f}",
            "今阴": f"{_o(t):.2f}→{_c(t):.2f}",
            "量比(前5日)": round(_vol_ratio(bs, i), 2)}


# ---------------------------------------------------------------------------
# W83：多方炮三种重点变体 + 蓄势待确认态
#   统一约定：每个 detail 都带「重点观察」键，值为该形态核心验证点的量化
#   结论文本（是**观察结论**而非硬过滤条件），报告层原样只读展示。
# ---------------------------------------------------------------------------
def _p_gap_duofangpao(bs, i):
    """跳空多方炮：首阳向上跳空突破 → 阴线回踩 → 再阳创新高。

    重点观察：突破缺口是否得到支撑（用形态之后的 K 线验证是否回补）。
    """
    if i < 3:
        return None
    prev, a, b, c = bs[i - 3], bs[i - 2], bs[i - 1], bs[i]
    gap_lo = _h(prev)                     # 缺口下沿 = 突破前最高
    gap_hi = _l(a)                        # 缺口上沿 = 首阳最低
    if not (_yang(a) and _yin(b) and _yang(c)):
        return None
    if gap_hi <= gap_lo + _EPS:           # 必须是真实向上跳空缺口
        return None
    if _c(c) <= _c(a):                    # 第三阳创组合新高
        return None
    if _body(c) < _body(b):               # 阳包阴
        return None
    if _l(b) < gap_lo - _EPS:             # 阴线回踩不能当场补掉缺口
        return None
    # 重点观察：形态之后（含 b/c 至今）缺口是否一直守住
    later = bs[i + 1:]
    tail = [_l(x) for x in later]
    support_lo = min([_l(b), _l(c)] + tail)
    held = support_lo >= gap_lo - _EPS
    if not later:
        verdict = (f"突破缺口 {gap_lo:.2f}~{gap_hi:.2f} 暂获支撑"
                   f"（回踩最低 {support_lo:.2f}，尚无后续K线，待验证）")
    elif held:
        verdict = (f"突破缺口 {gap_lo:.2f}~{gap_hi:.2f} 得到支撑"
                   f"（后续 {len(later)} 根最低 {support_lo:.2f}，未回补）")
    else:
        verdict = (f"突破缺口 {gap_lo:.2f}~{gap_hi:.2f} 已回补"
                   f"（最低 {support_lo:.2f} 跌破 {gap_lo:.2f}），支撑失效")
    return {
        "重点观察": verdict,
        "缺口区间": f"{gap_lo:.2f}~{gap_hi:.2f}",
        "缺口幅度%": round((gap_hi / gap_lo - 1) * 100, 2) if gap_lo > 0 else 0.0,
        "缺口支撑": "守住" if held else "回补",
        "后续根数": len(later),
        "回踩最低": round(support_lo, 2),
        "三根收盘": f"{_c(a):.2f} → {_c(b):.2f} → {_c(c):.2f}",
    }


def _p_liangyang_liangyin(bs, i):
    """两阳夹两阴：阳-阴-阴-阳。重点观察：调整期间是否缩量。"""
    if i < 3:
        return None
    a, b, c, d = bs[i - 3], bs[i - 2], bs[i - 1], bs[i]
    if not (_yang(a) and _yin(b) and _yin(c) and _yang(d)):
        return None
    if _c(d) <= _c(a):                     # 末阳收复并创组合新高
        return None
    if min(_l(b), _l(c)) < _o(a) - _body(a) * 0.5:   # 调整不深破首阳实体
        return None
    va = _v(a)
    if va <= 0:
        return None
    avg_adj = (_v(b) + _v(c)) / 2.0
    ratio = avg_adj / va
    shrunk = ratio <= 0.85
    verdict = (f"调整期{'缩量' if shrunk else '未缩量'}"
               f"（两阴均量/首阳量 {ratio:.2f}"
               f"{'，浮筹清洗充分' if shrunk else '，抛压仍存需警惕'}）")
    return {
        "重点观察": verdict,
        "调整缩量": "是" if shrunk else "否",
        "两阴均量/首阳量": round(ratio, 2),
        "首阳量": round(va, 0),
        "两阴均量": round(avg_adj, 0),
        "末阳量/首阳量": round(_v(d) / va, 2),
        "四根收盘": f"{_c(a):.2f} → {_c(b):.2f} → {_c(c):.2f} → {_c(d):.2f}",
    }


def _p_diedie_duofangpao(bs, i):
    """叠叠多方炮：阳-阴-阳-阴-阳（5 根，两组叠加）或 7 根（三组）。

    重点观察：每次回调低点是否抬高。优先匹配更长的叠加结构。
    """
    for n_yang in (4, 3):               # 4 阳=7 根 / 3 阳=5 根
        n_bars = 2 * n_yang - 1
        if i + 1 < n_bars:
            continue
        w = bs[i - n_bars + 1: i + 1]
        yang, yin = w[0::2], w[1::2]
        if not all(_yang(x) for x in yang) or not all(_yin(x) for x in yin):
            continue
        closes = [_c(x) for x in yang]
        if not all(closes[k] > closes[k - 1] + _EPS
                   for k in range(1, len(closes))):   # 上攻收盘逐级抬高
            continue
        lows = [_l(x) for x in yin]
        rising = all(lows[k] > lows[k - 1] + _EPS
                     for k in range(1, len(lows)))
        seq = " → ".join(f"{x:.2f}" for x in lows)
        verdict = (f"每次回调低点{'逐级抬高' if rising else '未逐级抬高'}"
                   f"（{seq}）"
                   f"{'，上攻节奏健康' if rising else '，动能有衰减风险'}")
        return {
            "重点观察": verdict,
            "回调低点抬高": "是" if rising else "否",
            "回调低点序列": seq,
            "叠加组数": len(yin),
            "上攻收盘": " → ".join(f"{x:.2f}" for x in closes),
            "区间涨幅%": round((closes[-1] / closes[0] - 1) * 100, 2)
                        if closes[0] > 0 else 0.0,
        }
    return None


def _p_duofangpao_pending(bs, i):
    """多方炮蓄势(待确认)：一阳后连续两天缩量震荡小阴小阳。

    仅在**最新一根**报出（这是「当下待确认」状态，历史无需刷屏）；
    重点观察：下周一能否放量阳线确认，以及失守条件。方向为中性。
    """
    if i != len(bs) - 1 or i < 4:
        return None
    a, b, c = bs[i - 2], bs[i - 1], bs[i]
    if not _yang(a):
        return None
    ref_body = _avg_body(bs, 10, i - 3)   # 首阳之前 10 根的平均实体
    if _body(a) < max(ref_body, _EPS) * 1.3:      # 首阳须有力度
        return None
    vr = _vol_ratio(bs, i - 2, 5)
    if vr < 1.2:                                  # 首阳放量启动
        return None
    if max(_body(b), _body(c)) > _body(a) * 0.5:  # 后两天小阴小阳
        return None
    if max(_v(b), _v(c)) > _v(a) * 0.85:          # 连续两天缩量
        return None
    guard = _o(a) + _body(a) * 0.3                # 首阳实体下 1/3 上沿
    if min(_c(b), _c(c)) < guard:
        return None
    if min(_l(b), _l(c)) < _l(a) - _amp(a) * 0.1:  # 不破首阳低点
        return None
    trigger = max(_h(a), _h(b), _h(c))
    verdict = (f"下周一若放量阳线收盘站上 {trigger:.2f} 则确认多方炮；"
               f"跌破 {_o(a):.2f}（首阳实体下沿）则蓄势失败")
    return {
        "重点观察": verdict,
        "状态": "待确认",
        "确认位": round(trigger, 2),
        "失守位": round(_o(a), 2),
        "首阳量比(前5日)": round(vr, 2),
        "两日缩量比": round(((_v(b) + _v(c)) / 2) / max(_v(a), _EPS), 2),
        "两日实体/首阳": round(
            ((_body(b) + _body(c)) / 2) / max(_body(a), _EPS), 2),
        "三根收盘": f"{_c(a):.2f} → {_c(b):.2f} → {_c(c):.2f}",
        "首阳日期": _date(a),
    }


_DETECTORS: Dict[str, Callable] = {
    "duofangpao": _p_duofangpao,
    "xianrenzhilu": _p_xianrenzhilu,
    "zaochenzhixing": _p_zaochenzhixing,
    "huanghunzhixing": _p_huanghunzhixing,
    "hongsanbing": _p_hongsanbing,
    "sanzhiwuya": _p_sanzhiwuya,
    "chuitouxian": _p_chuitouxian,
    "shejizhixing": _p_shejizhixing,
    "kanzhangtunmo": _p_kanzhangtunmo,
    "kandietunmo": _p_kandietunmo,
    # W83：多方炮三种重点变体 + 蓄势待确认态
    "gap_duofangpao": _p_gap_duofangpao,
    "liangyang_liangyin": _p_liangyang_liangyin,
    "diedie_duofangpao": _p_diedie_duofangpao,
    "duofangpao_pending": _p_duofangpao_pending,
}


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def resolve_enabled(enabled: Optional[Sequence[str]]) -> List[str]:
    """归一化启用列表：None/空 → 全部；非法 code 忽略；保持注册表顺序去重。"""
    if not enabled:
        return list(PATTERN_CODES)
    if isinstance(enabled, str):
        enabled = [x.strip() for x in enabled.split(",")]
    wanted = {str(x).strip() for x in enabled if str(x).strip()}
    return [c for c in PATTERN_CODES if c in wanted]


def format_summary(hits: Sequence[Dict[str, Any]], limit: int = 3) -> str:
    """命中列表 → 简短中文摘要（供 Excel/Web 列，无命中返回 '—'）。

    同一形态可能连续多根命中（如稳步上涨期的红三兵），摘要按 code
    **去重**（只留最近一次）再取前 limit 个，避免一个形态占满列宽。
    """
    if not hits:
        return "—"
    seen, uniq = set(), []
    for h in hits:                       # hits 已按日期降序
        code = h.get("code")
        if code in seen:
            continue
        seen.add(code)
        uniq.append(h)
    parts = []
    for h in uniq[:max(1, int(limit))]:
        d = str(h.get("date") or "")[5:] or "-"
        arrow = "多" if h.get("direction") == "看涨" else (
            "空" if h.get("direction") == "看跌" else "中")
        parts.append(f"{h.get('name', '?')}({d}·{arrow})")
    return "、".join(parts)


def format_focus(hits: Sequence[Dict[str, Any]], limit: int = 3) -> str:
    """W83：把命中里的「重点观察」结论聚合成一段文本（供报告列）。

    - 只取带「重点观察」键的形态（W82 的 10 个基础形态没有该键，自动跳过）；
    - 按 code 去重（同一形态连续命中只留最近一次），按日期降序已在 hits 中；
    - 无命中返回 '—'，与 W82 的 summary 口径一致，报告层无需判空特例。
    """
    if not hits:
        return "—"
    seen, parts = set(), []
    for h in hits:
        code = h.get("code")
        if code in seen:
            continue
        focus = (h.get("detail") or {}).get("重点观察")
        if not focus:
            continue
        seen.add(code)
        parts.append(f"{h.get('name', '?')}：{focus}")
        if len(parts) >= max(1, int(limit)):
            break
    return "；".join(parts) or "—"


def detect_combo_patterns(bars, *, period: str = "daily",
                          enabled: Optional[Sequence[str]] = None,
                          lookback: int = 60,
                          max_hits_per_pattern: int = 2) -> Dict[str, Any]:
    """在最近 ``lookback`` 根 K 线内检测启用的典型组合形态。

    :param bars: BarSeries 或 List[Bar]（日 K / 周 K 由调用方分别传入）
    :param period: ``daily`` / ``weekly``（仅作为结果标记，日周各自独立调用）
    :param enabled: 启用的形态 code 列表；None/空 = 全部
    :param lookback: 回溯窗口（根数），至少 5
    :param max_hits_per_pattern: 每个形态最多保留最近几个命中
    :return: ``{'period','lookback','n_bars','enabled','n_hits','hits','summary'}``
             hits 元素：``{'code','name','direction','date','detail'}``，按日期降序。
    """
    bs = _bars(bars)
    codes = resolve_enabled(enabled)
    lb = max(5, int(lookback or 60))
    per = max(1, int(max_hits_per_pattern or 2))
    empty = {"period": period, "lookback": lb, "n_bars": len(bs),
             "enabled": codes, "n_hits": 0, "hits": [], "summary": "—",
             "focus": "—"}
    if not bs or not codes:
        return empty

    per_code: Dict[str, List[Dict[str, Any]]] = {c: [] for c in codes}
    start = max(0, len(bs) - lb)
    # 仅从「该形态所需最少前置根数满足」的位置开始，避免越界与无谓计算
    for code in codes:
        fn = _DETECTORS.get(code)
        if fn is None:
            continue
        min_bars = int(PATTERN_SPECS[code].get("min_bars", 2))
        # min_bars 含所需的前置上下文（如 21 = 20 日涨幅 + 当日）
        i0 = max(start, min_bars - 1)
        for i in range(i0, len(bs)):
            try:
                det = fn(bs, i)
            except Exception:  # noqa: BLE001 - 单根异常不影响整池
                det = None
            if det is None:
                continue
            spec = PATTERN_SPECS[code]
            per_code[code].append({
                "code": code, "name": spec["name"],
                "direction": spec["direction"], "date": _date(bs[i]),
                "detail": det,
            })
            if len(per_code[code]) >= per * 4:   # 控制内存，尾部裁剪
                per_code[code] = per_code[code][-per:]

    hits: List[Dict[str, Any]] = []
    for code in codes:
        got = per_code[code][-per:]            # 保留最近 per 个
        got.reverse()                          # 日期降序
        hits.extend(got)
    hits.sort(key=lambda h: (h.get("date") or ""), reverse=True)
    return {"period": period, "lookback": lb, "n_bars": len(bs),
            "enabled": codes, "n_hits": len(hits), "hits": hits,
            "summary": format_summary(hits), "focus": format_focus(hits)}


__all__ = ["PATTERN_SPECS", "PATTERN_CODES", "PATTERN_GROUPS",
           "resolve_enabled", "format_summary", "format_focus",
           "detect_combo_patterns"]
