"""mystery.adapters.market — 多源行情 + 缓存（统一出口 BarSeries）。

取数顺序：本地库未过期 → ths_official → tdx_api → tdx_local。
指数：无条件先 tdx_local，跳过换手率与“落后 3 天内”重拉。
周/月：日 K 重采样（resample_engine: mystery），一期不用两套周期口径。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..core.models import BarSeries


class MarketDataClient:
    """多源行情客户端。P1 迁入 market_data_client.py / ths / tdx 逻辑。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def fetch_bars(self, symbol: str, freq: str = "1d",
                   start: Optional[str] = None, end: Optional[str] = None) -> BarSeries:
        """统一出口：本地库未过期 → ths_official → tdx_api → tdx_local。"""
        raise NotImplementedError("P1")

    def fetch_index(self, code: str, freq: str = "1d",
                    start: Optional[str] = None, end: Optional[str] = None) -> BarSeries:
        """指数：无条件先 tdx_local。"""
        raise NotImplementedError("P1")

    def fetch_stock_list(self) -> List[Dict]:
        """[{code, name}]，ths_official 优先，本地缓存兜底。"""
        raise NotImplementedError("P1")
