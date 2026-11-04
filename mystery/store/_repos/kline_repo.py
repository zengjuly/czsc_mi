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

# 单票/多票 upsert 共用同一条 SQL（ON CONFLICT 幂等 upsert）。
_UPSERT_KLINE_SQL = (
    "INSERT INTO stock_kline_data "
    "(code, date, period, open, high, low, close, volume, "
    "amount, turn, pctChg) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(code, date, period) DO UPDATE SET "
    "open=excluded.open, high=excluded.high, low=excluded.low, "
    "close=excluded.close, volume=excluded.volume, "
    "amount=excluded.amount, "
    "turn=COALESCE(excluded.turn, turn), "
    "pctChg=COALESCE(excluded.pctChg, pctChg)"
)

# 中文列顺序：与下方 upsert 元组位置一一对应。
_KLINE_CN = ('日期', '开盘价', '最高价', '最低价', '收盘价', '成交量', '成交额', '换手率', '涨跌幅')


def _build_kline_rows(rows: pd.DataFrame, codes, period: str) -> List[tuple]:
    """向量化构建 stock_kline_data 的 upsert 参数元组列表（替代 iterrows 逐行）。

    各列用 .values（numpy 标量）提取，与旧 iterrows 中 r.get() 返回的标量类型一致，
    因此 _f/_t/_d 的 NaN/假0 归一语义保持不变（零行为回归）。
    缺失列按 None 兜底（等价原 r.get(col) 返回 None → _f(None)=None 写 NULL）。
    codes 为长度 = 行数的序列（单票时为 [code]*n，多票时为逐行归一后的 code）。
    """
    # 缺列兜底：原 iterrows 用 r.get(col)，缺失列返回 None 而非 KeyError。
    for col in _KLINE_CN:
        if col not in rows.columns:
            rows[col] = pd.Series([None] * len(rows), index=rows.index)
    d_, o_, h_, l_, c_, v_, a_, t_, p_ = (rows[col].values for col in _KLINE_CN)
    return [
        (code, _d(dd), period,
         _f(o), _f(h), _f(l), _f(c), _f(v), _f(a),
         _t(t), _f(p))
        for code, dd, o, h, l, c, v, a, t, p in zip(
            codes, d_, o_, h_, l_, c_, v_, a_, t_, p_)
    ]


class KlineRepoMixin(BaseRepoMixin):
    """K 线行情（stock_kline_data）读写：load 升序读取、kline_last_date 新鲜度、upsert/upsert_many 向量化写入（turn 用 COALESCE 保旧值、_t 拦假0）。"""
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

            性能：单票逐行 execute 改为向量化提取 + 单条 executemany（与
            upsert_kline_many 共用 _UPSERT_KLINE_SQL / _build_kline_rows）。
            """
            rows = to_cn_columns(df) if 'date' in df.columns else df.copy()
            if max_rows and len(rows) > max_rows:
                rows = rows.tail(max_rows)
            data = _build_kline_rows(rows, [code] * len(rows), period)
            with self._lock:
                conn = self._connect()
                try:
                    if data:
                        conn.executemany(_UPSERT_KLINE_SQL, data)
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

            性能：iterrows 逐行取值改为 .values+zip 向量化（保留 _f/_t/_d 语义）。
            """
            rows = to_cn_columns(df) if 'date' in df.columns else df.copy()
            code_series = rows[code_col] if code_col in rows.columns \
                else rows['代码']
            # W46：代码统一为库内点格式（600000.SH → sh.600000，与读端
            # kline_last_date/get_kline 同格式）。旧版裸 str(code) 把 DuckDB
            # thscode 原样写库，双格式错位造成 2317 票孤儿行（sync 每天重拉
            # 重插、分析读点格式旧行停更）。非标准码宽容回退原样。
            codes = [_dbcode(c) for c in code_series.values]
            data = _build_kline_rows(rows, codes, period)
            if not data:
                return
            with self._lock:
                conn = self._connect()
                try:
                    conn.executemany(_UPSERT_KLINE_SQL, data)
                    _invalidate_analysis(conn, {d[0] for d in data})
                    if period == 'daily':
                        _prune_daily(conn, {d[0] for d in data})
                    conn.commit()
                finally:
                    conn.close()
