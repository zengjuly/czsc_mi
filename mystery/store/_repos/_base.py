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

class BaseRepoMixin:
    def __init__(self, db_path: Optional[str] = None):
            self.db_path = db_path or _DEFAULT_DB
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            self._lock = threading.RLock()
            self._init_db()
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
