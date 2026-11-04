"""mystery.store._repos.scan_repo - ScanRepoMixin（按职责从 MysteryDB 抽取，公开 API 不变）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._base import BaseRepoMixin
from ._helpers import *

class ScanRepoMixin(BaseRepoMixin):
    """扫描相关 K 线读取：空 turn 缺口行捕获（null_turn_rows / between）、set_turn 仅填空不覆盖、turnover_coverage 覆盖率 QA。"""
    def null_turn_rows(self, trade_date: str,
                           codes: Optional[List[str]] = None) -> List[Dict]:
            """指定交易日 turn IS NULL 且 volume 有效的日K行 [{code,date,volume}]。"""
            sql = ("SELECT code, substr(date,1,10) d, volume FROM stock_kline_data "
                   "WHERE period='daily' AND substr(date,1,10)=? "
                   "AND turn IS NULL AND volume > 0")
            args: List = [trade_date]
            if codes:
                sql += f" AND code IN ({','.join('?' * len(codes))})"
                args += list(codes)
            with self._lock:
                conn = self._connect()
                try:
                    return [{'code': r[0], 'date': r[1], 'volume': r[2]}
                            for r in conn.execute(sql, args).fetchall()]
                finally:
                    conn.close()
    def null_turn_rows_between(self, start_date: str, end_date: str,
                                   codes: Optional[List[str]] = None) -> List[Dict]:
            """[start_date, end_date) 区间 turn IS NULL 且 volume 有效的日K行。

            W26b 策略 B 回填用；右开（不含 end_date，当日由主路径处理，不重复计）。
            """
            sql = ("SELECT code, substr(date,1,10) d, volume FROM stock_kline_data "
                   "WHERE period='daily' AND substr(date,1,10)>=? "
                   "AND substr(date,1,10)<? "
                   "AND turn IS NULL AND volume > 0 ")
            args: List = [start_date, end_date]
            if codes:
                sql += f"AND code IN ({','.join('?' * len(codes))}) "
                args += list(codes)
            sql += "ORDER BY code, date"
            with self._lock:
                conn = self._connect()
                try:
                    return [{'code': r[0], 'date': r[1], 'volume': r[2]}
                            for r in conn.execute(sql, args).fetchall()]
                finally:
                    conn.close()
    def set_turn(self, code: str, date: str, turn: float,
                     source: str) -> None:
            """仅当该行 turn 仍为空时写入（legacy/official 绝不覆盖）。"""
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "UPDATE stock_kline_data SET turn=?, turn_source=? "
                        "WHERE code=? AND period='daily' AND substr(date,1,10)=? "
                        "AND turn IS NULL",
                        (turn, source, code, date))
                    conn.commit()
                finally:
                    conn.close()
    def turnover_coverage(self, days: int = 20,
                              codes: Optional[List[str]] = None) -> Dict:
            """近 N 个自然交易日的 turn 覆盖率 QA（按最新日期回退窗口）。"""
            with self._lock:
                conn = self._connect()
                try:
                    where, args = "period='daily'", []
                    if codes:
                        where += f" AND code IN ({','.join('?' * len(codes))})"
                        args = list(codes)
                    # 以库内最新日期为锚回退 30 天窗口（覆盖 ~20 交易日）
                    anchor = conn.execute(
                        f"SELECT MAX(substr(date,1,10)) FROM stock_kline_data "
                        f"WHERE {where}", args).fetchone()[0]
                    if not anchor:
                        return {'anchor': None, 'rows': 0, 'with_turn': 0,
                                'coverage': None, 'by_source': {}}
                    from datetime import datetime, timedelta
                    start = (datetime.strptime(anchor, '%Y-%m-%d')
                             - timedelta(days=int(days * 1.7) + 3)).strftime('%Y-%m-%d')
                    rows = conn.execute(
                        f"SELECT COALESCE(turn_source,'null'), COUNT(*), "
                        f"SUM(CASE WHEN turn > 0 AND turn < 80 THEN 1 ELSE 0 END) "
                        f"FROM stock_kline_data WHERE {where} "
                        f"AND substr(date,1,10)>=? AND substr(date,1,10)<=? "
                        f"GROUP BY 1", args + [start, anchor]).fetchall()
                    total = sum(r[1] for r in rows)
                    # 覆盖以 turn 有效值为准（W43：假 0/越界不算覆盖；
                    # 旧口径 `turn IS NOT NULL` 被历史假 0 灌成虚高覆盖率）
                    with_turn = sum(r[2] for r in rows)
                    return {'anchor': anchor, 'window_start': start,
                            'rows': total, 'with_turn': with_turn,
                            'coverage': round(with_turn / total, 4) if total else None,
                            'by_source': {r[0]: r[1] for r in rows}}
                finally:
                    conn.close()
