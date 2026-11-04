"""mystery.store._repos._helpers - db.py 共享常量与辅助函数（拆分后复用，避免循环 import）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_DEFAULT_DB = os.environ.get(
    "MYSTERY_DB_PATH",
    os.path.join(_REPO_ROOT, "data", "mystery_cache.db"),
)

_CN_COLS = {'date': '日期', 'open': '开盘价', 'high': '最高价', 'low': '最低价',
            'close': '收盘价', 'preclose': '昨收', 'volume': '成交量',
            'amount': '成交额', 'adjustflag': '复权状态', 'turn': '换手率',
            'tradestatus': '交易状态', 'pctChg': '涨跌幅', 'isST': '是否ST'}

DAILY_KLINE_MAX_ROWS = int(os.environ.get("MYSTERY_DAILY_K_MAX_ROWS", "2000"))

def _prune_daily(conn: sqlite3.Connection, codes) -> int:
    """滚动剪除：指定 code 集合内 daily 行数超上限的，删最旧行补齐。

    在 upsert 同一事务内调用，只扫写入触及的 code；PRIMARY KEY
    (code,date,period) 下 COUNT 与 cutoff 定位实测 <1ms（2026-09-17），
    未超限时仅一次聚合查询，成本可忽略。返回删除行数。
    """
    cap = DAILY_KLINE_MAX_ROWS
    if not cap:
        return 0
    uniq = [c for c in dict.fromkeys(codes) if c]
    if not uniq:
        return 0
    deleted = 0
    for i in range(0, len(uniq), 400):  # IN 变量数分块，避 SQLite 参数上限
        chunk = uniq[i:i + 400]
        ph = ",".join("?" * len(chunk))
        over = conn.execute(
            f"SELECT code, COUNT(*) FROM stock_kline_data "
            f"WHERE period='daily' AND code IN ({ph}) GROUP BY code "
            f"HAVING COUNT(*) > ?", (*chunk, cap)).fetchall()
        for code, _n in over:
            cur = conn.execute(
                "DELETE FROM stock_kline_data WHERE code=? AND period='daily' "
                "AND date <= (SELECT date FROM stock_kline_data WHERE code=? "
                "AND period='daily' ORDER BY date DESC LIMIT 1 OFFSET ?)",
                (code, code, cap))
            deleted += cur.rowcount
    return deleted

def to_cn_columns(df: pd.DataFrame) -> pd.DataFrame:
    """DB 列 → 中文列（含 日期/代码）。"""
    out = df.copy()
    rename = {k: v for k, v in _CN_COLS.items() if k in out.columns}
    return out.rename(columns=rename)

def _dot(code: str) -> str:
    """任意库内代码（sh.600519 / 600519.SH / thscode）→ 点号格式 600519.SH。

    W41：归一逻辑唯一实现在 adapters.codes.normalize_symbol（旧版本地复制
    的正则/推断分支已删，防两处漂移）；本函数保留 _dot 的宽容回退语义——
    normalize_symbol 抛 ValueError 的输入原样大写返回（原行为，调用方
    如 watchlist 混入非标准码不致炸查询）。codes.py 仅依赖 re，无 import 环
    （cache.dot_symbol 同款路径已验证）。
    """
    from mystery.adapters.codes import normalize_symbol
    try:
        return normalize_symbol(code)
    except ValueError:
        return str(code).strip().upper()

def _dbcode(code: str) -> str:
    """任意代码 → 库内点格式 sh.600519（W46：写端与读端格式统一）。

    复用 codes.db_code_of（内部先 normalize_symbol 再拆）；非法输入宽容
    回退原样小写（与 _dot 对称语义，不炸批量写入）。
    """
    from mystery.adapters.codes import db_code_of
    try:
        return db_code_of(code)
    except ValueError:
        return str(code).strip().lower()

def _invalidate_analysis(conn: sqlite3.Connection, codes) -> None:
    """W23：sync 更新 K 线 → 删除该票 analysis_cache（006.md 阶段 3）。

    W35 P0-C：chan_cache 一并失效——结构由 K 线派生，盘后补洞/回写同日 K
    后旧结构（笔/中枢/背驰）即过期；trade_date 键只挡新一天，挡不住同日修订。

    与 K 线写入同一连接/事务；表缺失（未迁移）时静默跳过。
    在 commit 之前调用，随同一事务提交。

    codes 只做一次 _dot 归一，两条 DELETE 复用同一参数列表，
    避免对同批 code 重复归一（多票批量写入热路径上省掉近半 Python 开销）。
    """
    params = [(_dot(c),) for c in codes]
    try:
        conn.executemany(
            "DELETE FROM analysis_cache WHERE symbol=?", params)
    except sqlite3.Error:
        pass
    try:
        conn.executemany(
            "DELETE FROM chan_cache WHERE symbol=?", params)
    except sqlite3.Error:
        pass

def _f(v: Any) -> Optional[float]:
    """通用浮点归一：None / NaN / 不可转换值 → None，其余转 float。写入端标量清洗，与读端容错同源。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except Exception:
        return None

def _t(v: Any) -> Optional[float]:
    """换手率写端口径（W43）：0/负数/越界(≥80)/NaN → None。

    上游（baostock/DuckDB v_daily_qfq）缺数以 0 填充；0 一旦入库，
    QA 的 `turn IS NOT NULL` 把它当有值（覆盖率虚高）、ffill 拿它当锚点
    （0 值传播）、mystery_rules 筹码集中度 mean 被拉歪。
    归一为 None 后 upsert 的 COALESCE 自动走保旧值/留空路径。
    与读端 adapters.market._turn_opt、core.turnover.is_valid_turnover 同源。
    """
    f = _f(v)
    if f is None or not (0 < f < 80):
        return None
    return f

def _d(v: Any) -> str:
    """日期写入端归一：pandas Timestamp/ISO 带时间后缀 → 'YYYY-MM-DD'。

    09-16 清洗教训：DuckDB 预同步写入 Timestamp 产生 'YYYY-MM-DD 00:00:00'
    长格式，与在线回写的短格式在主键 (code,date,period) 下变成同日双行
    （1020 万条），污染 MA/窗口计算。所有入库日期必须过此函数。
    """
    s = str(v)
    if len(s) > 10 and s[10] in ' T':
        s = s[:10]
    return s

__all__ = ['logger', '_REPO_ROOT', '_DEFAULT_DB', '_CN_COLS', 'DAILY_KLINE_MAX_ROWS', '_prune_daily', 'to_cn_columns', '_dot', '_dbcode', '_invalidate_analysis', '_f', '_t', '_d']
