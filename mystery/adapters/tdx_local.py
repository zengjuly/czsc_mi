"""mystery.adapters.tdx_local — 通达信本地数据（vipdoc + 增量 + 复权因子）。

指数豁免换手率/落后检查。
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd


class TdxLocalClient:
    """通达信本地客户端。P1 迁入 data/tdx_local_client.py + incremental/gbbq。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None, is_index: bool = False) -> Optional[pd.DataFrame]:
        raise NotImplementedError("P1")

    def get_weekly(self, symbol: str, start: Optional[str] = None,
                   end: Optional[str] = None) -> Optional[pd.DataFrame]:
        raise NotImplementedError("P1")

    def get_index(self, code: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        raise NotImplementedError("P1")
