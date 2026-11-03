"""mystery.store._repos.kline_repo - KlineRepoMixin（按职责从 MysteryDB 抽取，公开 API 不变）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._base import BaseRepoMixin
from ._helpers import *

class KlineRepoMixin(BaseRepoMixin):
    def load_kline(self, code: str, period: str = 'daily',
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None) -> pd.DataFrame:
            """读本地缓存行情（升序，英文列：date/open/high/low/close/volume/amount/turn/pctChg…）。"""
            with self._lock:
                conn = self._connect()
                try:
                    sql = ("SELECT date, code, open, high, low, close, preclose, "
                           "volume, amount, adjustflag, turn, tradestatus, pctChg, isST "
                           "FROM stock_kline_data WHERE code=? AND period=?")
                    params: List[Any] = [code, period]
                    if start_date:
                        sql += " AND date>=?"
                        params.append(start_date)
                    if end_date:
                        sql += " AND date<=?"
                        params.append(end_date)
                    sql += " ORDER BY date ASC"
                    df = pd.read_sql_query(sql, conn, params=params)
                    return df
                finally:
                    conn.close()
    def kline_last_date(self, code: str, period: str = 'daily') -> Optional[str]:
            """本地缓存该票最新日期（MAX(date)，不整表加载——新鲜度检查专用）。"""
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT MAX(date) FROM stock_kline_data WHERE code=? AND period=?",
                        (code, period)).fetchone()
                    return str(row[0])[:10] if row and row[0] else None
                finally:
                    conn.close()
    def upsert_kline(self, df: pd.DataFrame, code: str, period: str,
                         max_rows: Optional[int] = None) -> None:
            """写行情（df 为中文列或英文列均可）。

            换手率用 COALESCE：新值为 None 时保留库内旧值（ths 数据无换手率，
            避免覆盖 baostock 同步的历史 turn —— 2026-08-27 教训）。
            W43：写入前过 `_t()` —— turn<=0 的假 0（baostock/DuckDB 缺数语义）
            归一为 None，让 COALESCE 走保旧值路径，杜绝假 0 入库再污染
            ffill 锚点与覆盖率 QA（读端 `_turn_opt` 早已把 0 当 None，写端
            不拦就是让脏数据永远躺在库里装作"有值"）。
            """
            rows = to_cn_columns(df) if 'date' in df.columns else df.copy()
            if max_rows and len(rows) > max_rows:
                rows = rows.tail(max_rows)
            with self._lock:
                conn = self._connect()
                try:
                    for _, r in rows.iterrows():
                        conn.execute(
                            "INSERT INTO stock_kline_data "
                            "(code, date, period, open, high, low, close, volume, "
                            "amount, turn, pctChg) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(code, date, period) DO UPDATE SET "
                            "open=excluded.open, high=excluded.high, low=excluded.low, "
                            "close=excluded.close, volume=excluded.volume, "
                            "amount=excluded.amount, "
                            "turn=COALESCE(excluded.turn, turn), "
                            "pctChg=COALESCE(excluded.pctChg, pctChg)",
                            (code, _d(r.get('日期')), period,
                             _f(r.get('开盘价')), _f(r.get('最高价')), _f(r.get('最低价')),
                             _f(r.get('收盘价')), _f(r.get('成交量')), _f(r.get('成交额')),
                             _t(r.get('换手率')), _f(r.get('涨跌幅'))))
                    _invalidate_analysis(conn, [code])
                    if period == 'daily':
                        _prune_daily(conn, [code])
                    conn.commit()
                finally:
                    conn.close()
    def upsert_kline_many(self, df: pd.DataFrame, period: str = 'daily',
                              code_col: str = 'thscode') -> None:
            """批量 upsert 多票行情（W12b：单大事务 executemany）。

            预同步 5200 只逐票调 upsert_kline = 5200 个事务（实测 7.9s）；
            合并成单事务 executemany 实测 0.05s。df 需含 code_col 列 +
            中文列（日期/开盘价/...）。全失败或全成功（单事务）。
            """
            rows = to_cn_columns(df) if 'date' in df.columns else df.copy()
            code_series = rows[code_col] if code_col in rows.columns \
                else rows['代码']
            # W46：代码统一为库内点格式（600000.SH → sh.600000，与读端
            # kline_last_date/get_kline 同格式）。旧版裸 str(code) 把 DuckDB
            # thscode 原样写库，双格式错位造成 2317 票孤儿行（sync 每天重拉
            # 重插、分析读点格式旧行停更）。非标准码宽容回退原样。
            data = [
                (_dbcode(code), _d(r.get('日期')), period,
                 _f(r.get('开盘价')), _f(r.get('最高价')), _f(r.get('最低价')),
                 _f(r.get('收盘价')), _f(r.get('成交量')), _f(r.get('成交额')),
                 _t(r.get('换手率')), _f(r.get('涨跌幅')))
                for code, (_, r) in zip(code_series, rows.iterrows())
            ]
            if not data:
                return
            with self._lock:
                conn = self._connect()
                try:
                    conn.executemany(
                        "INSERT INTO stock_kline_data "
                        "(code, date, period, open, high, low, close, volume, "
                        "amount, turn, pctChg) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(code, date, period) DO UPDATE SET "
                        "open=excluded.open, high=excluded.high, low=excluded.low, "
                        "close=excluded.close, volume=excluded.volume, "
                        "amount=excluded.amount, "
                        "turn=COALESCE(excluded.turn, turn), "
                        "pctChg=COALESCE(excluded.pctChg, pctChg)", data)
                    _invalidate_analysis(conn, {d[0] for d in data})
                    if period == 'daily':
                        _prune_daily(conn, {d[0] for d in data})
                    conn.commit()
                finally:
                    conn.close()
