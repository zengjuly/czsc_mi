"""mystery.services.task_lock — W49：写库任务统一互斥 + 进度心跳。

背景（2026-09-18 全市场扫描僵死事故复盘）：W25 的 flock 只护住了
scan_market 一个入口，sync / sector-sync-kline / sync-shares /
sync-turnover 四条写路径无锁——它们与扫描并发时重建 WAL，正是 fork
worker 握旧句柄读写分裂僵死的根因。本模块提供：

1. ``hold_task_lock(db_path, name)`` — 同类任务并发拒绝（不排队），
   报错含持有者 pid/名称/开始时间；崩溃自动释放（flock 语义）。
2. ``report_progress(...)`` — 锁持有者写 ``<db>.tasks.json`` 心跳
   （done/total/label），Web「系统状态」页读取展示定时任务进度。
   心跳文件与锁分离：读侧无需拿锁，写失败静默不阻塞任务。
3. ``running_tasks(db_path)`` — 读侧：列出仍在持锁的心跳（锁已释放
   的心跳视为过期，自动忽略），供页面展示"谁在跑、跑到哪"。

锁文件按任务名分文件（``<db>.lock.<name>``），与 W25 的
``<db>.scanlock``（不同任务名之间可并行）语义保持一致。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from datetime import datetime
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

TASK_NAMES = ('sync', 'sector_kline', 'shares', 'turnover', 'scan')


class TaskBusyError(RuntimeError):
    """W49：同名写库任务已在运行，新开被拒（不排队）。

    继承 RuntimeError 保持既有 `except RuntimeError` 调用点兼容；
    CLI/Web 可专门 catch 本类给出「已有任务进行中」提示（exit 3）。
    """

# 心跳写入节流：至少间隔 N 秒或步数增量 N 才落盘（全市场逐票回调
# 频率很高，每票一次 JSON 重写会拖慢扫描/同步）。
_HB_MIN_INTERVAL = 2.0
_HB_MIN_STEP = 20


def task_lock_path(db_path: str, name: str) -> str:
    return f"{db_path}.lock.{name}"


def heartbeat_path(db_path: str) -> str:
    return f"{db_path}.tasks.json"


@contextlib.contextmanager
def hold_task_lock(db_path: str, name: str,
                   label: str = "", force: bool = False) -> Iterator[None]:
    """持有所属任务的排他锁；被占则 RuntimeError（拒绝，不排队）。

    :param name: 任务名（TASK_NAMES 之一；调用方保证稳定）
    :param label: 写入锁文件的描述（进冲突报错与心跳）
    :param force: True 跳过加锁（与 W25 --force 后门语义一致）
    """
    if force:
        yield
        return
    path = task_lock_path(db_path, name)
    try:
        fh = open(path, 'a+')
    except OSError as e:
        logger.warning(f"[task_lock] 锁文件不可写（{path}）: {e}；本次不加锁继续")
        yield
        return
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            import errno
            if e.errno in (errno.EACCES, errno.EAGAIN):
                fh.seek(0)
                who = (fh.read() or '').strip() or '其他进程'
                raise TaskBusyError(
                    f"已有同名任务在运行：{name}（{who}）。"
                    "写库任务并发被拒绝（不排队），请等其结束后重试。"
                ) from e
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} task={name} started="
                 f"{datetime.now().isoformat(timespec='seconds')}"
                 + (f" label={label}" if label else ""))
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def report_progress(db_path: str, name: str, done: int, total: int,
                    label: str = "", force: bool = False) -> None:
    """锁持有者上报进度心跳（合并写 <db>.tasks.json）。永不抛异常。

    节流：距上次写入 <_HB_MIN_INTERVAL 秒且步数增量 <_HB_MIN_STEP 时
    跳过（done>=total 收尾写必过，保证终态可见）。
    force=True 立即写（任务刚启动写 label/total 时用）。
    """
    key = (db_path, name)
    now = datetime.now()
    ts = now.timestamp()
    last = _HB_STATE.get(key)
    if last is not None and not force:
        last_ts, last_done = last
        if (ts - last_ts < _HB_MIN_INTERVAL
                and 0 <= done - last_done < _HB_MIN_STEP):
            return
    _HB_STATE[key] = (ts, done)
    path = heartbeat_path(db_path)
    try:
        tasks = {}
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                tasks = data.get('tasks') or {}
        tasks[name] = {
            'label': label or (tasks.get(name) or {}).get('label') or '',
            'done': int(done), 'total': int(total),
            'pid': os.getpid(),
            'updated': now.isoformat(timespec='seconds'),
        }
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'tasks': tasks}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # 心跳坏了绝不阻塞任务
        logger.debug(f"[task_lock] 心跳写入失败 {path}: {str(e)[:80]}")


_HB_STATE: dict = {}


def clear_progress(db_path: str, name: str) -> None:
    """任务收尾删掉自己的心跳（永不抛异常）。"""
    _HB_STATE.pop((db_path, name), None)
    path = heartbeat_path(db_path)
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        tasks = (data or {}).get('tasks') or {}
        if name not in tasks:
            return
        tasks.pop(name)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'tasks': tasks}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug(f"[task_lock] 心跳清理失败 {path}: {str(e)[:80]}")


def _lock_held(name: str, db_path: str) -> Optional[str]:
    """探测本进程视角下该任务锁是否被占：返回锁文件内容或 None。"""
    path = task_lock_path(db_path, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r+') as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # 被占 → 读持有者信息
                content = fh.read().strip()
                return content or '运行中'
            # 拿到锁 = 没人跑，立刻放掉
            fcntl.flock(fh, fcntl.LOCK_UN)
        return None
    except OSError:
        return None


def running_tasks(db_path: str) -> list:
    """当前真实在跑的任务（心跳 ∩ 锁仍被持有）。

    返回 [{'name','label','done','total','updated','pid','pid_alive',
    'pct'}, ...]；崩溃/被 kill 残留的心跳因锁已释放而不会列出。
    """
    path = heartbeat_path(db_path)
    out = []
    try:
        if not os.path.exists(path):
            return out
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        tasks = (data or {}).get('tasks') or {}
    except Exception:
        return out
    for name, t in tasks.items():
        who = _lock_held(name, db_path)
        if who is None:
            continue  # 锁已释放 = 任务早已结束，心跳过期
        try:
            pid = int(t.get('pid') or 0)
        except (TypeError, ValueError):
            pid = 0
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = pid != 0 and not _pid_dead(pid)
        done = int(t.get('done') or 0)
        total = int(t.get('total') or 0)
        out.append({
            'name': name, 'label': t.get('label') or '',
            'done': done, 'total': total,
            'pct': (done / total) if total else None,
            'updated': t.get('updated') or '', 'pid': pid,
            'pid_alive': alive, 'holder': who,
        })
    out.sort(key=lambda x: x.get('updated') or '', reverse=True)
    return out


def _pid_dead(pid: int) -> bool:
    """/proc 判进程是否已消失（pid 复用防护：僵尸进程也视为死）。"""
    try:
        with open(f"/proc/{pid}/stat", encoding='utf-8') as f:
            parts = f.read().rsplit(') ', 1)
            state = parts[1].split()[0] if len(parts) > 1 else '?'
        return state == 'Z'
    except OSError:
        return True
