"""W25 扫描互斥锁（006.md 阶段5）：同一时刻只允许一个持久化扫描。

锁实现为 flock 文件锁（scan_market 入口持锁）：
- 并发第二方拒绝（RuntimeError，不排队）
- 持有者进程退出/崩溃自动释放
- no_persist / force 不加锁
本测试 mock _scan_market_impl，只验证锁语义。
"""
import multiprocessing as mp
import os
import subprocess
import sys
import time

import pytest

from mystery.services import scan as scan_mod
from mystery.services.scan import _hold_scan_lock, _scan_lock_path


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / 'test.db')


def _hold_and_signal(db_path, ready, stop):
    """子进程：持锁后通知，直到收到停止信号。"""
    with _hold_scan_lock(db_path):
        ready.set()
        stop.wait(30)


def test_lock_released_after_with_block(db):
    with _hold_scan_lock(db):
        assert os.path.exists(_scan_lock_path(db))
    # 出块后第二方可立即获取
    with _hold_scan_lock(db):
        pass


def test_lock_records_holder(db):
    with _hold_scan_lock(db):
        with open(_scan_lock_path(db)) as f:
            info = f.read()
    assert f'pid={os.getpid()}' in info


def test_second_holder_rejected(db):
    ctx = mp.get_context('fork')
    ready, stop = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_and_signal, args=(db, ready, stop))
    p.start()
    try:
        assert ready.wait(10), '子进程未进入持锁状态'
        with pytest.raises(RuntimeError) as ei:
            with _hold_scan_lock(db):
                pass
        assert '已有扫描在运行' in str(ei.value)
        assert f'pid={p.pid}' in str(ei.value)  # 报错含持有者 pid
    finally:
        stop.set()
        p.join(10)


def test_lock_released_on_crash(db):
    """持有者进程被 SIGKILL → 锁自动释放（flock 语义）。"""
    code = (
        f"import sys, os; sys.path.insert(0, {os.getcwd()!r});"
        f"from mystery.services.scan import _hold_scan_lock;"
        f"from mystery.store.db import MysteryDB;"
        f"_ = MysteryDB(db_path={db!r}) and None;"
        f"lk = _hold_scan_lock({db!r}); lk.__enter__();"
        f"print(os.getpid(), flush=True);"
        f"import time; time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, '-c', code],
                            stdout=subprocess.PIPE, text=True)
    pid_line = proc.stdout.readline().strip()
    assert pid_line.isdigit(), f'子进程启动异常: {pid_line}'
    proc.kill()
    proc.wait(10)
    # 崩溃后第二方应能拿锁
    with _hold_scan_lock(db):
        pass


def test_scan_market_locks_and_forwards(db, monkeypatch):
    """持久化扫描持锁：外部第二方（子进程，同进程 fd 间 flock 不互斥）被拒。"""
    calls = {}

    probe = (
        f"import sys, os\n"
        f"sys.path.insert(0, {os.getcwd()!r})\n"
        f"from mystery.services.scan import _hold_scan_lock\n"
        f"try:\n"
        f"    _hold_scan_lock({db!r}).__enter__()\n"
        f"    sys.exit(0)\n"
        f"except RuntimeError:\n"
        f"    sys.exit(42)\n"
    )

    def fake_impl(**kw):
        calls.update(kw)
        # 持锁期间外部进程第二方必被拒
        r = subprocess.run([sys.executable, '-c', probe], timeout=30)
        assert r.returncode == 42, f'锁未在扫描期间生效: rc={r.returncode}'
        return []

    monkeypatch.setattr(scan_mod, '_scan_market_impl', fake_impl)
    monkeypatch.setattr(scan_mod, 'MysteryDB',
                        lambda db_path=None: type(
                            'D', (), {'db_path': db})())
    # 正常持久化扫描：加锁
    scan_mod.scan_market(watchlist=['600519.SH'])
    assert calls.get('force') is False
    # no_persist：不加锁（probe rc 应为 0）
    def fake_free(**kw):
        calls.clear()
        calls.update(kw)
        r = subprocess.run([sys.executable, '-c', probe], timeout=30)
        assert r.returncode == 0, 'no_persist 不应持锁'
        return []
    monkeypatch.setattr(scan_mod, '_scan_market_impl', fake_free)
    scan_mod.scan_market(watchlist=['600519.SH'], no_persist=True)
    assert calls.get('no_persist') is True
    # force：不加锁
    scan_mod.scan_market(watchlist=['600519.SH'], force=True)
    assert calls.get('force') is True
