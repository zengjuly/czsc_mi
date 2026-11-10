"""W80/W82：日报汇总字段的单一映射（Excel / HTML 共用）。

全部只读 ``AnalysisResult.to_dict()`` / scan payload 的**顶层字段**，
不取数、不重算、不改判——与 ``core.scan_signals.classify`` 落库时的口径一致：
  - ``turnover_20``：近 20 日均换手率(%)，仓位/chip_low 判据；
  - ``chip_low`` / ``chip_low_unknown`` / ``chip_quiet``：筹码标签三态；
  - 无换手数据 → chip_low_unknown=True（禁止把 unknown 当 True）。

放在 reports 层（而非 core）以保证 core 零改动、零 IO 约束不变。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def turnover_20_value(d: Dict[str, Any]) -> Optional[float]:
    """近 20 日均换手率(%)，缺失/非法 → None（不伪造 0）。"""
    v = d.get("turnover_20")
    if v is None or v == "":
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def chip_low_text(d: Dict[str, Any]) -> str:
    """筹码低位判定：✅ / ❌ / 换手缺失（三态，不把 unknown 当 True）。"""
    if d.get("chip_low_unknown"):
        return "换手缺失"
    return "✅" if d.get("chip_low") else "❌"


def chip_tag_text(d: Dict[str, Any]) -> str:
    """筹码相关标签（综合三态，供日报对账）。"""
    if d.get("chip_low_unknown"):
        return "换手缺失"
    if d.get("chip_low"):
        return "低位缩量"
    if d.get("chip_quiet"):
        return "高位缩量"
    return "—"


def turnover_source_text(d: Dict[str, Any]) -> str:
    """换手来源标记（W80 顺带）：有换手数值→bar；全 0/缺失→待补。

    仅作展示提示，不判定数据真伪；区分「真实低换手」与「换手数据缺失」，
    避免全 0 被当成真实缩量（关联 W81 数据质量告警）。
    """
    v = turnover_20_value(d)
    if v is None:
        return "缺失"
    return "低换手" if v < 2 else "bar"


# ---------------------------------------------------------------------------
# W82：典型 K 线组合形态（全部只读 payload 顶层 candlestick 字段，不重算）
# ---------------------------------------------------------------------------
def candlestick_part(d: Dict[str, Any], period: str) -> Dict[str, Any]:
    """取日/周某一周期的分析结果（旧结果无字段 → 空 dict）。"""
    cs = d.get("candlestick") or {}
    part = cs.get(period) or {}
    return part if isinstance(part, dict) else {}


def candlestick_daily_text(d: Dict[str, Any]) -> str:
    """日 K 形态摘要；旧任务/未启用 → '—'。"""
    cs = d.get("candlestick") or {}
    if not cs:
        return "—"
    return str(cs.get("summary_daily") or
               candlestick_part(d, "daily").get("summary") or "—")


def candlestick_weekly_text(d: Dict[str, Any]) -> str:
    """周 K 形态摘要；旧任务/未启用 → '—'。"""
    cs = d.get("candlestick") or {}
    if not cs:
        return "—"
    return str(cs.get("summary_weekly") or
               candlestick_part(d, "weekly").get("summary") or "—")


def candlestick_direction_text(d: Dict[str, Any]) -> str:
    """最近形态方向（看涨/看跌/—），仅供速览，不进评分。"""
    cs = d.get("candlestick") or {}
    return str(cs.get("direction") or "—")


def candlestick_hits(d: Dict[str, Any], period: str,
                     limit: int = 5) -> list:
    """某周期命中明细列表（Web 个股页用；最多 limit 条）。"""
    hits = candlestick_part(d, period).get("hits") or []
    return [h for h in hits if isinstance(h, dict)][:max(0, int(limit))]
