"""W88：5579 只全量三浪收尾内存优化 + Web 后台扫描改独立进程。

全部用合成 K 线 / monkeypatch / 内存 SQLite，零网络、零生产库改动。
核心承诺：**结论口径零变化**——旧「items 全量路径」与新「流式 evaluated
路径」的 evaluate_portfolio 返回逐字段完全一致（含非候选票的 scale_down）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from mystery.core import wave3
from mystery.services import wave3_scan as ws
from mystery.services import scan_launcher as sl
from mystery.services import bg_task_log as btl


# ---------------------------------------------------------------------------
# 合成数据（与 W69/W75 回归同口径，80 根 > MIN_BARS=60）
# ---------------------------------------------------------------------------

def _series(closes, symbol="600150.SH"):
    from mystery.core.models import Bar, BarSeries
    n = len(closes)
    base = datetime(2025, 1, 1)
    bars = [
        Bar(dt=(base + timedelta(days=i)).strftime("%Y-%m-%d"),
            open=float(c), high=float(c) * 1.01, low=float(c) * 0.99,
            close=float(c), volume=1000.0, amount=float(c) * 1000.0,
            turnover=None, pct_chg=0.0)
        for i, c in enumerate(closes)
    ]
    return BarSeries(symbol=symbol, freq="1d", adjust="qfq",
                     bars=bars, source="db")


def _closes():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    seg3 = [11.0 + 0.004 * i for i in range(18)]
    return seg0 + seg1 + seg2 + seg3


class _FakeClient:
    db = object()

    def resample_bars(self, daily, freq):
        return daily


def _rows(n):
    return [{"symbol": f"{600000 + i}.SH", "name": f"票{i}",
             "score": 100.0 - i * 0.01, "advice": "持有",
             "trade_date": "2026-11-13", "true_resonance": False,
             "chip_low": False}
            for i in range(n)]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """隔离线上网页覆盖层 + env，保证基线不受生产配置污染。"""
    from mystery.services import runtime_settings as rts
    monkeypatch.setattr(rts, "SCAN_WAVE3_OVERRIDE_PATH",
                        tmp_path / "scan_wave3_override.json")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3", "1")
    for k in ("MYSTERY_SCAN_WAVE3_MAX", "MYSTERY_SCAN_WAVE3_STRICT",
              "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "MYSTERY_SCAN_WAVE3_AUTO_MAX"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# 1. 口径对拍：流式路径与旧全量路径逐字段一致（最重要）
# ---------------------------------------------------------------------------

def test_stream_path_identical_to_legacy_items_path():
    """新旧两条路径的 evaluate_portfolio 返回必须 JSON 完全一致。

    尤其覆盖「非候选票也被原地重算 scale_down / shares 置零」——若只把候选
    传给组合层，非候选票的 wave3 仓位字段会与旧口径不同。
    """
    recs = ws._records(_series(_closes()))
    items = [{"symbol": f"{600000 + i}.SH", "name": f"票{i}",
              "daily": recs, "weekly": recs, "monthly": recs}
             for i in range(4)]
    rets = {it["symbol"]: ws._returns(recs) for it in items}
    kw = dict(equity=1_000_000.0, risk_pct=0.01,
              returns_by_symbol=rets, strict=False)

    legacy = wave3.evaluate_portfolio(items, **kw)
    evaluated = [wave3.evaluate(it["daily"], it["weekly"], it["monthly"],
                                equity=kw["equity"], risk_pct=kw["risk_pct"],
                                symbol=it["symbol"], name=it["name"],
                                strict=kw["strict"])
                 for it in items]
    stream = wave3.evaluate_portfolio(evaluated=evaluated, **kw)

    assert json.dumps(stream, ensure_ascii=False, sort_keys=True) == \
        json.dumps(legacy, ensure_ascii=False, sort_keys=True)
    assert stream["scanned"] == 4


def test_stream_path_empty_is_same_as_legacy_empty():
    legacy = wave3.evaluate_portfolio([], equity=100_000)
    stream = wave3.evaluate_portfolio(evaluated=[], equity=100_000)
    assert json.dumps(stream, sort_keys=True) == \
        json.dumps(legacy, sort_keys=True)


# ---------------------------------------------------------------------------
# 2. 收尾流式：不再把完整 K 线堆进 items，且全量票都有三浪结论
# ---------------------------------------------------------------------------

def test_attach_streams_evaluations_without_items(monkeypatch):
    s = _series(_closes())
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: s)
    calls = {"evaluated": 0, "items": 0}
    orig = ws.wave3.evaluate_portfolio

    def _spy(*a, **k):
        calls["evaluated"] = len(k.get("evaluated") or [])
        calls["items"] = len(a[0]) if a and a[0] else 0
        return orig(*a, **k)

    monkeypatch.setattr(ws.wave3, "evaluate_portfolio", _spy)
    rows = _rows(120)
    summary = ws.attach_wave3_to_results(rows, db=object(),
                                         client=_FakeClient())
    assert summary["enabled"] is True
    assert summary["requested"] == 120 and summary["loaded"] == 120
    assert calls["evaluated"] == 120 and calls["items"] == 0
    assert all("wave3" in r for r in rows)
    assert all(r["wave3"]["verdict"] in ("候选", "观察", "回避") for r in rows)
    assert not any(r["wave3"]["verdict"] == "未评估" for r in rows)


def test_attach_stream_reports_tail_stage(monkeypatch):
    """收尾逐票上报心跳，流式评估不会 600s 静默被判中断。

    W88 保持 W75 的回调契约：恰好 picked 次、stage 恒为「收尾·三浪主升评估」；
    组合层信息只写日志，不额外发回调。
    """
    s = _series(_closes())
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: s)
    stages = []
    rows = _rows(6)
    ws.attach_wave3_to_results(
        rows, db=object(), client=_FakeClient(),
        progress_cb=lambda d, t, st: stages.append(st))
    assert len(stages) == 6
    assert all(st == "收尾·三浪主升评估" for st in stages)


def test_attach_stream_single_failure_is_isolated(monkeypatch):
    """单票取数失败仍不影响整轮，其余票照常有三浪结论。"""
    s = _series(_closes())

    def _flaky(db, sym):
        if sym.endswith("001.SH"):
            raise RuntimeError("模拟取数失败")
        return s

    monkeypatch.setattr(ws, "_daily_from_db", _flaky)
    rows = _rows(3)
    summary = ws.attach_wave3_to_results(rows, db=object(),
                                         client=_FakeClient())
    assert summary["failed"] == 1
    assert all("wave3" in r for r in rows)
    bad = [r for r in rows if r["symbol"].endswith("001.SH")]
    assert bad[0]["wave3"]["verdict"] == "回避"


# ---------------------------------------------------------------------------
# 3. AUTO_TRIGGER >= 池大小：不再误报「已自动限流」
# ---------------------------------------------------------------------------

def test_auto_trigger_ge_pool_is_not_misreported(monkeypatch):
    """线上 5600/5600 覆盖层：池 600 只，不应标 auto_limited=True。"""
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: None)
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "5600")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_MAX", "5600")
    rows = _rows(600)
    summary = ws.attach_wave3_to_results(rows, db=object(),
                                         client=_FakeClient())
    assert summary["auto_limited"] is False
    assert summary["cap_reason"] == "none"
    assert summary["cap"] is None
    assert summary["requested"] == 600


def test_explicit_zero_forces_full_pool(monkeypatch):
    """用户坚持「5579 只全部要三浪结论」：MAX=0 显式强制全量。"""
    s = _series(_closes())
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: s)
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_MAX", "0")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "5600")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_MAX", "5600")
    rows = _rows(600)
    summary = ws.attach_wave3_to_results(rows, db=object(),
                                         client=_FakeClient())
    assert summary["cap_reason"] == "explicit"
    assert summary["loaded"] == 600
    assert not any(r["wave3"]["verdict"] == "未评估" for r in rows)


# ---------------------------------------------------------------------------
# 4. 独立进程启动器
# ---------------------------------------------------------------------------

def test_build_scan_argv_market():
    argv = sl.build_scan_argv("market", "abcd1234", exe="/venv/bin/czsc-mi")
    assert argv[:2] == ["/venv/bin/czsc-mi", "scan"]
    assert "--resume" in argv and "--market" in argv
    assert "--bg-task-id" in argv and "abcd1234" in argv
    assert "--watchlist" not in argv


def test_build_scan_argv_watchlist():
    argv = sl.build_scan_argv("watchlist", "ef567890", exe="czsc-mi")
    assert "--watchlist" in argv and "--market" not in argv


def test_build_scan_argv_rejects_unknown():
    with pytest.raises(ValueError):
        sl.build_scan_argv("sector", "x")


def test_launch_uses_systemd_transient_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(sl, "systemd_available", lambda: True)
    monkeypatch.setattr(sl.shutil, "which", lambda x: "/usr/bin/" + x)
    captured = {}

    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(sl.subprocess, "Popen", _popen)
    info = sl.launch("/tmp/prod.db", "abcd1234", "market",
                     exe="/venv/bin/czsc-mi", log_base=tmp_path, cwd="/tmp")
    assert info["mode"] == "systemd-run" and info["pid"] == 4321
    assert "--unit=czsc-mi-scan-abcd1234" in captured["cmd"]
    assert "--collect" in captured["cmd"]
    assert "MYSTERY_DB_PATH=/tmp/prod.db" in captured["cmd"][4]
    assert captured["kw"]["start_new_session"] is True
    assert "exec /venv/bin/czsc-mi scan --resume" in captured["cmd"][-1]
    assert captured["kw"]["env"]["MYSTERY_DB_PATH"] == "/tmp/prod.db"


def test_launch_falls_back_to_popen(monkeypatch, tmp_path):
    monkeypatch.setattr(sl, "systemd_available", lambda: False)
    captured = {}

    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return SimpleNamespace(pid=999)

    monkeypatch.setattr(sl.subprocess, "Popen", _popen)
    info = sl.launch("/tmp/prod.db", "abcd1234", "watchlist",
                     exe="czsc-mi", log_base=tmp_path, cwd="/tmp")
    assert info["mode"] == "popen"
    assert captured["cmd"] == sl.build_scan_argv("watchlist", "abcd1234",
                                                 exe="czsc-mi")
    assert captured["kw"]["start_new_session"] is True
    assert info["log"].endswith("scan_abcd1234.log")


def test_cli_executable_not_hardcoded():
    """AGENTS.md §0：业务代码不得写本机绝对路径。"""
    import pathlib
    src = pathlib.Path(sl.__file__).read_text(encoding="utf-8")
    assert "/home/ai/" not in src


# ---------------------------------------------------------------------------
# 5. Web 按钮：不再起 daemon 线程，台账留痕
# ---------------------------------------------------------------------------

def test_bg_launch_process_path_does_not_start_thread(monkeypatch, tmp_path):
    from mystery.apps.web.views import components as comp
    db = str(tmp_path / "web.db")
    store = {"lock": threading.Lock(), "tasks": {}}
    monkeypatch.setattr(comp, "_bg_store", lambda: store)
    monkeypatch.setattr(comp, "_service", lambda: SimpleNamespace(
        market=SimpleNamespace(db=SimpleNamespace(db_path=db))))
    launched = {}

    def _fake_launch(db_path, tid, kind):
        launched.update(db=db_path, tid=tid, kind=kind)
        return {"mode": "test"}

    monkeypatch.setattr(sl, "launch", _fake_launch)
    threads = []
    monkeypatch.setattr(comp.threading, "Thread",
                        lambda **kw: threads.append(kw)
                        or SimpleNamespace(start=lambda: None))
    tid = comp._bg_launch("全部股票", scan_kind="market", scan_type="market")
    assert launched["kind"] == "market" and launched["tid"] == tid
    assert launched["db"] == db
    assert not threads, "W88 独立进程路径不应再起 Web 内 daemon 线程"
    logs = btl.recent(db)
    assert logs and logs[0]["id"] == tid
    assert logs[0]["status"] == "running"
    assert logs[0]["scan_type"] == "market"


def test_bg_launch_failed_launch_records_error(monkeypatch, tmp_path):
    from mystery.apps.web.views import components as comp
    db = str(tmp_path / "web.db")
    store = {"lock": threading.Lock(), "tasks": {}}
    monkeypatch.setattr(comp, "_bg_store", lambda: store)
    monkeypatch.setattr(comp, "_service", lambda: SimpleNamespace(
        market=SimpleNamespace(db=SimpleNamespace(db_path=db))))

    def _boom(*a, **k):
        raise OSError("systemd-run 不可用")

    monkeypatch.setattr(sl, "launch", _boom)
    tid = comp._bg_launch("全部股票", scan_kind="market")
    log = btl.recent(db)[0]
    assert log["id"] == tid and log["status"] == "error"
    assert "启动独立扫描进程失败" in log["error"]


# ---------------------------------------------------------------------------
# 6. CLI：--market / --bg-task-id 与台账终态
# ---------------------------------------------------------------------------

def test_cli_scan_help_has_w88_flags(capsys, monkeypatch):
    from mystery.apps import cli
    monkeypatch.setattr(cli, "_ensure_default_ths_env", lambda: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["scan", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--market" in out and "--bg-task-id" in out


def test_cli_market_keeps_full_universe(monkeypatch, tmp_path):
    """--market 取消 limit 默认 100 防呆，全市场 5579 只不被截到 100。"""
    from mystery.apps import cli
    seen = {}

    def _fake_scan_market(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr("mystery.services.scan.scan_market", _fake_scan_market)
    monkeypatch.setattr(cli, "_print_db_fingerprint", lambda: None)
    monkeypatch.setattr(cli, "_suffix_orphans_hard_fail", lambda: 0)
    args = SimpleNamespace(limit=None, watchlist=False, market=True,
                           min_score=None, cfg={}, no_persist=True,
                           board=None, force=False, no_auto_sync=True,
                           resume=True, report=False, signal=None)
    rc = cli._cmd_scan_body(args)
    assert rc == 0
    assert seen["limit"] is None


def test_cli_bg_task_writes_terminal(monkeypatch, tmp_path):
    from mystery.apps import cli
    db = str(tmp_path / "cli.db")
    btl.log_start(db, "tid88", "全部股票", scan_type="market")
    monkeypatch.setattr(btl, "db_path_of", lambda cfg=None: db)

    def _fake_body(args, *, progress_cb=None, job_holder=None,
                   result_holder=None):
        progress_cb(10, 10, "收尾·写入扫描结果")
        if job_holder is not None:
            job_holder.append(777)
        if result_holder is not None:
            result_holder.append(10)
        return 0

    monkeypatch.setattr(cli, "_cmd_scan_body", _fake_body)
    rc = cli._cmd_scan(SimpleNamespace(bg_task_id="tid88", cfg={}))
    assert rc == 0
    log = btl.recent(db)[0]
    assert log["status"] == "done"
    assert log["job_id"] == 777 and log["n_results"] == 10


def test_cli_bg_task_error_terminal(monkeypatch, tmp_path):
    from mystery.apps import cli
    db = str(tmp_path / "cli.db")
    btl.log_start(db, "tid99", "全部股票")
    monkeypatch.setattr(btl, "db_path_of", lambda cfg=None: db)

    def _boom(*a, **k):
        raise RuntimeError("模拟 W25 并发锁拒绝")

    monkeypatch.setattr(cli, "_cmd_scan_body", _boom)
    with pytest.raises(RuntimeError):
        cli._cmd_scan(SimpleNamespace(bg_task_id="tid99", cfg={}))
    log = btl.recent(db)[0]
    assert log["status"] == "error"
    assert "并发锁拒绝" in log["error"]
