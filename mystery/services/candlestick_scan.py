"""W82 service：把典型 K 线组合形态分析挂到唯一分析入口的产出上。

职责
----
- 读取网页/env 覆盖层配置（``runtime_settings``）；
- 对**已经取好的**日 K / 周 K 分别调用 ``core.candlestick_patterns`` 纯函数
  （零额外取数：周 K 由 ``AnalysisService`` 日 K 重采样得到）；
- 产出严格 JSON 可序列化的 dict，写入 ``AnalysisResult.candlestick``，
  随 ``to_dict()`` 进入日报 Excel/HTML、扫描结果 payload、Web 个股页。

不做的事
--------
- 不改评分 / 信号 / ``RULE_VER``；形态结论**仅展示**；
- 不新建分析入口、不在报告生成器里取数（生成器只读 payload）；
- core 零 IO：core 只接受注入的 Bar 列表与稀疏配置。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from mystery.core.candlestick_patterns import detect_combo_patterns
from mystery.services import runtime_settings as rts

logger = logging.getLogger(__name__)


def _empty_period(period: str, reason: str) -> Dict[str, Any]:
    return {"period": period, "lookback": 0, "n_bars": 0, "enabled": [],
            "n_hits": 0, "hits": [], "summary": "—", "reason": reason}


def analyze_candlestick(daily_bars=None, weekly_bars=None,
                        options: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
    """日 K / 周 K 独立检测，返回可序列化结果 dict。

    :param daily_bars: BarSeries 或 List[Bar]（日 K）
    :param weekly_bars: BarSeries 或 List[Bar]（周 K，由日 K 重采样）
    :param options: 归一化配置；None 时从覆盖层/env/默认读取
    """
    try:
        opts = options or rts.candle_options_normalized()
    except Exception as exc:  # noqa: BLE001 - 配置读失败不阻断分析
        logger.warning("[candlestick] 配置读取失败，回退默认: %s", exc)
        opts = {"enabled": True, "daily": True, "weekly": True,
                "lookback": 60, "patterns": []}

    base: Dict[str, Any] = {
        "enabled": bool(opts.get("enabled")),
        "config": {
            "daily": bool(opts.get("daily", True)),
            "weekly": bool(opts.get("weekly", True)),
            "lookback": int(opts.get("lookback", 60)),
            "patterns": list(opts.get("patterns") or []),
        },
        "daily": {}, "weekly": {},
        "summary_daily": "—", "summary_weekly": "—",
        "direction": "—", "n_hits": 0,
    }
    if not base["enabled"]:
        base["daily"] = _empty_period("daily", "总开关关闭")
        base["weekly"] = _empty_period("weekly", "总开关关闭")
        return base

    lb = int(opts.get("lookback", 60))
    patterns = list(opts.get("patterns") or [])

    def _one(bars, period: str, on: bool) -> Dict[str, Any]:
        if not on:
            return _empty_period(period, "该周期未启用")
        if bars is None:
            return _empty_period(period, "无 K 线数据")
        try:
            return detect_combo_patterns(
                bars, period=period, enabled=patterns, lookback=lb)
        except Exception as exc:  # noqa: BLE001 - 单周期失败不影响主分析
            logger.warning("[candlestick] %s 检测失败: %s", period, exc)
            return _empty_period(period, f"检测异常: {str(exc)[:60]}")

    base["daily"] = _one(daily_bars, "daily", bool(opts.get("daily", True)))
    base["weekly"] = _one(weekly_bars, "weekly", bool(opts.get("weekly", True)))
    base["summary_daily"] = base["daily"].get("summary") or "—"
    base["summary_weekly"] = base["weekly"].get("summary") or "—"
    base["n_hits"] = int(base["daily"].get("n_hits", 0)) + \
        int(base["weekly"].get("n_hits", 0))
    # 最近方向：优先日 K 最新命中，其次周 K 最新命中（仅供报表一列速览）
    for part in (base["daily"], base["weekly"]):
        hits = part.get("hits") or []
        if hits:
            base["direction"] = hits[0].get("direction") or "—"
            break
    return base


__all__ = ["analyze_candlestick"]
