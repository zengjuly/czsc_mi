"""W21：schema migration 框架（006.md 阶段 1）。

- 全新库：schema.sql + migrations/*.sql 全部应用，表齐、列齐。
- 旧库（无 scan_type / 无 turn_source）：迁移补列，历史行不丢。
- 幂等：重复初始化不报错、不重复应用。
"""
from __future__ import annotations

import os
import sqlite3

from mystery.store.db import MysteryDB

MIG_FILES = ['001_scan_type.sql', '002_analysis_cache.sql', '003_turnover.sql']


def _migs(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT id FROM schema_migrations").fetchall()}
    finally:
        conn.close()


def _cols(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def test_fresh_db_applies_all_migrations(tmp_path):
    db_path = str(tmp_path / 'fresh.db')
    MysteryDB(db_path)
    assert _migs(db_path) == set(MIG_FILES)
    assert 'scan_type' in _cols(db_path, 'scan_jobs')
    assert 'turn_source' in _cols(db_path, 'stock_kline_data')
    conn = sqlite3.connect(db_path)
    try:
        for t in ('analysis_cache', 'float_share_snapshot'):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone(), t
    finally:
        conn.close()


def test_legacy_db_migration_keeps_history(tmp_path):
    """旧库：scan_jobs 无 scan_type、kline 无 turn_source，且有历史数据。"""
    db_path = str(tmp_path / 'legacy.db')
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE scan_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT,
        started_at TEXT, finished_at TEXT, n_ok INTEGER, n_fail INTEGER)""")
    conn.execute("INSERT INTO scan_jobs (trade_date, n_ok) VALUES ('2026-09-01', 10)")
    conn.execute("""CREATE TABLE stock_kline_data (
        code TEXT NOT NULL, date TEXT NOT NULL, period TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, preclose REAL,
        volume REAL, amount REAL, adjustflag REAL, turn REAL,
        tradestatus REAL, pctChg REAL, isST REAL,
        PRIMARY KEY (code, date, period))""")
    conn.execute("INSERT INTO stock_kline_data (code, date, period, close, turn) "
                 "VALUES ('sh.600519', '2026-09-01', 'daily', 1400, 0.35)")
    conn.commit()
    conn.close()

    MysteryDB(db_path)
    # 新列补上 + 历史行保留 + 旧行 scan_type 默认 market
    assert 'scan_type' in _cols(db_path, 'scan_jobs')
    assert 'turn_source' in _cols(db_path, 'stock_kline_data')
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT scan_type, n_ok FROM scan_jobs "
                           "WHERE trade_date='2026-09-01'").fetchone()
        assert row == ('market', 10)
        turn = conn.execute("SELECT turn FROM stock_kline_data "
                            "WHERE code='sh.600519'").fetchone()
        assert turn == (0.35,)
    finally:
        conn.close()


def test_reinit_idempotent(tmp_path):
    db_path = str(tmp_path / 'idem.db')
    MysteryDB(db_path)
    MysteryDB(db_path)  # 第二次不应报错、不重复应用
    assert _migs(db_path) == set(MIG_FILES)
