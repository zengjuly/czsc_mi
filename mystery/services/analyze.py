"""mystery.services.analyze — 唯一分析入口（P1 实现）。

对外只暴露 analyze_one_stock() → AnalysisResult。
Web / CLI / scan / 板块钻取全部走这里，保证同股同分（误差 ≤ 1）。
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from ..adapters import market, sector
from ..core.models import AnalysisResult
from ..core.mystery_rules import MysteryLogic


def chan_enabled() -> bool:
    """环境变量 MYSTERY_CHAN_ENABLED 覆盖 config（一期默认关）。"""
    v = os.environ.get("MYSTERY_CHAN_ENABLED")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "off", "")
    return False


def analyze_one_stock(symbol: str, include_detail: bool = True,
                      market_client: Optional[market.MarketDataClient] = None,
                      sector_client: Optional[sector.SectorClient] = None,
                      logic: Optional[MysteryLogic] = None) -> AnalysisResult:
    """单票完整分析（伪代码见 CLAUDE.md §7.4）。P1 实现。"""
    raise NotImplementedError("P1")
