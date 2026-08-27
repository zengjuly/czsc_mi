"""mystery.core.resonance — 共振/板块强度（迁自 resonance_analyzer.py）。

calculate_industry_score_from_sector 改为吃板块 K 线 DataFrame，不自己查库。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


def calculate_industry_score_from_sector(sector_kline: pd.DataFrame) -> Optional[float]:
    """板块指数 K 线 → 行业强度分（0~25，>12.5 向上）。P1 迁入。"""
    raise NotImplementedError("P1")


def industry_trend_from_kline(sector_kline: pd.DataFrame) -> Optional[bool]:
    """板块指数 K 线 → 行业趋势布尔。P1 迁入。"""
    raise NotImplementedError("P1")
