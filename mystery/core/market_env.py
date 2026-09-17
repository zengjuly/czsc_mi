"""mystery.core.market_env — 大盘滤网展示（W37，纯函数零 IO）。

单一实现：所有入口（Excel/HTML/Web/终端）只读调用 anchor_env/market_env。
规则原文（Mistery趋势交易论 143-155 行）的年线滤网语义搬到大盘层面**只作
展示与筛选辅助，不进 scorer、不改 rule_ver**（用户「规则不变只改展示」）。

锚选择（实测 2026-09-16）：同花顺全A 880008.TI 与中证全指 000985 在 fuyao
index-historical 均无数据（对照组 000001.SH 正常），故用 上证 000001.SH +
国证1000 399311.SZ 双锚近似全市场；两者都走既有 MarketDataClient.fetch_index
归一通道（会话缓存，每进程一次降级链），无旁路取数。

注意（W41 审校）：399311 是**国证1000**（沪深北大中盘指数），不是深证成指
（深证成指 = 399001.SZ）。不选 399001 是实测原因：W39 核查时 399001 在上游
停更约三周，而 399311 数据新鲜（max 到最近交易日）。仅作全市场代理展示。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 展示用锚：(显示名, 内部代码)。代码可被 normalize/fetch_index 直接消化。
ANCHORS = (("上证指数", "000001.SH"), ("国证1000", "399311.SZ"))

_MIN_BARS = 250   # 年线滤网需 ≥250 根


def _ma(closes: List[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def anchor_env(name: str, code: str,
               closes: List[float]) -> Dict[str, Any]:
    """单锚大盘环境快照（缺数据 → unknown 字段 None，不抛）。"""
    out: Dict[str, Any] = {"name": name, "code": code, "close": None,
                           "ma250": None, "above": None, "aligned": None,
                           "chg_20d": None, "date": ""}
    if not closes or len(closes) < _MIN_BARS:
        return out
    c = closes[-1]
    ma5, ma20, ma60 = _ma(closes, 5), _ma(closes, 20), _ma(closes, 60)
    ma250 = _ma(closes, 250)
    if ma5 is None or ma20 is None or ma60 is None or ma250 is None:
        return out
    out["close"] = round(c, 2)
    out["ma250"] = round(ma250, 2)
    out["above"] = bool(c > ma250)                       # 站上年线
    out["aligned"] = bool(ma5 > ma20 > ma60 > ma250)     # 多头顺次排列
    out["chg_20d"] = round((c / closes[-21] - 1) * 100, 2)
    return out


def market_env(anchors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """双锚合成判定（只展示）：
    全「上年线+多头排列」→ 多头滤网通过；任一破年线 → 警示；否则中间态。
    """
    got = [a for a in anchors if a.get("above") is not None]
    if len(got) < len(anchors) or not got:
        verdict = "大盘未知"          # 有锚缺数据：诚实 unknown，不猜
    elif all(a["above"] and a["aligned"] for a in got):
        verdict = "多头（全部站上年线+均线多头排列）"
    elif any(not a["above"] for a in got):
        verdict = "警示（有指数破年线）"
    else:
        verdict = "震荡（上年线但未全部多头排列）"
    return {"verdict": verdict, "anchors": anchors}
