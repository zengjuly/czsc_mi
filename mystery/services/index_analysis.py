"""W84：各大指数分析（只读展示，不进评分、不改 rule_ver）。

提供：
- ``MAJOR_INDEXES``：需要纳入日报 / 全量后台分析的各大宽基指数清单。
- ``analyze_index`` / ``analyze_major_indexes``：对单只 / 全部宽基指数做纯价格
  研判（年线位置、MA 多头排列、近期涨跌幅、日K/周K 典型组合形态），返回
  可 JSON 序列化的 dict 列表，供日报 Excel/HTML、全量后台扫描报告与 Web 复用。
- ``index_summary_line``：一行文字摘要（头部 QA / 飞书正文共用）。

设计约束（与 W82/W83 一致）：
- 零 IO：所有取数通过传入的 ``MarketDataClient`` 完成，本模块不直接碰库 / 网络。
- 纯价格：指数无换手 / 行业 / 财务，只做价格面研判。
- 缺数据 → ``has_data=False``，结论字段置 ``None``，绝不抛异常。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from mystery.core import candlestick_patterns as _cp

# 各大宽基指数（名称, 内部代码）；内部代码走既有 normalize_symbol 归一通道。
MAJOR_INDEXES: Tuple[Tuple[str, str], ...] = (
    ("上证指数", "000001.SH"),
    ("深证成指", "399001.SZ"),
    ("创业板指", "399006.SZ"),
    ("沪深300", "000300.SH"),
    ("上证50", "000016.SH"),
    ("中证500", "000905.SH"),
    ("科创50", "000688.SH"),
    ("中证1000", "000852.SH"),
)


def _closes(bars) -> List[float]:
    """取 BarSeries 收盘价序列（升序）。"""
    return [float(b.close) for b in (bars.bars if bars else [])]


def _sma_last(closes: List[float], n: int) -> Optional[float]:
    """最近 n 根收盘价的简单移动平均；不足 n 根返回 None。"""
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def _year_line_state(closes: List[float]) -> Dict[str, Any]:
    """年线（250 日）位置研判。

    返回 {above(bool|None), dist_pct(float|None)}：
    - above：最新收盘 > 年线 → True；否则 False；不足 250 根 → None。
    - dist_pct：(close - 年线) / 年线 × 100。
    """
    ma250 = _sma_last(closes, 250)
    if ma250 is None or ma250 == 0:
        return {"above": None, "dist_pct": None}
    close = closes[-1]
    return {"above": close > ma250,
            "dist_pct": round((close - ma250) / ma250 * 100, 2)}


def _ma_aligned(closes: List[float]) -> Optional[bool]:
    """MA 多头排列：MA5 ≥ MA10 ≥ MA20 ≥ MA60（短到长依次不降）。

    不足 60 根返回 None（无法判定）。
    """
    ma5 = _sma_last(closes, 5)
    ma10 = _sma_last(closes, 10)
    ma20 = _sma_last(closes, 20)
    ma60 = _sma_last(closes, 60)
    if None in (ma5, ma10, ma20, ma60):
        return None
    return ma5 >= ma10 >= ma20 >= ma60


def _pct_change(closes: List[float], n: int) -> Optional[float]:
    """最近 n 日涨跌幅(%)：需 n+1 根；不足返回 None。"""
    if len(closes) < n + 1:
        return None
    base = closes[-(n + 1)]
    if base == 0:
        return None
    return round((closes[-1] - base) / base * 100, 2)


def analyze_index(market, name: str, code: str) -> Dict[str, Any]:
    """单只宽基指数研判（纯价格，只读展示）。

    :param market: ``MarketDataClient`` 实例（负责取数 + 会话级缓存 + 落库）。
    :returns: dict，含 name/code/date/close/chg_5d/chg_20d/above_year/
              year_dist_pct/ma_aligned/candle_daily/candle_weekly/has_data。
              任何取数失败都降级为 has_data=False，不抛。
    """
    base = {"name": name, "code": code, "date": "", "close": None,
            "chg_5d": None, "chg_20d": None, "above_year": None,
            "year_dist_pct": None, "ma_aligned": None,
            "candle_daily": "—", "candle_weekly": "—", "has_data": False}
    try:
        daily = market.fetch_index(code, "1d")
        if not daily or not daily.bars:
            return base
        closes = _closes(daily)
        if len(closes) < 6:
            return base
        weekly = market.fetch_index(code, "1w")
        last = daily.bars[-1]
        base.update({
            "date": str(last.dt)[:10],
            "close": round(float(last.close), 2),
            "chg_5d": _pct_change(closes, 5),
            "chg_20d": _pct_change(closes, 20),
            "above_year": _year_line_state(closes)["above"],
            "year_dist_pct": _year_line_state(closes)["dist_pct"],
            "ma_aligned": _ma_aligned(closes),
            "candle_daily": (_cp.detect_combo_patterns(daily, period="daily",
                                lookback=120).get("summary") or "—"),
            "candle_weekly": (_cp.detect_combo_patterns(weekly, period="weekly",
                                  lookback=120).get("summary") or "—"
                              if weekly and weekly.bars else "—"),
            "has_data": True,
        })
    except Exception:  # noqa: BLE001 指数研判失败不阻塞主流程
        pass
    return base


def analyze_major_indexes(market) -> List[Dict[str, Any]]:
    """全部 MAJOR_INDEXES 研判；无数据的指数 has_data=False 仍保留在列表。"""
    return [analyze_index(market, n, c) for n, c in MAJOR_INDEXES]


def _year_label(d: Dict[str, Any]) -> str:
    if d.get("above_year") is None:
        return "未知"
    tag = "站上年线" if d["above_year"] else "跌破年线"
    dist = d.get("year_dist_pct")
    return f"{tag}({dist:+.1f}%)" if dist is not None else tag


def _ma_label(d: Dict[str, Any]) -> str:
    a = d.get("ma_aligned")
    if a is None:
        return "未知"
    return "多头排列" if a else "非多头"


def index_summary_line(idxs: List[Dict[str, Any]]) -> str:
    """一行文字摘要：各大指数 收盘(年线位置/MA排列,20日%)。缺数据跳过。"""
    parts = []
    for d in idxs:
        if not d.get("has_data"):
            continue
        chg = d.get("chg_20d")
        chg_s = f"{chg:+.1f}%" if chg is not None else "—"
        parts.append(f"{d['name']}{d.get('close')}({_year_label(d)}/"
                     f"{_ma_label(d)},20日{chg_s})")
    return "[各大指数] " + "；".join(parts) if parts else "[各大指数] 暂无数据"


__all__ = ["MAJOR_INDEXES", "analyze_index", "analyze_major_indexes",
           "index_summary_line"]
