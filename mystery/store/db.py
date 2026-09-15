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


def _dot(code: str) -> str:
    """任意库内代码（sh.600519 / 600519.SH / thscode）→ 点号格式 600519.SH。

    与 adapters.codes.normalize_symbol 同逻辑（此处本地实现避免 import 环）。
    """
    import re as _re
    s = str(code).strip().lower()
    m = _re.match(r'^(?:(sh|sz|bj)\.?)?(\d{6})(?:\.(sh|sz|bj))?$', s)
    if not m:
        return str(code).strip().upper()
    prefix, digits, suffix = m.group(1), m.group(2), m.group(3)
    exch = (suffix or prefix or '').upper()
    if not exch:
        exch = 'BJ' if digits.startswith('92') else (
            'SH' if digits[0] in '569' else 'SZ')
    return f"{digits}.{exch}"


def _invalidate_analysis(conn: sqlite3.Connection, codes) -> None:
    """W23：sync 更新 K 线 → 删除该票 analysis_cache（006.md 阶段 3）。

    与 K 线写入同一连接/事务；表缺失（未迁移）时静默跳过。
    在 commit 之前调用，随同一事务提交。
    """
    try:
        conn.executemany(
            "DELETE FROM analysis_cache WHERE symbol=?",
            [(_dot(c),) for c in codes])
    except sqlite3.Error:
        pass


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
        """库不存在或缺表时执行 schema.sql（幂等 CREATE TABLE IF NOT EXISTS），
        再按文件名顺序执行 migrations/*.sql（W21，006.md 阶段 1）。

        schema 单一事实来源：mystery/store/schema.sql（与现网库兼容）。
        迁移记账表 schema_migrations(id TEXT PK, applied_at)：每个 .sql 只执行一次；
        语句级执行并容忍 "duplicate column name"（新库经 schema.sql 已带该列，
        迁移中的 ALTER 属重复，跳过即可——如 001_scan_type 对全新库）。
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
                self._run_migrations(conn)
                conn.commit()
            finally:
                conn.close()

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """按序执行未应用的 migrations/*.sql（幂等，单事务每文件）。"""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "id TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        done = {r[0] for r in conn.execute(
            "SELECT id FROM schema_migrations").fetchall()}
        mig_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "migrations")
        if not os.path.isdir(mig_dir):
            return
        for fname in sorted(os.listdir(mig_dir)):
            if not fname.endswith(".sql"):
                continue
            if fname in done:
                continue
            with open(os.path.join(mig_dir, fname), encoding="utf-8") as f:
                sql = f.read()
            # 语句级执行：拆掉注释行后按分号分段（迁移文件均无触发器/存储过程）
            body = "\n".join(
                ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
            for stmt in [s.strip() for s in body.split(";") if s.strip()]:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    # 新库 schema.sql 已建好列时 ALTER 重复 —— 视为已应用
                    if "duplicate column name" in str(e).lower():
                        continue
                    raise
            conn.execute("INSERT OR REPLACE INTO schema_migrations (id) VALUES (?)",
                         (fname,))
            logger.info(f"[db] 已应用迁移 {fname}")

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
                _invalidate_analysis(conn, [code])
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
        data = [
            (str(code), str(r.get('日期')), period,
             _f(r.get('开盘价')), _f(r.get('最高价')), _f(r.get('最低价')),
             _f(r.get('收盘价')), _f(r.get('成交量')), _f(r.get('成交额')),
             _f(r.get('换手率')), _f(r.get('涨跌幅')))
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
        from ..adapters.codes import db_code_of
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

    def upsert_sector_meta(self, sector_code: str, sector_name: str,
                           parent_type: str = '行业/概念',
                           is_active: int = 1) -> None:
        """upsert 板块元数据（保持入参原格式写入，与存量混格式兼容）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO sector_meta (sector_code, sector_name, parent_type, "
                    "base_code, is_active, last_sync_date) VALUES (?,?,?,?,?,"
                    "date('now','localtime')) "
                    "ON CONFLICT(sector_code) DO UPDATE SET "
                    "sector_name=excluded.sector_name, parent_type=excluded.parent_type, "
                    "is_active=excluded.is_active, last_sync_date=excluded.last_sync_date",
                    (str(sector_code).strip(), str(sector_name).strip(),
                     parent_type, str(sector_code).strip().split('.')[-1]
                     .replace('ths_', '')[:6], int(is_active)))
                conn.commit()
            finally:
                conn.close()

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

    # ---------------- 财务 ----------------
    def set_financial(self, code: str, report_date: str,
                      roe=None, roe_avg=None, np_margin=None, gp_margin=None,
                      pe=None, pb=None, eps_ttm=None, divid_cash=None) -> None:
        """upsert 财务快照（code 归一为 sh.600519，与 get_financial 一致）。"""
        from ..adapters.codes import db_code_of
        db_code = db_code_of(code) if not code.startswith(('sh.', 'sz.', 'bj.')) \
            else code
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO stock_financial_data "
                    "(code, report_date, roe, roe_avg, np_margin, gp_margin, "
                    "net_profit, eps_ttm, PB, PE, divid_cash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (db_code, report_date, roe, roe_avg, np_margin, gp_margin,
                     None, eps_ttm, pb, pe, divid_cash))
                conn.commit()
            finally:
                conn.close()

    def get_financial(self, code: str) -> Dict[str, Any]:
        """最新财报快照 {pe, pb, roe, roe_avg, eps_ttm, divid_cash, report_date...}。"""
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
                    # report_date 是 'YYYY-Q'/'YYYY-MM-DD' 字符串，绝不能 float()——
                    # float('2026-2') 抛 ValueError 曾让本函数整体 except 返回 {}，
                    # 86 只自选每天逐股在线补财务（W12）。
                    fin = {'report_date': row[0]}
                    for k, v in zip(['roe', 'roe_avg', 'np_margin', 'gp_margin',
                                     'net_profit', 'eps_ttm', 'PB', 'PE',
                                     'divid_cash'], row[1:]):
                        fin[k] = float(v) if v is not None else None
                    return fin
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


    # ---------------- 换手治理（W22，006.md 阶段 2） ----------------
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
                    f"SUM(CASE WHEN turn IS NOT NULL THEN 1 ELSE 0 END) "
                    f"FROM stock_kline_data WHERE {where} "
                    f"AND substr(date,1,10)>=? AND substr(date,1,10)<=? "
                    f"GROUP BY 1", args + [start, anchor]).fetchall()
                total = sum(r[1] for r in rows)
                # 覆盖以 turn 值为准（历史行 turn_source 为 NULL 但值在）
                with_turn = sum(r[2] for r in rows)
                return {'anchor': anchor, 'window_start': start,
                        'rows': total, 'with_turn': with_turn,
                        'coverage': round(with_turn / total, 4) if total else None,
                        'by_source': {r[0]: r[1] for r in rows}}
            finally:
                conn.close()

    def turnover_qa_stats(self, trade_date: str,
                          codes: Optional[List[str]] = None,
                          window_days: int = 33) -> Dict:
        """W26b QA：窗口 [trade_date-window_days, trade_date] 内按票统计。

        universe 口径：窗口内有日 K 行的票才算分母（无 K 不摊薄覆盖率）。
        n_with_shares：存在 as_of <= trade_date 股本快照的票（可派生票）。
        by_source 含 null 键 = 窗口内仍无 turn 的行数（chip_low_unknown 根源）。
        """
        from datetime import datetime, timedelta
        start = (datetime.strptime(trade_date, '%Y-%m-%d')
                 - timedelta(days=window_days)).strftime('%Y-%m-%d')
        with self._lock:
            conn = self._connect()
            try:
                where = ("period='daily' AND substr(date,1,10)>=? "
                         "AND substr(date,1,10)<=?")
                args: List = [start, trade_date]
                if codes:
                    where += f" AND code IN ({','.join('?' * len(codes))})"
                    args += list(codes)
                uni = ('' if codes else
                       " AND code IN (SELECT code FROM stock_industry_info"
                       " WHERE type='1' OR type IS NULL)")
                rows = conn.execute(
                    f"SELECT code, COALESCE(turn_source,'null'), COUNT(*), "
                    f"SUM(CASE WHEN turn IS NOT NULL THEN 1 ELSE 0 END) "
                    f"FROM stock_kline_data WHERE {where}{uni} "
                    f"GROUP BY code, 2", args).fetchall()
                stats = {'as_of': trade_date, 'window_start': start,
                         'n_symbols': 0, 'n_with_shares': 0,
                         'rows': 0, 'with_turn': 0, 'coverage': None,
                         'by_source': {}, 'n_symbols_full': 0}
                by_code: Dict[str, tuple] = {}
                for code, src, n, w in rows:
                    prev = by_code.get(code, (0, 0))
                    by_code[code] = (prev[0] + n, prev[1] + w)
                    stats['by_source'][src] = stats['by_source'].get(src, 0) + n
                if codes:
                    stats['n_symbols'] = len(set(codes))
                else:
                    stats['n_symbols'] = len(by_code)
                for code, (n, w) in by_code.items():
                    stats['rows'] += n
                    stats['with_turn'] += w
                    if n and w == n:
                        stats['n_symbols_full'] += 1
                snaps = conn.execute(
                    "SELECT DISTINCT thscode FROM float_share_snapshot "
                    "WHERE as_of<=?", (trade_date,)).fetchall()
                snap_set = {r[0] for r in snaps}
                targets = set(codes) if codes else set(by_code)
                stats['n_with_shares'] = sum(
                    1 for c in targets if _dot(c) in snap_set)
                if stats['rows']:
                    stats['coverage'] = round(
                        stats['with_turn'] / stats['rows'], 4)
                return stats
            finally:
                conn.close()


def _f(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except Exception:
        return None
