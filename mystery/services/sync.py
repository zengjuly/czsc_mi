"""mystery.services.sync — 行情同步（迁自 sync_all_market.py / sync_sector_data.py）。"""
from __future__ import annotations

from typing import Optional


def sync_market(period: str = "daily", days: int = 365, force: bool = False,
                symbols: Optional[list] = None) -> dict:
    """同步行情到本地库。P1 实现。"""
    raise NotImplementedError("P1")
