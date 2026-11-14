"""W97：pandas.read_sql_query 与 W61 线程复用连接的兼容性专项。

背景
----
_connect() 返回 _ConnWrapper（属性转发代理、close() 为 no-op，用于线程级
连接复用）。pandas 3.x 的 read_sql_query 只认 SQLAlchemy connectable / URI /
sqlite3.Connection，传入代理会每次抛一条 UserWarning：

    pandas only supports SQLAlchemy connectable (engine/connection) or
    database string URI or sqlite3 DBAPI2 connection ...

全市场回放 5582 只票逐票读 K 线 + 板块读 sector_kline，日志被刷数千行，
掩盖真正的信息。W97 在两处 read_sql_query 前用 unwrap_sqlite() 取出底层
真实连接，非代理连接原样返回。

零回归契约
----------
- 查询结果 values / dtypes / 列顺序与改前完全一致；
- 线程级连接复用语义不变（pandas 不关闭外部连接，wrapper.close() 仍 no-op）；
- 原生 sqlite3.Connection / FakeDB 等其它对象原样返回，既有调用零变化；
- load_kline / get_sector_kline 两条生产读路径不再产生该警告。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import warnings

import pandas as pd
import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mystery.store._repos._base import _ConnWrapper, unwrap_sqlite
from mystery.store.db import MysteryDB

PANDAS_WARN = "pandas only supports SQLAlchemy connectable"

KLINE_SQL = (
    "SELECT date, code, open, high, low, close, preclose, volume, amount, "
    "adjustflag, turn, tradestatus, pctChg, isST FROM stock_kline_data "
    "WHERE code=? AND period=? ORDER BY date ASC"
)


@pytest.fixture
def env(tmp_path):
    """临时库：3 根个股 K 线 + 3 根板块 K 线（走生产 schema 建表）。"""
    p = str(tmp_path / "t.db")
    db = MysteryDB(db_path=p)
    conn = db._connect()
    for d, close in (("2026-01-13", 10.0), ("2026-01-14", 11.0),
                     ("2026-01-15", 12.0)):
        conn.execute(
            "INSERT OR REPLACE INTO stock_kline_data "
            "(code, date, period, open, high, low, close, volume, amount, "
            " turn, pctChg) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("sh.600001", d, "daily", close, close + 1, close - 1, close,
             100000, 1000000.0, 1.5, 1.0))
        conn.execute(
            "INSERT OR REPLACE INTO sector_kline "
            "(sector_code, sector_name, trade_date, open, high, low, close, "
            " volume, amount) VALUES (?,?,?,?,?,?,?,?,?)",
            ("ths_TEST", "测试板块", d, close, close, close, close,
             1000000, 10000000.0))
    conn.commit()
    return db, p


def _collect(fn):
    """执行 fn，返回 (结果, 匹配 PANDAS_WARN 的警告数)。"""
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        out = fn()
    return out, sum(1 for w in rec if PANDAS_WARN in str(w.message))


# ---------------------------------------------------------------------------
# unwrap_sqlite 本身
# ---------------------------------------------------------------------------
def test_unwrap_proxy_returns_real_connection():
    real = sqlite3.connect(":memory:")
    w = _ConnWrapper(real)
    assert unwrap_sqlite(w) is real
    assert isinstance(unwrap_sqlite(w), sqlite3.Connection)


def test_unwrap_plain_connection_is_identity():
    real = sqlite3.connect(":memory:")
    assert unwrap_sqlite(real) is real


def test_unwrap_foreign_object_is_identity():
    class FakeDB:
        _real = "not a connection"

    fake = FakeDB()
    assert unwrap_sqlite(fake) is fake
    plain = object()
    assert unwrap_sqlite(plain) is plain


# ---------------------------------------------------------------------------
# 警告消除 + 结果等价
# ---------------------------------------------------------------------------
def test_read_sql_query_no_warning_after_unwrap(env):
    db, _ = env
    conn = db._connect()
    real = unwrap_sqlite(conn)
    plain, n_plain = _collect(
        lambda: pd.read_sql_query(KLINE_SQL, real, params=("sh.600001", "daily")))
    assert n_plain == 0, "unwrap 后不应再有 pandas connectable 警告"


@pytest.mark.filterwarnings("ignore:pandas only supports SQLAlchemy connectable")
def test_read_sql_query_result_identical_to_proxy(env):
    db, _ = env
    conn = db._connect()
    real = unwrap_sqlite(conn)
    via_proxy = pd.read_sql_query(KLINE_SQL, conn, params=("sh.600001", "daily"))
    via_real = pd.read_sql_query(KLINE_SQL, real, params=("sh.600001", "daily"))
    assert via_proxy.equals(via_real)
    assert via_proxy.dtypes.equals(via_real.dtypes)
    assert list(via_proxy.columns) == list(via_real.columns)
    assert len(via_real) == 3


def test_proxy_connection_still_reusable(env):
    """pandas 不得关闭外部连接；wrapper.close() 仍为 no-op（W61 复用契约）。"""
    db, _ = env
    conn = db._connect()
    real = unwrap_sqlite(conn)
    pd.read_sql_query(KLINE_SQL, real, params=("sh.600001", "daily"))
    conn.close()  # no-op
    assert conn.execute("SELECT COUNT(*) FROM stock_kline_data").fetchone()[0] == 3
    assert real.execute("SELECT COUNT(*) FROM stock_kline_data").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# 两条生产读路径
# ---------------------------------------------------------------------------
def test_load_kline_no_warning_and_zero_regression(env):
    db, p = env
    df, n = _collect(lambda: db.load_kline("sh.600001"))
    assert n == 0
    assert len(df) == 3
    assert list(df["date"]) == ["2026-01-13", "2026-01-14", "2026-01-15"]
    # 与直接用原生连接执行同一 SQL 完全一致
    raw = sqlite3.connect(p)
    baseline = pd.read_sql_query(KLINE_SQL, raw, params=("sh.600001", "daily"))
    assert df.equals(baseline)
    assert df.dtypes.equals(baseline.dtypes)
    raw.close()


def test_get_sector_kline_no_warning(env):
    db, _ = env
    df, n = _collect(lambda: db.get_sector_kline("ths_TEST"))
    assert n == 0
    assert df is not None and len(df) == 3
    assert "收盘价" in df.columns


def test_get_sector_kline_no_warning_with_end_date(env):
    """W95/③ 的 as-of 截断路径同样不再告警。"""
    db, _ = env
    df, n = _collect(lambda: db.get_sector_kline("ths_TEST", end_date="2026-01-14"))
    assert n == 0
    assert df is not None and len(df) == 2
