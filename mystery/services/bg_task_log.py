"""mystery.services.bg_task_log — W77：后台扫描任务持久化台账。

背景
----
Web 后台任务此前只存在 ``@st.cache_resource`` 的内存字典（components.py
``_bg_store()``）：服务重启、跨浏览器会话、W75「清除卡住的任务」之后记录
全部消失；而 ``scan_jobs`` 表按 W18 约定「同类型只保留最新一份」，每次写库
先删光同类型旧 job，历史扫描记录也被抹掉。二者叠加，用户点击
「🚀 后台扫描全部股票」后，在「最近任务」里常常看不到任何记录——尤其
扫描失败 / 被并发锁拒绝 / daemon 线程被重启打断时，连一条痕迹都不留。

本模块把每次后台任务的启动、进度、终态写进生产库 ``bg_task_log`` 表
（轻量短事务，每任务数行），与 ``scan_jobs`` 通过 ``job_id`` 关联：服务
重启后仍可查，页面「后台任务台账」展示全部历史。所有写操作异常硬隔离，
绝不影响扫描本身。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS bg_task_log (
  id           TEXT PRIMARY KEY,
  label        TEXT NOT NULL,
  status       TEXT NOT NULL,
  done         INTEGER DEFAULT 0,
  total        INTEGER DEFAULT 0,
  stage        TEXT DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  job_id       INTEGER,
  n_results    INTEGER DEFAULT 0,
  error        TEXT DEFAULT '',
  scan_type    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bg_task_log_created
  ON bg_task_log (created_at DESC);
"""

STATUS_LABELS = {
    "running": "⏳ 运行中",
    "done": "✅ 完成",
    "error": "❌ 失败",
    "cleared": "🧹 已清除",
    "interrupted": "⚠️ 中断",
}

# 台账保留上限（轻量行，200 条约几十 KB）；每次启动新任务时裁剪一次。
LOG_KEEP = 200

_ensured: set = set()
_ensured_lock = threading.Lock()


def _ts(ts: Optional[float] = None) -> str:
    """与 datetime.now().isoformat(timespec='seconds') 同口径的本地时间串。"""
    return datetime.fromtimestamp(
        ts if ts is not None else time.time()).isoformat(timespec="seconds")


def db_path_of(cfg: Optional[Dict[str, Any]] = None) -> str:
    """解析生产库路径（与 MarketDataClient / scan_market 口径一致）。"""
    from mystery.store.db import MysteryDB
    return MysteryDB(db_path=(cfg or {}).get("db_path")).db_path


def ensure_table(db_path: str) -> None:
    """惰性建表（新库由 schema.sql / migration 建；此处双保险，进程内缓存）。"""
    with _ensured_lock:
        if db_path in _ensured:
            return
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.executescript(TABLE_DDL)
        conn.commit()
    finally:
        conn.close()
    with _ensured_lock:
        _ensured.add(db_path)


def _safe(fn, *args, **kwargs):
    """台账是辅助记录：任何异常都只告警，绝不抛给扫描/页面。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bg_task_log] {fn.__name__} 失败（不影响扫描）: "
                       f"{str(e)[:120]}")
        return None


def _exec(db_path: str, sql: str, params: tuple) -> None:
    ensure_table(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def log_start(db_path: str, task_id: str, label: str, *,
              created: Optional[float] = None, scan_type: str = "") -> None:
    """任务启动即留痕：即使随后崩溃 / 被重启打断，台账里也看得到。"""
    now = _ts(created)
    _safe(_exec, db_path,
          "INSERT OR REPLACE INTO bg_task_log "
          "(id,label,status,done,total,stage,created_at,updated_at,"
          " job_id,n_results,error,scan_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (task_id, label, "running", 0, 0, "", now, now,
           None, 0, "", scan_type))
    _safe(prune, db_path, LOG_KEEP)


def log_progress(db_path: str, task_id: str, done: int, total: int,
                 stage: str = "", *, updated: Optional[float] = None) -> None:
    """进度心跳（调用侧负责节流；这里只做一次轻量 UPDATE）。"""
    _safe(_exec, db_path,
          "UPDATE bg_task_log SET done=?, total=?, stage=?, updated_at=? "
          "WHERE id=? AND status='running'",
          (int(done or 0), int(total or 0), str(stage or ""),
           _ts(updated), task_id))


def log_finish(db_path: str, task_id: str, status: str, *,
               job_id: Optional[int] = None, n_results: int = 0,
               error: str = "", updated: Optional[float] = None) -> None:
    """任务终态：done / error / cleared / interrupted。"""
    _safe(_exec, db_path,
          "UPDATE bg_task_log SET status=?, job_id=?, n_results=?, "
          "error=?, updated_at=? WHERE id=?",
          (status, job_id, int(n_results or 0), str(error or "")[:300],
           _ts(updated), task_id))


def recent(db_path: str, limit: int = 20) -> List[Dict[str, Any]]:
    """最近任务台账（创建时间降序）；读失败返回空列表，不阻断页面。"""
    def _read():
        ensure_table(db_path)
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            cur = conn.execute(
                "SELECT id,label,status,done,total,stage,created_at,"
                "updated_at,job_id,n_results,error,scan_type "
                "FROM bg_task_log ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (int(limit),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    return _safe(_read) or []


def mark_stale(db_path: str, timeout_seconds: float = 600.0) -> int:
    """把超时无更新的 running 记录标记为 interrupted（进程重启/崩溃残留）。

    返回被标记条数。只改状态不删记录，历史仍可查。
    """
    cutoff = _ts(time.time() - float(timeout_seconds))
    msg = f"超过 {int(timeout_seconds)} 秒无进度更新，判定中断"

    def _run():
        ensure_table(db_path)
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            cur = conn.execute(
                "UPDATE bg_task_log SET status='interrupted', "
                "error=CASE WHEN error='' OR error IS NULL THEN ? ELSE error END, "
                "updated_at=? WHERE status='running' AND updated_at < ?",
                (msg, _ts(), cutoff))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    return _safe(_run) or 0


def prune(db_path: str, keep: int = LOG_KEEP) -> int:
    """只保留最近 keep 条台账记录，避免无限膨胀。"""
    def _run():
        ensure_table(db_path)
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            cur = conn.execute(
                "DELETE FROM bg_task_log WHERE id IN ("
                " SELECT id FROM bg_task_log "
                " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (max(1, int(keep)),))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    return _safe(_run) or 0


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "-")
