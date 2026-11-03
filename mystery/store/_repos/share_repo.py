"""mystery.store._repos.share_repo - ShareRepoMixin（按职责从 MysteryDB 抽取，公开 API 不变）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._base import BaseRepoMixin
from ._helpers import *

class ShareRepoMixin(BaseRepoMixin):
    def upsert_float_share(self, thscode: str, as_of: str,
                               float_market_cap: Optional[float],
                               last_price: Optional[float],
                               float_shares: Optional[float],
                               source: str = 'auction_final') -> None:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO float_share_snapshot "
                        "(thscode, as_of, float_market_cap, last_price, "
                        " float_shares, source) VALUES (?,?,?,?,?,?)",
                        (thscode, as_of, _f(float_market_cap), _f(last_price),
                         _f(float_shares), source))
                    conn.commit()
                finally:
                    conn.close()
    def upsert_float_share_many(self, rows) -> int:
            """批量 upsert 股本快照（单事务 executemany，消除逐行 commit 往返开销）。

            rows: 可迭代对象，每元素为元组
                (thscode, as_of, float_market_cap, last_price, float_shares[, source])；
            source 缺省 'auction_final'。返回写入行数。
            """
            rows = list(rows)
            if not rows:
                return 0
            norm = [(r[0], r[1], _f(r[2]), _f(r[3]), _f(r[4]),
                     (r[5] if len(r) > 5 else 'auction_final')) for r in rows]
            with self._lock:
                conn = self._connect()
                try:
                    conn.executemany(
                        "INSERT OR REPLACE INTO float_share_snapshot "
                        "(thscode, as_of, float_market_cap, last_price, "
                        " float_shares, source) VALUES (?,?,?,?,?,?)",
                        norm)
                    conn.commit()
                finally:
                    conn.close()
            return len(norm)
    def get_float_share(self, thscode: str,
                            as_of_max: Optional[str] = None) -> Optional[Dict]:
            """取 as_of <= as_of_max 的最新股本快照（低频快照，事件才更新）。"""
            with self._lock:
                conn = self._connect()
                try:
                    if as_of_max:
                        row = conn.execute(
                            "SELECT as_of, float_shares, float_market_cap, last_price "
                            "FROM float_share_snapshot "
                            "WHERE thscode=? AND as_of<=? "
                            "ORDER BY as_of DESC LIMIT 1",
                            (thscode, as_of_max)).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT as_of, float_shares, float_market_cap, last_price "
                            "FROM float_share_snapshot "
                            "WHERE thscode=? "
                            "ORDER BY as_of DESC LIMIT 1",
                            (thscode,)).fetchone()
                    if not row:
                        return None
                    return {'as_of': row[0], 'float_shares': row[1],
                            'float_market_cap': row[2], 'last_price': row[3]}
                finally:
                    conn.close()
