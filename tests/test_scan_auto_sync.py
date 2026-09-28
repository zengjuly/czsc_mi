"""扫描前行情时效检查 + 自动同步（_ensure_scan_data 分支）。

判定口径（库内 daily）：
- 最新日 >= 真实最新交易日 **且** 该日覆盖率 >= _MIN_COVERAGE → 已最新，跳过；
  只看 MAX(date) 太宽松（实测库里仅 2 只有当天数据也被判已最新）；
- 落后 + 已收盘（≥15:30）或非交易日 → sync_market() 全量（无 symbols）；
- 落后 + 盘中交易日 → sync_market(symbols=codes) 仅更新本次扫描股票；
- auto_sync=False → 跳过（不查日历、不查库、不调 sync_market）。
同步异常不阻断（返回说明字符串，扫描继续）。

mock 注意：_ensure_scan_data 内部是函数级 `from ..adapters.calendar
import` / `from .sync import sync_market`，必须 patch **源头模块**
（calendar / sync），patch scan 模块自身属性无效。
完全离线：不碰网络、不碰生产库。
"""
import sqlite3
from datetime import datetime

import pytest

import mystery.adapters.calendar as cal_mod
import mystery.services.scan as scan_mod
import mystery.services.sync as sync_mod
from mystery.services.scan import _ensure_scan_data


def make_db(latest, n_on_latest, n_total):
    """构造 daily 表：latest 日有 n_on_latest 只，总计 n_total 只。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE stock_kline_data (code TEXT, date TEXT, period TEXT)")
    rows = []
    for i in range(n_total):
        rows.append((f"sh60{i:04d}", "2026-09-20", "daily"))
    for i in range(n_on_latest):
        rows.append((f"sh60{i:04d}", latest, "daily"))
    conn.executemany("INSERT INTO stock_kline_data VALUES (?,?,?)", rows)
    return conn


class _FakeSvc:
    def __init__(self, latest, n_on_latest=100, n_total=100):
        conn = make_db(latest, n_on_latest, n_total)
        self.market = type("M", (), {"db": type("D", (), {
            "_connect": staticmethod(lambda: conn)})()})()


@pytest.fixture
def cal(monkeypatch):
    """固定日历常量、最新交易日与交易日判定。"""
    holder = {"latest": "2026-09-25", "trade_day": True}
    monkeypatch.setattr(cal_mod, "_CLOSE_HOUR", 15)
    monkeypatch.setattr(cal_mod, "_CLOSE_MINUTE", 30)
    monkeypatch.setattr(cal_mod, "get_latest_trade_date",
                        lambda: holder["latest"])
    monkeypatch.setattr(cal_mod, "is_trade_day",
                        lambda d=None: holder["trade_day"])
    return holder


def _freeze_now(monkeypatch, dt):
    class _FakeDT:
        @staticmethod
        def now():
            return dt

    monkeypatch.setattr(scan_mod, "datetime", _FakeDT)


def _spy_sync(monkeypatch, calls):
    def fake_sync_market(**kw):
        calls.append(kw)
        return {"synced": 2, "failed": 0}

    monkeypatch.setattr(sync_mod, "sync_market", fake_sync_market)


@pytest.mark.parametrize("db_latest,cover_n,now_str,expect", [
    # 已最新且覆盖足够：无论收盘与否都不同步
    ("2026-09-25", 100, "2026-09-25 10:00:00", "skip"),
    ("2026-09-25", 100, "2026-09-25 16:00:00", "skip"),
    # 落后一天 + 盘中 → 仅更新本次扫描股票
    ("2026-09-24", 100, "2026-09-25 10:00:00", "symbols"),
    # 落后一天 + 已收盘 → 全量
    ("2026-09-24", 100, "2026-09-25 16:00:00", "full"),
    # 恰在 15:30 整 → 已收盘 → 全量
    ("2026-09-24", 100, "2026-09-25 15:30:00", "full"),
    # 日期已到但覆盖率仅 2% → 视为不是最新 → 盘中走 symbols
    ("2026-09-25", 2, "2026-09-25 10:00:00", "symbols"),
    # 日期已到但覆盖率不足 + 已收盘 → 全量补齐
    ("2026-09-25", 2, "2026-09-25 16:00:00", "full"),
])
def test_ensure_scan_data_branches(monkeypatch, cal, db_latest, cover_n,
                                   now_str, expect):
    svc = _FakeSvc(db_latest, n_on_latest=cover_n, n_total=100)
    calls = []
    _spy_sync(monkeypatch, calls)
    _freeze_now(monkeypatch, datetime.fromisoformat(now_str))

    note = _ensure_scan_data(["sh600000", "sz000001"], svc, {}, True)
    if expect == "skip":
        assert calls == [] and "已最新" in note
    elif expect == "full":
        assert calls and "symbols" not in calls[0]
        assert "全量同步" in note
    else:
        assert calls and calls[0].get("symbols") == ["sh600000", "sz000001"]
        assert "仅更新本次扫描" in note


def test_auto_sync_disabled_skips_everything(monkeypatch, cal):
    """auto_sync=False：不查日历、不查库、不调 sync_market。"""
    svc = _FakeSvc("2020-01-01")
    monkeypatch.setattr(cal_mod, "get_latest_trade_date",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("不应查日历")))
    note = _ensure_scan_data(["sh600000"], svc, {}, False)
    assert "关闭" in note


def test_non_trade_day_treated_as_closed(monkeypatch, cal):
    """周末/节假日（钟点未到 15:30）→ 仍走全量同步。"""
    cal["trade_day"] = False
    svc = _FakeSvc("2026-09-24")
    calls = []
    _spy_sync(monkeypatch, calls)
    _freeze_now(monkeypatch, datetime(2026, 9, 26, 10, 0))   # 周六 10:00
    note = _ensure_scan_data(["sh600000"], svc, {}, True)
    assert calls and "symbols" not in calls[0]
    assert "全量同步" in note


def test_sync_failure_does_not_block(monkeypatch, cal):
    """sync_market 抛异常 → 返回说明字符串，不抛给扫描。"""
    svc = _FakeSvc("2026-09-24")
    monkeypatch.setattr(sync_mod, "sync_market",
                        lambda **kw: (_ for _ in ()).throw(
                            RuntimeError("数据源挂了")))
    _freeze_now(monkeypatch, datetime(2026, 9, 25, 16, 0))
    note = _ensure_scan_data(["sh600000"], svc, {}, True)
    assert "同步失败" in note and "继续扫描" in note
