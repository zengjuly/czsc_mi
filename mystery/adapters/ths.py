"""mystery.adapters.ths — 同花顺扶摇数据源（子进程，列标准化）。

P1 迁入 data/ths_client.py 的 fuyao 子进程行为；API key 只走环境变量/配置注入。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd


class ThsClient:
    """扶摇客户端。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        """日 K（列：日期/开盘/最高/最低/收盘/成交量…）。"""
        raise NotImplementedError("P1")

    def get_stock_list(self) -> List[Dict]:
        """[{code, name}]。"""
        raise NotImplementedError("P1")

    def get_financial(self, symbol: str) -> Dict:
        """财务摘要 {pe, pb, roe, dividend...}。"""
        raise NotImplementedError("P1")
