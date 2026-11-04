"""mystery.store._repos.sector_repo - SectorRepoMixin（按职责从 MysteryDB 抽取，公开 API 不变）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._base import BaseRepoMixin
from ._helpers import *

class SectorRepoMixin(BaseRepoMixin):
    def get_sector_kline(self, sector_code: str) -> Optional[pd.DataFrame]:
            """板块指数 K 线（升序，中文列：收盘价/成交额…）。自动归一 ths_ 前缀。"""
            code = str(sector_code)
            if not code.startswith('ths_'):
                code = f'ths_{code.split(".")[0]}'
            with self._lock:
                conn = self._connect()
                try:
                    df = pd.read_sql_query(
                        "SELECT trade_date, sector_code, open, high, low, close, "
                        "volume, amount FROM sector_kline WHERE sector_code=? "
                        "ORDER BY trade_date ASC", conn, params=(code,))
                    if df is None or df.empty:
                        return None
                    df = df.rename(columns={'trade_date': '日期', 'open': '开盘价',
                                            'high': '最高价', 'low': '最低价',
                                            'close': '收盘价', 'volume': '成交量',
                                            'amount': '成交额'})
                    return df
                finally:
                    conn.close()
    def get_sector_meta(self, active_only: bool = True) -> list:
            """板块元数据 [(sector_code, sector_name, parent_type), ...]。"""
            with self._lock:
                conn = self._connect()
                try:
                    sql = "SELECT sector_code, sector_name, parent_type FROM sector_meta"
                    if active_only:
                        sql += " WHERE is_active=1"
                    return conn.execute(sql).fetchall()
                finally:
                    conn.close()
    def get_sector_stocks(self, sector_code: str) -> List[str]:
            """板块成分股代码（sh600519 格式），优先 rel 表，constituents 兜底。

            rel 表 sector_code 存「881101.TI」格式（无 ths_ 前缀）；入参
            ths_881101 / 881101.TI / 881101 统一归一为 881101.TI 再查。
            """
            code = str(sector_code).strip()
            if code.startswith('ths_'):
                code = code[4:]
            if '.' not in code:
                code = f'{code}.TI'
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT stock_code FROM stock_sector_rel WHERE sector_code=? "
                        "ORDER BY is_primary DESC", (code,)).fetchall()
                    codes = [r[0] for r in rows]
                    if not codes:
                        rows = conn.execute(
                            "SELECT stock_code FROM sector_constituents "
                            "WHERE sector_code=?", (code,)).fetchall()
                        codes = [r[0] for r in rows]
                    return [c.replace('.', '') for c in codes]
                finally:
                    conn.close()
    @staticmethod
    def _norm_sector_ti(sector_code: str) -> str:
            """ths_886015 / 886015 / 886015.TI → 886015.TI（rel/constituents 存储格式）。"""
            code = str(sector_code).strip()
            if code.startswith('ths_'):
                code = code[4:]
            if '.' not in code:
                code = f'{code}.TI'
            return code
    def upsert_stock_sector_rel(self, stock_code: str, sector_code: str,
                                    is_primary: int = 0) -> None:
            """写股票-板块关系（stock 归一 sh.600519，sector 归一 886015.TI）。

            行业板块（881/884）is_primary=1（主行业语义，与存量回填一致）；
            概念板块（885/886）is_primary=0，不影响 get_industry 主行业查询。
            """
            from mystery.adapters.codes import db_code_of
            stock_db = db_code_of(stock_code)
            sector_ti = self._norm_sector_ti(sector_code)
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT INTO stock_sector_rel (stock_code, sector_code, is_primary) "
                        "VALUES (?,?,?) "
                        "ON CONFLICT(stock_code, sector_code) "
                        "DO UPDATE SET is_primary=excluded.is_primary",
                        (stock_db, sector_ti, int(is_primary)))
                    conn.commit()
                finally:
                    conn.close()
    def upsert_sector_kline(self, df: pd.DataFrame, sector_code: str,
                                sector_name: str = '') -> int:
            """写板块指数日K（df 中文列或英文列均可，UPSERT 幂等）。返回写入行数。

            W39：sector_kline 只有读路径没有写路径导致停更近一月——同步统一走
            sync_sector_kline（fuyao index-historical 归一通道），本方法为其唯一写入口。

            性能：iterrows 逐行构建参数元组改为 .values+zip 向量化（保留
            float()/int(v) if pd.notna(v) else 0 / str(dd)[:10] 语义；缺失的
            volume/amount 列兜底为 0，与原 r.get() 容错等价）。
            """
            if df is None or df.empty:
                return 0
            code = str(sector_code)
            if not code.startswith('ths_'):
                code = f'ths_{code.split(".")[0]}'
            d = df.copy()
            ren = {v: k for k, v in _CN_COLS.items() if v in d.columns}
            d = d.rename(columns=ren)
            need = {'date', 'open', 'high', 'low', 'close'}
            if not need.issubset(d.columns):
                raise ValueError(f"sector_kline 缺列: {sorted(need - set(d.columns))}")
            # 缺失 volume/amount 列时兜底 0（与原 r.get() 返回 None → 0 一致）。
            vols = d['volume'] if 'volume' in d.columns else pd.Series(0, index=d.index)
            amts = d['amount'] if 'amount' in d.columns else pd.Series(0.0, index=d.index)
            rows = [
                (code, sector_name or code,
                 str(dd)[:10],
                 float(o), float(h), float(l), float(c),
                 int(v) if pd.notna(v) else 0,
                 float(a) if pd.notna(a) else 0.0)
                for dd, o, h, l, c, v, a in zip(
                    d['date'].values, d['open'].values, d['high'].values,
                    d['low'].values, d['close'].values,
                    vols.values, amts.values)
            ]
            with self._lock:
                conn = self._connect()
                try:
                    conn.executemany(
                        "INSERT INTO sector_kline (sector_code, sector_name, "
                        "trade_date, open, high, low, close, volume, amount, "
                        "source_type) VALUES (?,?,?,?,?,?,?,?,?,'ths') "
                        "ON CONFLICT(sector_code, trade_date) DO UPDATE SET "
                        "open=excluded.open, high=excluded.high, "
                        "low=excluded.low, close=excluded.close, "
                        "volume=excluded.volume, amount=excluded.amount, "
                        "update_time=CURRENT_TIMESTAMP", rows)
                    conn.commit()
                finally:
                    conn.close()
            return len(rows)
    def clear_sector_kline(self) -> int:
            """清空 sector_kline 全表（W39 date_ms 错位重建专用，配合
            sync_sector_kline(full_since=...) 使用）。返回删除行数。"""
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute("DELETE FROM sector_kline")
                    conn.commit()
                    return cur.rowcount
                finally:
                    conn.close()
    def get_sector_kline_dates(self) -> Dict[str, str]:
            """sector_code → 库内最新 trade_date（W39 增量同步断点用）。"""
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT sector_code, MAX(trade_date) FROM sector_kline "
                        "GROUP BY sector_code").fetchall()
                finally:
                    conn.close()
            return {r[0]: r[1] for r in rows if r[1]}
    def get_sector_kline_names(self) -> Dict[str, str]:
            """sector_code → 任一存量 sector_name（W39 同步保留板块名，不覆盖）。"""
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT sector_code, MIN(sector_name) FROM sector_kline "
                        "GROUP BY sector_code").fetchall()
                finally:
                    conn.close()
            return {r[0]: r[1] for r in rows if r[1]}
    def get_sector_kline_sectors(self) -> List[str]:
            """sector_kline 现有全部板块代码（升序，W39 同步清单）。"""
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT DISTINCT sector_code FROM sector_kline "
                        "ORDER BY sector_code").fetchall()
                finally:
                    conn.close()
            return [r[0] for r in rows]
    def ensure_sector_meta(self, sector_code: str) -> None:
            """板块元数据存在则只刷新 last_sync_date；不存在才插入占位行。

            禁止用代码覆盖已有 sector_name（如 创新药）。
            """
            code = str(sector_code).strip()
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT INTO sector_meta (sector_code, sector_name, "
                        "parent_type, base_code, is_active, last_sync_date) "
                        "VALUES (?, ?, '行业/概念', ?, 1, date('now','localtime')) "
                        "ON CONFLICT(sector_code) DO UPDATE SET "
                        "is_active=1, last_sync_date=date('now','localtime')",
                        (code, code, code.replace('ths_', '')[:6]))
                    conn.commit()
                finally:
                    conn.close()
