"""mystery.store._repos._base - MysteryDB 连接/初始化基类（所有 Repo Mixin 共享）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._helpers import *

STORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class _ConnWrapper:
    """连接代理：转发所有属性到真实 sqlite3.Connection。

    复用期内 close() 为 no-op（连接归线程持有，进程/线程退出由 OS 回收），
    消除每操作开关连接的抖动；不改变任何查询结果或事务语义。
    """
    __slots__ = ("_real",)

    def __init__(self, real):
            object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
            if name == "_real":
                object.__setattr__(self, name, value)
            else:
                setattr(object.__getattribute__(self, "_real"), name, value)

    def close(self):
            # 复用期内不真正关闭；释放由线程/进程退出兜底
            pass

    def _dispose(self):
            try:
                object.__getattribute__(self, "_real").close()
            except Exception:
                pass


class BaseRepoMixin:
    """所有业务 Repo Mixin 的基类：提供 sqlite 短连接（WAL）、库初始化（建表+按序迁移）与线程锁。MysteryDB 通过多重继承聚合各业务 Mixin。"""
    def __init__(self, db_path: Optional[str] = None):
            """初始化 db_path（缺省取 MYSTERY_DB_PATH 或仓库默认库），创建父目录与线程锁，并执行库初始化（建表+迁移）。"""
            self.db_path = db_path or _DEFAULT_DB
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            self._lock = threading.RLock()
            self._conn_local = threading.local()  # W61：每线程复用同一连接
            self._init_db()
    def _connect(self) -> sqlite3.Connection:
            """线程级连接复用（W61）：同一线程内复用同一底层 sqlite 连接。

            旧实现每次 _connect() 都新建并关闭连接（含 WAL pragma），单票
            analyze_one_stock 内 fetch_bars/get_industry/get_financial/
            get_stock_name/cache.get/cache.put 共 11~13 次开闭；全市场扫描
            数千票 = 数万次连接抖动。改为每线程持有一个连接（threading.local），
            复用期内 close() 经 _ConnWrapper 变 no-op，进程/线程退出由 OS 回收。
            每次复用前若残留未提交事务则 rollback，保证事务语义不变；读路径
            不产生写锁、多连接经 WAL 并发安全；写入仍走既有 _lock + commit。
            """
            local = self._conn_local
            w = getattr(local, "wrapper", None)
            if w is not None:
                # 清掉上一次操作异常路径遗留的未提交事务
                try:
                    if w.in_transaction:
                        w.rollback()
                except Exception:
                    w = None
            if w is None:
                real = sqlite3.connect(self.db_path, timeout=30,
                                       check_same_thread=False)
                try:
                    real.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
                w = _ConnWrapper(real)
                local.wrapper = w
            return w
    def _init_db(self) -> None:
            """库不存在或缺表时执行 schema.sql（幂等 CREATE TABLE IF NOT EXISTS），
            再按文件名顺序执行 migrations/*.sql（W21，006.md 阶段 1）。

            schema 单一事实来源：mystery/store/schema.sql（与现网库兼容）。
            迁移记账表 schema_migrations(id TEXT PK, applied_at)：每个 .sql 只执行一次；
            语句级执行并容忍 "duplicate column name"（新库经 schema.sql 已带该列，
            迁移中的 ALTER 属重复，跳过即可——如 001_scan_type 对全新库）。
            """
            schema_path = os.path.join(STORE_DIR,
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
            mig_dir = os.path.join(STORE_DIR,
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
