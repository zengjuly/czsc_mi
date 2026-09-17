"""W46 回归：upsert_kline_many 写端代码格式归一（用户 2026-09-17「继续优化」）。

旧版 code_col='thscode' 路径裸 str(code) 写库 → 600000.SH 与读端
sh.600000 错位，DuckDB 预同步 2317 票产生双格式孤儿行（sync 每日重拉
重插、分析读点格式旧行停更）。修复=_dbcode 归一 + 存量合并。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from mystery.store.db import MysteryDB, _dbcode


def _mk_db():
    fd = tempfile.mkdtemp()
    return MysteryDB(str(Path(fd) / "t.db"))


def _df(codes):
    n = len(codes)
    return pd.DataFrame({
        "thscode": codes,
        "date": [f"2026-09-{15 + i % 2:02d}" for i in range(n)],
        "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n,
        "close": [10.5] * n, "volume": [1000] * n, "amount": [10500.0] * n,
        "turnover": [2.5] * n, "pctChg": [1.0] * n,
    })


def test_dbcode_mapping():
    assert _dbcode("600000.SH") == "sh.600000"
    assert _dbcode("000001.SZ") == "sz.000001"
    assert _dbcode("920002.BJ") == "bj.920002"
    assert _dbcode("sh.600519") == "sh.600519"  # 已点格式幂等
    assert _dbcode("sh600519") == "sh.600519"


def test_many_writes_dot_format():
    db = _mk_db()
    db.upsert_kline_many(_df(["600000.SH", "000001.SZ"]), "daily",
                         code_col="thscode")
    conn = db._connect()
    try:
        codes = sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM stock_kline_data"))
        assert codes == ["sh.600000", "sz.000001"], codes
    finally:
        conn.close()


def test_repeat_sync_no_duplicate_rows():
    """同一 thscode 批写两次（模拟 sync 每日重拉）→ 每票每日期恒一行。"""
    db = _mk_db()
    df = _df(["600519.SH"])
    db.upsert_kline_many(df, "daily", code_col="thscode")
    db.upsert_kline_many(df, "daily", code_col="thscode")
    conn = db._connect()
    try:
        n, suffix = conn.execute(
            "SELECT COUNT(*), SUM(code GLOB '*.SH') "
            "FROM stock_kline_data WHERE period='daily'").fetchone()
        assert n == 1 and suffix == 0, (n, suffix)
    finally:
        conn.close()
