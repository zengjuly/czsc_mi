"""W75：修复「后台扫描任务一直卡在最后」——超大池自动限流 + 收尾进度可见。

全部用合成数据 + monkeypatch，零网络、零写库、零生产库/自选股改动。
根因：W69 三浪附加在逐票扫描 done=total 后，对全市场 5580 只逐只查库
+ resample + 三浪评估 + O(C²) 相关性矩阵，耗时十几分钟、内存数 GB，且收尾
阶段不更新进度心跳 → 页面永远停在 100%「运行中」。
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from mystery.services import wave3_scan as ws
from mystery.services import scan as scan_mod
from mystery.apps.web.views import components as comp


class _FakeClient:
    db = object()

    def resample_bars(self, daily, freq):
        return daily


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「未显式设置 MAX」的干净状态出发。"""
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3", "1")
    for k in ("MYSTERY_SCAN_WAVE3_MAX", "MYSTERY_SCAN_WAVE3_STRICT",
              "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "MYSTERY_SCAN_WAVE3_AUTO_MAX"):
        monkeypatch.delenv(k, raising=False)
    # 取数直接返回 None → items 空、矩阵空，仅验证限流/调度逻辑，秒级完成
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: None)


def _rows(n, score_base=100.0):
    return [{"symbol": f"{600000 + i}.SH", "name": f"票{i}",
             "score": score_base - i * 0.01, "advice": "持有",
             "trade_date": "2026-11-08", "true_resonance": False}
            for i in range(n)]


def _attach(rows, **kw):
    return ws.attach_wave3_to_results(
        rows, db=object(), client=_FakeClient(), **kw)


# ---------------------------------------------------------------------------
# 1. 超大池自动限流（核心修复）
# ---------------------------------------------------------------------------

def test_big_pool_auto_limited():
    """600 只 > 默认 trigger 500 且未设 MAX → 自动限到前 200 只。"""
    rows = _rows(600)
    summary = _attach(rows)
    assert summary["enabled"] is True
    assert summary["auto_limited"] is True
    assert summary["cap_reason"] == "auto"
    assert summary["cap"] == 200
    assert summary["pool_size"] == 600
    assert summary["requested"] == 200
    verdicts = [r.get("wave3", {}).get("verdict") for r in rows]
    assert verdicts.count("未评估") == 400
    assert all("W75 自动限流" in r["wave3"].get("reason", "")
               for r in rows if r.get("wave3", {}).get("verdict") == "未评估")
    # 每一行都挂了同一份组合摘要（旧展示逻辑不破）
    assert all(r.get("wave3_portfolio", {}).get("auto_limited") is True
               for r in rows)


def test_small_pool_not_limited():
    """自选股等小池(<500)行为完全不变：全量评估、auto_limited=False。"""
    rows = _rows(168)
    summary = _attach(rows)
    assert summary["auto_limited"] is False
    assert summary["cap_reason"] == "none"
    assert summary["cap"] is None
    assert summary["requested"] == 168
    assert not any(r.get("wave3", {}).get("verdict") == "未评估" for r in rows)


# ---------------------------------------------------------------------------
# 2. 显式 env 完全尊重（可回退/可强制全量）
# ---------------------------------------------------------------------------

def test_explicit_max_wins(monkeypatch):
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_MAX", "5")
    rows = _rows(600)
    summary = _attach(rows)
    assert summary["auto_limited"] is False
    assert summary["cap_reason"] == "explicit"
    assert summary["cap"] == 5 and summary["requested"] == 5
    assert sum(1 for r in rows
               if r.get("wave3", {}).get("verdict") == "未评估") == 595


def test_explicit_zero_forces_full(monkeypatch):
    """显式 MAX=0 = 强制全量（回退到 W69 旧行为），自动限流让位。"""
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_MAX", "0")
    rows = _rows(600)
    summary = _attach(rows)
    assert summary["auto_limited"] is False
    assert summary["cap_reason"] == "explicit"
    assert summary["cap"] is None and summary["requested"] == 600
    assert not any(r.get("wave3", {}).get("verdict") == "未评估" for r in rows)


def test_custom_trigger_and_cap(monkeypatch):
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "10")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_MAX", "3")
    rows = _rows(20)
    summary = _attach(rows)
    assert summary["auto_limited"] is True
    assert summary["cap"] == 3 and summary["requested"] == 3
    assert sum(1 for r in rows
               if r.get("wave3", {}).get("verdict") == "未评估") == 17


def test_trigger_boundary_not_limited(monkeypatch):
    """恰好等于 trigger 不触发（> 才触发），边界稳定。"""
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "500")
    rows = _rows(500)                 # == trigger，不应触发
    s = _attach(rows)
    assert s["auto_limited"] is False
    assert s["requested"] == 500


# ---------------------------------------------------------------------------
# 3. 收尾阶段进度回调（不再停在 100% 空转）
# ---------------------------------------------------------------------------

def test_progress_cb_reports_tail_stage():
    rows = _rows(600)
    calls = []
    _attach(rows, progress_cb=lambda d, t, stage="": calls.append((d, t, stage)))
    assert len(calls) == 200                      # 只报限流后的 200 只
    assert all(stage == "收尾·三浪主升评估" for _, _, stage in calls)
    assert calls[0][0] == 0 and calls[0][1] == 200
    assert calls[-1][0] == 199


def test_progress_cb_exception_is_isolated():
    """回调抛异常不得中断三浪附加（附加策略不影响既有扫描）。"""
    rows = _rows(3)
    def _boom(*a, **k):
        raise RuntimeError("模拟前端回调异常")
    summary = _attach(rows, progress_cb=_boom)
    assert summary["enabled"] is True           # 未因回调异常降级


def test_progress_cb_none_no_error():
    rows = _rows(3)
    assert _attach(rows, progress_cb=None)["enabled"] is True


# ---------------------------------------------------------------------------
# 4. summary_line 体现自动限流（日报/Web/CLI 可观测）
# ---------------------------------------------------------------------------

def test_summary_line_shows_auto_limit():
    rows = _rows(600)
    _attach(rows)
    line = ws.wave3_summary_line(rows)
    assert "超大池自动限流" in line and "200/600" in line


def test_summary_line_no_limit_note_on_small_pool():
    rows = _rows(3)
    _attach(rows)
    assert "超大池自动限流" not in ws.wave3_summary_line(rows)


# ---------------------------------------------------------------------------
# 5. scan.py 收尾透传 + 心跳不倒退
# ---------------------------------------------------------------------------

def test_persist_or_return_forwards_progress_cb(monkeypatch):
    captured = {}

    def _fake_attach(rows, **kw):
        captured["progress_cb"] = kw.get("progress_cb")
        captured["db"] = kw.get("db")
        captured["client"] = kw.get("client")

    monkeypatch.setattr(scan_mod, "_attach_wave3", _fake_attach)
    svc = SimpleNamespace(market=SimpleNamespace(db=object()))
    rows = _rows(2)
    stage_calls = []
    cb = lambda d, t, stage="": stage_calls.append((d, t, stage))
    out = scan_mod._persist_or_return(rows, 0, True, svc, None,
                                      progress_cb=cb)
    assert out is rows
    assert captured["progress_cb"] is cb
    assert ("收尾·写入扫描结果" in [c[2] for c in stage_calls])


def test_persist_or_return_no_progress_cb_still_ok(monkeypatch):
    """未传 progress_cb（CLI/日报）时行为不变，不外抛。"""
    monkeypatch.setattr(scan_mod, "_attach_wave3", lambda rows, **kw: None)
    svc = SimpleNamespace(market=SimpleNamespace(db=object()))
    rows = _rows(2)
    assert scan_mod._persist_or_return(rows, 0, True, svc, None) is rows


def test_scan_cb_keeps_full_progress_on_tail(monkeypatch):
    """收尾阶段心跳保持满进度 + 阶段 label，不回退到 0/N。"""
    calls = []
    monkeypatch.setattr(scan_mod, "report_progress",
                        lambda db, name, d, t, label="", force=False:
                        calls.append((d, t, label)))
    monkeypatch.setattr(scan_mod, "clear_progress", lambda *a, **k: None)
    monkeypatch.setattr(scan_mod, "MysteryDB",
                        lambda **k: SimpleNamespace(db_path="/tmp/x.db"))
    monkeypatch.setattr(scan_mod, "_hold_scan_lock",
                        lambda *a, **k: contextlib.nullcontext())

    def _fake_impl(**kw):
        cb = kw["progress_cb"]
        cb(5580, 5580)                              # 逐票完成
        cb(0, 200, "收尾·三浪主升评估")             # 收尾独立刻度
        cb(200, 200, "收尾·写入扫描结果")
        return []

    monkeypatch.setattr(scan_mod, "_scan_market_impl", _fake_impl)
    scan_mod.scan_market(no_persist=True)
    # 逐票：d=5580,t=5580；收尾：d=t=200/200（满进度），label 为阶段文字
    assert calls[0] == (5580, 5580, "")
    tail = [c for c in calls if c[2]]
    assert tail and all(d == t for d, t, _ in tail)
    assert all("收尾" in label for _, _, label in tail)


# ---------------------------------------------------------------------------
# 6. components 僵尸防护（常量 + cb 兼容 3 参数）
# ---------------------------------------------------------------------------

def test_bg_stale_constant():
    assert comp._BG_STALE_SECONDS >= 300      # 至少 5 分钟，不误判正常扫描


def test_bg_launch_cb_accepts_stage(monkeypatch):
    """_bg_launch 的 cb 支持 3 参数（stage），且记录 updated 时间戳。"""
    import threading
    import time
    store = {"lock": threading.Lock(), "tasks": {}}
    monkeypatch.setattr(comp, "_bg_store", lambda: store)

    def _fn(cb, holder):
        cb(10, 10, "收尾·三浪主升评估")
        return []

    tid = comp._bg_launch("全部股票", _fn)
    # 等后台线程跑完（轮询 store 状态）
    for _ in range(100):
        t = store["tasks"].get(tid)
        if t and t["status"] == "done":
            break
        time.sleep(0.01)
    t = store["tasks"].get(tid)
    assert t and t["status"] == "done"
    assert t["stage"] == "收尾·三浪主升评估"
    assert t["updated"] >= t["created"]
