"""_invalidate_analysis：两条缓存表失效语义 + 单点归一复用。

直接对内存 sqlite 验证：
 - 给定 code 列表，analysis_cache / chan_cache 中对应 symbol 被删除；
 - 未列入的 symbol 不受影响；
 - _dot 归一只发生一次（两条 DELETE 复用同一参数列表）。
symbol 存储格式以 _dot() 归一结果为准，断言与具体格式解耦。
"""
import sqlite3

from mystery.store._repos._helpers import _invalidate_analysis, _dot


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE analysis_cache (symbol TEXT, payload TEXT)")
    conn.execute("CREATE TABLE chan_cache (symbol TEXT, payload TEXT)")
    return conn


def test_invalidate_clears_given_symbols_only():
    sym_a = _dot("600000.SH")      # 将被失效
    sym_b = _dot("sz.000001")      # 将被失效
    sym_other = _dot("000002.SZ")  # 不被失效，应保留
    conn = _make_conn()
    conn.execute("INSERT INTO analysis_cache VALUES (?, 'a1')", (sym_a,))
    conn.execute("INSERT INTO analysis_cache VALUES (?, 'a2')", (sym_other,))
    conn.execute("INSERT INTO chan_cache VALUES (?, 'c1')", (sym_b,))
    conn.execute("INSERT INTO chan_cache VALUES (?, 'c2')", (sym_other,))
    conn.commit()

    _invalidate_analysis(conn, ["600000.SH", "sz.000001"])
    conn.commit()

    a_left = {r[0] for r in conn.execute("SELECT symbol FROM analysis_cache")}
    c_left = {r[0] for r in conn.execute("SELECT symbol FROM chan_cache")}
    # 给定 code 从两张表都被删；未列入的 sym_other 在两张表均保留
    assert a_left == {sym_other}, a_left
    assert c_left == {sym_other}, c_left


def test_invalidate_empty_is_noop():
    conn = _make_conn()
    conn.execute("INSERT INTO analysis_cache VALUES ('x', 'a1')")
    conn.commit()
    _invalidate_analysis(conn, [])
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0] == 1


def test_dot_called_once_per_code():
    # 通过 monkey 计数验证 _dot 只对每张 code 归一一次（而非两条 DELETE 各一次）
    calls = []
    orig = _dot

    def spy(code):
        calls.append(code)
        return orig(code)

    import mystery.store._repos._helpers as H
    H._dot = spy
    try:
        conn = _make_conn()
        _invalidate_analysis(conn, ["600000.SH", "sz.000001"])
    finally:
        H._dot = orig
    # 2 个 code 各归一 1 次，不应翻倍
    assert calls.count("600000.SH") == 1
    assert calls.count("sz.000001") == 1
