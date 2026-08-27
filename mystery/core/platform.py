"""mystery.core.platform — 震荡平台 / 自适应 VAP-ATR 平台（迁自 adaptive_platform.py）。

入参 DataFrame / BarSeries，纯函数，零 IO。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


def analyze_adaptive_platform(daily: pd.DataFrame, stock_code: str = "",
                              latest_only: bool = True, **kwargs) -> Dict[str, Any]:
    """自适应 VAP-ATR 平台（gemmi 优化）。P1 迁入。"""
    raise NotImplementedError("P1")


def platform_breakthrough(daily: pd.DataFrame, **kwargs) -> Dict[str, Any]:
    """平台突破判定（供 MysteryLogic.platform_breakthrough_analysis 复用）。P1 迁入。"""
    raise NotImplementedError("P1")
