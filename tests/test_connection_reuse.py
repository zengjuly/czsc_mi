"""W61 回归：MysteryDB._connect() 线程级连接复用。

锁定三项行为：
1. 同线程内连续 _connect() 复用同一底层真实连接（消除每操作开闭抖动）。
2. 不同线程各自持有独立真实连接（线程安全隔离）。
3. 复用前回滚残留未提交事务（保证事务语义不变：异常路径的旧 close 会回滚，
   改为 no-op 后由 _connect 兜底 rollback）。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store import db as dbmod


def test_same_thread_reuses_connection(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "a.db"))
    a = db._connect()
    b = db._connect()
    assert type(a).__name__ == "_ConnWrapper"
    assert a._real is b._real  # 同线程复用同一真实连接


def test_thread_isolation(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "b.db"))
    ids = []
    lock = threading.Lock()

    def worker():
        c = db._connect()
        with lock:
            ids.append(id(c._real))

    ts = [threading.Thread(target=worker) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # 3 个线程 → 3 个不同真实连接对象（彼此隔离）。
    # W69：改用 list 收集，不再以 threading.get_ident() 作 dict key ——
    # 线程结束后 ident 可被新线程复用，曾偶发把 3 个结果覆盖成 1 个。
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_rollback_clears_uncommitted(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "c.db"))
    c = db._connect()
    c.execute("CREATE TABLE t(x)")
    c.commit()
    # 未提交的写入（异常路径下旧实现靠 close 回滚）
    c.execute("INSERT INTO t VALUES (1)")
    assert c.in_transaction is True
    # 复用连接时应自动回滚残留事务
    c2 = db._connect()
    assert c2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_committed_write_persists(tmp_path):
    db = dbmod.MysteryDB(db_path=str(tmp_path / "d.db"))
    c = db._connect()
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (2)")
    c.commit()
    # 已提交的事务复用时保持（不被误回滚）
    c2 = db._connect()
    assert c2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
