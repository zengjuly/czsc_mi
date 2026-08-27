"""mystery.adapters.sector — 板块数据（指数 K 线 / 行业归属）。

板块强度禁止成分股抽样；只用 sector_kline 真实指数。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd


class SectorClient:
    """板块客户端。P1 迁入 sync_sector_data / sync_sector_meta / 主行业关系。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def get_industry(self, symbol: str) -> Dict:
        """返回 {code, name, score, up}：主行业 + 强度分 + 趋势。"""
        raise NotImplementedError("P1")

    def get_sector_kline(self, sector_code: str, freq: str = "1d") -> pd.DataFrame:
        """板块指数 K 线（ths_ 前缀格式）。"""
        raise NotImplementedError("P1")
