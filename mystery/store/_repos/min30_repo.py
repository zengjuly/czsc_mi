"""mystery.store._repos.min30_repo — 分钟K线与MACD状态存储（W103）。

独立精简表，不碰 1.79GB 的日K主表：
- stock_kline_min30(code, period, dt, open, high, low, close, volume, amount)
    period: '30m' | '60m'；主键(code,period,dt)，单索引(period,dt)。
    30m 保留20天（160根，够30min MACD）；60m 保留40天（160根，够60min MACD）。
- macd_state(code, freq, state, cross, dif, dea, hist, ts)
    每票每周期一行，记录上次 MACD 状态，用于金叉/死叉变更差分。

60min 直接存 tdx-web 的 hour 数据（不再从30min聚合——实测20天30min聚合后
仅56根，不足MACD稳定所需~80根；直接存60min保留40天有160根，充足且检测零HTTP）。

代码格式统一 600519.SH；dt 为 'YYYY-MM-DD HH:MM'。价格存实时值（已从
tdx 放大1000倍还原）。线程安全沿用项目 repo 的 _lock + 每操作新连接模式。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.environ.get(
    "MYSTERY_DB_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "data", "mystery_cache.db"),
)
MIN30_KEEP_DAYS = int(os.environ.get("MYSTERY_MIN30_KEEP_DAYS", "20"))
MIN60_KEEP_DAYS = int(os.environ.get("MYSTERY_MIN60_KEEP_DAYS", "40"))


class Min30Repo:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DEFAULT_DB
        self._lock = threading.RLock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(stock_kline_min30)").fetchall()]
                if cols and "period" not in cols:
                    # 旧版无 period 列（本次 W103 开发中间态）→ 重建
                    logger.warning("[min30] 检测到旧表结构，重建 stock_kline_min30")
                    conn.execute("DROP TABLE IF EXISTS stock_kline_min30")
                    cols = []
                if not cols:
                    conn.execute("""
                        CREATE TABLE stock_kline_min30 (
                            code   TEXT NOT NULL,
                            period TEXT NOT NULL,
                            dt     TEXT NOT NULL,
                            open   REAL,
                            high   REAL,
                            low    REAL,
                            close  REAL,
                            volume REAL,
                            amount REAL,
                            PRIMARY KEY (code, period, dt)
                        )
                    """)
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_min30_period_dt "
                        "ON stock_kline_min30(period, dt)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS macd_state (
                        code   TEXT NOT NULL,
                        freq   TEXT NOT NULL,
                        state  TEXT,
                        cross  TEXT,
                        dif    REAL,
                        dea    REAL,
                        hist   REAL,
                        ts     TEXT,
                        PRIMARY KEY (code, freq)
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    def upsert_bars(self, code: str, bars: List[Dict],
                    period: str = "30m") -> int:
        """写入某票分钟K线（增量，主键去重）。period: 30m/60m。"""
        if not bars:
            return 0
        rows = [
            (code, period, str(b["dt"]),
             b.get("open"), b.get("high"), b.get("low"), b.get("close"),
             b.get("volume"), b.get("amount"))
            for b in bars
        ]
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.executemany(
                    "INSERT OR REPLACE INTO stock_kline_min30 "
                    "(code,period,dt,open,high,low,close,volume,amount) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", rows)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def load_bars(self, code: str, period: str = "30m") -> List[Dict]:
        """读取某票分钟K线（升序），供 MACD 计算。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT dt,open,high,low,close,volume,amount "
                    "FROM stock_kline_min30 WHERE code=? AND period=? ORDER BY dt",
                    (code, period)).fetchall()
            finally:
                conn.close()
        return [
            {"dt": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "amount": r[6]}
            for r in rows
        ]

    def last_dt(self, code: str, period: str = "30m") -> Optional[str]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT MAX(dt) FROM stock_kline_min30 "
                    "WHERE code=? AND period=?", (code, period)).fetchone()
                return row[0] if row and row[0] else None
            finally:
                conn.close()

    def prune(self, keep_days: Optional[int] = None,
              period: str = "30m") -> int:
        """滚动裁剪：删除该周期早于保留期的最旧行。"""
        days = keep_days if keep_days is not None else (
            MIN60_KEEP_DAYS if period == "60m" else MIN30_KEEP_DAYS)
        if days <= 0:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                latest = conn.execute(
                    "SELECT MAX(dt) FROM stock_kline_min30 WHERE period=?",
                    (period,)).fetchone()[0]
                if not latest:
                    return 0
                from datetime import datetime, timedelta
                d = datetime.strptime(latest[:16].replace("T", " "), "%Y-%m-%d %H:%M")
                cutoff = (d - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
                cur = conn.execute(
                    "DELETE FROM stock_kline_min30 WHERE period=? AND dt < ?",
                    (period, cutoff))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    # ---------------- MACD 状态（变更差分）----------------
    def get_state(self, code: str, freq: str) -> Optional[Dict]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT state,cross,dif,dea,hist,ts FROM macd_state "
                    "WHERE code=? AND freq=?", (code, freq)).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return {"state": row[0], "cross": row[1], "dif": row[2],
                "dea": row[3], "hist": row[4], "ts": row[5]}

    def put_state(self, code: str, freq: str, summary: Dict, ts: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO macd_state "
                    "(code,freq,state,cross,dif,dea,hist,ts) VALUES (?,?,?,?,?,?,?,?)",
                    (code, freq, summary.get("state"), summary.get("cross"),
                     summary.get("dif"), summary.get("dea"),
                     summary.get("hist"), ts))
                conn.commit()
            finally:
                conn.close()

    def stats(self) -> Dict:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT period, COUNT(*), COUNT(DISTINCT code), MIN(dt), MAX(dt) "
                    "FROM stock_kline_min30 GROUP BY period").fetchall()
                states = conn.execute(
                    "SELECT COUNT(*) FROM macd_state").fetchone()[0]
            finally:
                conn.close()
        out = {"macd_states": states}
        for r in rows:
            out[r[0]] = {"rows": r[1], "codes": r[2],
                         "min_dt": r[3], "max_dt": r[4]}
        out["min30_rows"] = out.get("30m", {}).get("rows", 0)
        return out
