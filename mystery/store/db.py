"""mystery.store.db — SQLite 数据中枢（schema 复用现网库）。

默认库：config.db_path（环境变量 MYSTERY_DB_PATH 可覆盖），
兜底 ./data/mystery_cache.db。P1 迁入 db_manager.py / data_engine.py 逻辑。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

_DEFAULT_DB = os.environ.get(
    "MYSTERY_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "data", "mystery_cache.db"),
)


class MysteryDB:
    """本地库客户端。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DEFAULT_DB

    def connect(self):
        raise NotImplementedError("P1")

    # ---- 行情 ----
    def get_kline(self, symbol: str, freq: str = "1d") -> Optional[Any]:
        raise NotImplementedError("P1")

    def upsert_kline(self, symbol: str, freq: str, df) -> None:
        raise NotImplementedError("P1")

    # ---- 股票/板块 ----
    def get_stock_list(self) -> List[Dict]:
        raise NotImplementedError("P1")

    def get_primary_industry(self, symbol: str) -> Optional[tuple]:
        raise NotImplementedError("P1")

    def get_sector_kline(self, sector_code: str, freq: str = "1d") -> Optional[Any]:
        raise NotImplementedError("P1")

    # ---- 缠论缓存 ----
    def get_chan_cache(self, symbol: str, freq: str, trade_date: str, czsc_ver: str) -> Optional[str]:
        raise NotImplementedError("P1")

    def set_chan_cache(self, symbol: str, freq: str, trade_date: str,
                       czsc_ver: str, payload_json: str) -> None:
        raise NotImplementedError("P1")
