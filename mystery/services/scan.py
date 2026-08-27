"""mystery.services.scan — 全市场扫描（只调 analyze_one_stock(include_detail=False)）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def scan_market(limit: Optional[int] = None, watchlist: Optional[List[str]] = None,
                **kwargs) -> List[Dict[str, Any]]:
    """扫描市场，返回 AnalysisResult.to_dict() 列表。P1/P3 实现。"""
    raise NotImplementedError("P1")
