"""W95/③：as-of 回放展示层历史化（板块指数 K 线按日截断）专项。

W94 的 `--as-of` 已把个股/指数 K 线严格截断，但三处展示层仍读「当前
快照」：行业腿强度分、板块展示 sector_kline、换手 QA 的 kline_max。
本轮利用 sector_kline 自带的 935 个交易日历史（2023-01-03~2026-11-13），
给 `db.get_sector_kline` 加 `end_date` 参数并沿 SectorClient / 板块分析 /
CLI QA 全链路透传 as_of，使回放截面的行业与板块也是历史值。

零回归契约：end_date=None / 无 MYSTERY_AS_OF 时，SQL 与 W94 前完全一致，
所有既有调用方（正式扫描、Web、日报）行为不变。

本测试覆盖：
- db.get_sector_kline(end_date) 截断、默认零回归、空结果；
- SectorClient 从 cfg / env 读 as_of 且 cfg 优先，截断生效；
- analyze_sector 把 as_of 作为 end_date 传给 db；
- analyze_focus_sectors 未显式传 as_of 时从 env 读并透传；
- CLI 换手 QA 行在回放时标注历史截面。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mystery.adapters.sector import SectorClient
from mystery.services import sector_analysis as SA
from mystery.store.db import MysteryDB


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.pop("MYSTERY_AS_OF", None)
    yield
    os.environ.pop("MYSTERY_AS_OF", None)
    if old is not None:
        os.environ["MYSTERY_AS_OF"] = old


@pytest.fixture
def env(tmp_path):
    """临时库 + sector_kline 三个交易日（走生产 schema 建表）。"""
    p = str(tmp_path / "t.db")
    db = MysteryDB(db_path=p)
    conn = db._connect()
    for date, close in (("2026-01-13", 100.0), ("2026-01-14", 101.0),
                        ("2026-01-15", 102.0)):
        conn.execute(
            "INSERT OR REPLACE INTO sector_kline "
            "(sector_code, sector_name, trade_date, open, high, low, close, "
            " volume, amount) VALUES (?,?,?,?,?,?,?,?,?)",
            ("ths_TEST", "测试板块", date, close, close, close, close,
             1000000, 10000000.0))
    conn.commit()
    return db, p


# ---------------------------------------------------------------------------
# db.get_sector_kline(end_date)
# ---------------------------------------------------------------------------
def test_db_truncates_by_end_date(env):
    db, _ = env
    full = db.get_sector_kline("ths_TEST")
    cut = db.get_sector_kline("ths_TEST", end_date="2026-01-14")
    assert list(full["日期"]) == ["2026-01-13", "2026-01-14", "2026-01-15"]
    assert list(cut["日期"]) == ["2026-01-13", "2026-01-14"]
    assert list(cut["收盘价"]) == [100.0, 101.0]


def test_db_default_is_zero_regression(env):
    db, _ = env
    assert len(db.get_sector_kline("ths_TEST")) == 3
    assert len(db.get_sector_kline("ths_TEST", end_date=None)) == 3


def test_db_end_date_before_all_returns_none(env):
    db, _ = env
    assert db.get_sector_kline("ths_TEST", end_date="2025-01-01") is None


def test_db_code_normalization_still_works(env):
    db, _ = env
    assert len(db.get_sector_kline("TEST", end_date="2026-01-14")) == 2


# ---------------------------------------------------------------------------
# SectorClient as_of 注入
# ---------------------------------------------------------------------------
def test_sector_client_as_of_from_cfg(env):
    _, p = env
    c = SectorClient({"db_path": p, "as_of": "2026-01-14"})
    assert c.as_of == "2026-01-14"
    assert list(c.get_sector_kline("ths_TEST")["日期"]) == [
        "2026-01-13", "2026-01-14"]


def test_sector_client_as_of_from_env(env, monkeypatch):
    _, p = env
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-13")
    c = SectorClient({"db_path": p})
    assert c.as_of == "2026-01-13"
    assert list(c.get_sector_kline("ths_TEST")["日期"]) == ["2026-01-13"]


def test_sector_client_cfg_takes_priority(env, monkeypatch):
    _, p = env
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-13")
    c = SectorClient({"db_path": p, "as_of": "2026-01-14"})
    assert c.as_of == "2026-01-14"
    assert len(c.get_sector_kline("ths_TEST")) == 2


def test_sector_client_no_as_of_zero_regression(env):
    _, p = env
    c = SectorClient({"db_path": p})
    assert c.as_of == ""
    assert len(c.get_sector_kline("ths_TEST")) == 3


# ---------------------------------------------------------------------------
# 板块分析链路透传
# ---------------------------------------------------------------------------
def test_analyze_sector_passes_end_date():
    seen = []

    class FakeDB:
        def get_sector_kline(self, code, end_date=None):
            seen.append(end_date)
            return None

    SA.analyze_sector(FakeDB(), "ths_TEST", "测试板块", as_of="2026-01-14")
    assert seen == ["2026-01-14"]


def test_analyze_sector_zero_regression_passes_none():
    seen = []

    class FakeDB:
        def get_sector_kline(self, code, end_date=None):
            seen.append(end_date)
            return None

    SA.analyze_sector(FakeDB(), "ths_TEST", "测试板块")
    assert seen == [None]


def _patch_focus(monkeypatch, calls):
    monkeypatch.setattr(SA, "resolve_focus_codes",
                        lambda *a, **k: ["ths_TEST"])
    monkeypatch.setattr(SA, "list_sectors",
                        lambda d: [{"code": "ths_TEST", "name": "测试板块"}])
    monkeypatch.setattr(
        SA, "analyze_sector",
        lambda db, c, name, **kw: calls.append(kw.get("as_of"))
        or {"has_data": False})


def test_focus_sectors_reads_as_of_from_env(monkeypatch):
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-14")
    calls = []
    _patch_focus(monkeypatch, calls)
    SA.analyze_focus_sectors(object(), opts={
        "enabled": True, "focus": "auto", "top_n": 8, "corr_window": 60})
    assert calls == ["2026-01-14"]


def test_focus_sectors_explicit_as_of_wins(monkeypatch):
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-14")
    calls = []
    _patch_focus(monkeypatch, calls)
    SA.analyze_focus_sectors(object(), opts={
        "enabled": True, "focus": "auto", "top_n": 8, "corr_window": 60},
        as_of="2026-01-13")
    assert calls == ["2026-01-13"]


def test_focus_sectors_no_as_of_zero_regression(monkeypatch):
    monkeypatch.delenv("MYSTERY_AS_OF", raising=False)
    calls = []
    _patch_focus(monkeypatch, calls)
    SA.analyze_focus_sectors(object(), opts={
        "enabled": True, "focus": "auto", "top_n": 8, "corr_window": 60})
    assert calls == [""]


# ---------------------------------------------------------------------------
# CLI 换手 QA 行：回放标注
# ---------------------------------------------------------------------------
def test_turnover_qa_line_marks_replay(monkeypatch):
    from mystery.apps import cli
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-14")
    monkeypatch.setattr("mystery.services.watchlist.load_watchlist", lambda: [])

    class FakeDB:
        def data_fingerprint(self):
            return {"db_path": "/tmp/t.db", "kline_max": "2026-11-13",
                    "as_of_max": "2026-11-13", "suffix_orphans": 0}

        def turnover_qa_stats(self, trade_date, codes=None):
            return {"coverage": None, "n_symbols": 0,
                    "n_with_shares": 0, "window_start": ""}

        def sector_coverage_stats(self, codes=None):
            return {}

    line = cli._turnover_qa_line(FakeDB(), [], "2026-11-13")
    assert "历史回放 2026-01-14" in line
    assert "非回放截面" in line
