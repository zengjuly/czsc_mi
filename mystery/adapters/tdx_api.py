"""mystery.adapters.tdx_api — 通达信 HTTP API（代码带交易所前缀 SH600519）。"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd


class TdxApiClient:
    """tdx-api HTTP 客户端。P1 迁入 data/tdx_api_client.py。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        raise NotImplementedError("P1")

    def get_index(self, code: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        raise NotImplementedError("P1")
