"""mystery.store.db — SQLite 数据中枢（schema 复用现网 mystery_cache.db）。

默认库：环境变量 MYSTERY_DB_PATH（生产库）→ 仓内 ./data/mystery_cache.db。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_DB = os.environ.get(
    "MYSTERY_DB_PATH",
    os.path.join(_REPO_ROOT, "data", "mystery_cache.db"),
)

# DB 英文列 → 中文列（与旧仓 market_data_client._to_cn_columns 一致）
_CN_COLS = {'date': '日期', 'open': '开盘价', 'high': '最高价', 'low': '最低价',
            'close': '收盘价', 'preclose': '昨收', 'volume': '成交量',
            'amount': '成交额', 'adjustflag': '复权状态', 'turn': '换手率',
            'tradestatus': '交易状态', 'pctChg': '涨跌幅', 'isST': '是否ST'}


def to_cn_columns(df: pd.DataFrame) -> pd.DataFrame:
    """DB 列 → 中文列（含 日期/代码）。"""
    out = df.copy()
    rename = {k: v for k, v in _CN_COLS.items() if k in out.columns}
    return out.rename(columns=rename)


class MysteryDB:
    """本地库客户端（读为主，写带锁）。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DEFAULT_DB
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    # ---------------- 连接/建表 ----------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _init_db(self) -> None:
        """库不存在或缺表时执行 schema.sql（幂等 CREATE TABLE IF NOT EXISTS）。

        schema 单一事实来源：mystery/store/schema.sql（与现网库兼容）。
        """
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, encoding="utf-8") as f:
                ddl = f.read()
        else:
            ddl = "CREATE TABLE IF NOT EXISTS chan_cache (symbol TEXT, freq TEXT, trade_date TEXT, czsc_ver TEXT, payload_json TEXT, PRIMARY KEY(symbol, freq, trade_date, czsc_ver));"
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(ddl)
                conn.commit()
            finally:
                conn.close()

    # ---------------- 行情 ----------------
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

    def upsert_kline(self, df: pd.DataFrame, code: str, period: str,
                     max_rows: Optional[int] = None) -> None:
        """写行情（df 为中文列或英文列均可）。

        换手率用 COALESCE：新值为 None 时保留库内旧值（ths 数据无换手率，
        避免覆盖 baostock 同步的历史 turn —— 2026-08-27 教训）。
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
                        (code, str(r.get('日期')), period,
                         _f(r.get('开盘价')), _f(r.get('最高价')), _f(r.get('最低价')),
                         _f(r.get('收盘价')), _f(r.get('成交量')), _f(r.get('成交额')),
                         _f(r.get('换手率')), _f(r.get('涨跌幅'))))
                conn.commit()
            finally:
                conn.close()

    # ---------------- 股票/板块 ----------------
    def get_stock_list(self, stock_only: bool = True) -> List[Dict]:
        """证券列表 [{code, name}]，code 形如 sh.600519。"""
        with self._lock:
            conn = self._connect()
            try:
                sql = ("SELECT code, code_name FROM stock_industry_info "
                       "WHERE code_name IS NOT NULL AND code_name != ''")
                if stock_only:
                    sql += " AND (type='1' OR type IS NULL)"
                rows = conn.execute(sql).fetchall()
                out = []
                for c, n in rows:
                    if n and str(n) != 'nan':
                        out.append({'code': str(c), 'name': str(n)})
                return out
            finally:
                conn.close()

    def get_stock_name(self, code: str) -> str:
        """查股票名称（sh600519 / sh.600519 / 600519.SH 均可）。"""
        from ..adapters.codes import db_code_of
        try:
            db_code = db_code_of(code) if not code.startswith(('sh.', 'sz.', 'bj.')) else code
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT code_name FROM stock_industry_info WHERE code=? LIMIT 1",
                        (db_code,)).fetchone()
                    return str(row[0]) if row and row[0] else ""
                finally:
                    conn.close()
        except Exception:
            return ""

    def get_primary_industry(self, stock_code: str) -> Optional[tuple]:
        """股票主行业 → (sector_code, sector_name) 或 None。"""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT r.sector_code, m.sector_name "
                    "FROM stock_sector_rel r LEFT JOIN sector_meta m "
                    "ON r.sector_code=m.sector_code "
                    "WHERE r.stock_code=? AND r.is_primary=1 LIMIT 1",
                    (stock_code,)).fetchone()
                return row
            finally:
                conn.close()

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

    # ---------------- 财务 ----------------
    def get_financial(self, code: str) -> Dict[str, Any]:
        """最新财报快照 {pe, pb, roe, roe_avg, eps_ttm, dividend, report_date...}。"""
        from ..adapters.codes import db_code_of
        db_code = db_code_of(code) if not code.startswith(('sh.', 'sz.', 'bj.')) else code
        try:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT report_date, roe, roe_avg, np_margin, gp_margin, "
                        "net_profit, eps_ttm, PB, PE, divid_cash "
                        "FROM stock_financial_data WHERE code=? "
                        "ORDER BY report_date DESC LIMIT 1", (db_code,)).fetchone()
                    if not row:
                        return {}
                    cols = ['report_date', 'roe', 'roe_avg', 'np_margin', 'gp_margin',
                            'net_profit', 'eps_ttm', 'PB', 'PE', 'divid_cash']
                    return {k: (float(v) if v is not None else None)
                            for k, v in zip(cols, row)}
                finally:
                    conn.close()
        except Exception:
            return {}

    # ---------------- 缠论缓存 ----------------
    def get_chan_cache(self, symbol: str, freq: str, trade_date: str,
                       czsc_ver: str) -> Optional[str]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload_json FROM chan_cache "
                    "WHERE symbol=? AND freq=? AND trade_date=? AND czsc_ver=?",
                    (symbol, freq, trade_date, czsc_ver)).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    def set_chan_cache(self, symbol: str, freq: str, trade_date: str,
                       czsc_ver: str, payload_json: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO chan_cache "
                    "(symbol, freq, trade_date, czsc_ver, payload_json) "
                    "VALUES (?,?,?,?,?)",
                    (symbol, freq, trade_date, czsc_ver, payload_json))
                conn.commit()
            finally:
                conn.close()


def _f(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except Exception:
        return None
