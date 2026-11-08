"""W69：三浪主升结论合并进扫描结果（回归测试）。

全部用合成 K 线 + monkeypatch，零网络、零写库、零生产自选股改动。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from mystery.core import wave3
from mystery.services import wave3_scan as ws
from mystery.services import scan as scan_mod


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------

def _series(closes, vols=None, symbol="600150.SH"):
    """构造本地 BarSeries（与 W68 只读取数同结构）。"""
    from mystery.core.models import Bar, BarSeries
    n = len(closes)
    vols = vols or [1000.0] * n
    base = datetime(2025, 1, 1)
    bars = [
        Bar(dt=(base + timedelta(days=i)).strftime("%Y-%m-%d"),
            open=float(c), high=float(c) * 1.01, low=float(c) * 0.99,
            close=float(c), volume=float(v), amount=float(c) * float(v),
            turnover=None, pct_chg=0.0)
        for i, (c, v) in enumerate(zip(closes, vols))
    ]
    return BarSeries(symbol=symbol, freq="1d", adjust="qfq",
                     bars=bars, source="db")


def _closes():
    """80 根：横盘 → 第一浪 → 第二浪回调 → 尾部企稳（够 MIN_BARS=60）。"""
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    seg3 = [11.0 + 0.004 * i for i in range(18)]
    return seg0 + seg1 + seg2 + seg3


class _FakeClient:
    db = object()

    def resample_bars(self, daily, freq):
        return daily


@pytest.fixture(autouse=True)
def _isolate_wave3_override(monkeypatch, tmp_path):
    """W76：隔离网页覆盖层，保证 W69 回归基线不受用户网页配置影响。"""
    from mystery.services import runtime_settings as rts
    monkeypatch.setattr(rts, "SCAN_WAVE3_OVERRIDE_PATH",
                        tmp_path / "scan_wave3_override.json")


@pytest.fixture
def fake_kline(monkeypatch):
    """把日 K 取数替换为合成序列，避免触碰生产库。"""
    s = _series(_closes())
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: s)
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3", "1")
    monkeypatch.delenv("MYSTERY_SCAN_WAVE3_MAX", raising=False)
    monkeypatch.delenv("MYSTERY_SCAN_WAVE3_STRICT", raising=False)
    return s


def _rows(n=3):
    return [{"symbol": f"{600000 + i}.SH", "name": f"票{i}",
             "score": 80.0 - i, "advice": "持有", "trade_date": "2026-11-05",
             "true_resonance": i == 0, "chip_low": False}
            for i in range(n)]


def _attach(rows, **kw):
    return ws.attach_wave3_to_results(rows, db=object(), client=_FakeClient(),
                                      **kw)


# ---------------------------------------------------------------------------
# 附加字段
# ---------------------------------------------------------------------------

def test_attach_adds_wave3_fields(fake_kline):
    rows = _rows()
    summary = _attach(rows)
    assert summary["enabled"] is True
    assert summary["scanned"] == 3
    for r in rows:
        w = r["wave3"]
        for key in ("verdict", "phase", "position", "resonance", "trend_ok",
                    "zone", "stage", "stage_name", "hard", "quality",
                    "failed_hard", "entry", "stop", "lots", "shares",
                    "risk_amount", "scale_down", "reason"):
            assert key in w, f"缺字段 {key}"
        assert w["verdict"] in ("候选", "观察", "回避")
        assert set(w["zone"]) == {"月线", "周线", "日线"}
        assert r["wave3_portfolio"]["enabled"] is True


def test_attach_portfolio_summary_keys(fake_kline):
    rows = _rows()
    summary = _attach(rows)
    p = rows[0]["wave3_portfolio"]
    for key in ("scanned", "candidates", "equity", "risk_pct", "sum_risk",
                "portfolio_risk", "total_risk_pct", "scale_down",
                "high_corr_pairs", "assumed_pairs", "matrix_psd",
                "max_total_risk_pct", "note", "disclaimer"):
        assert key in p, f"组合摘要缺字段 {key}"
    assert wave3.DISCLAIMER == p["disclaimer"]
    assert summary["requested"] == 3 and summary["loaded"] == 3


def test_attach_payload_json_serializable(fake_kline):
    rows = _rows()
    _attach(rows)
    restored = json.loads(json.dumps(rows, ensure_ascii=False))
    assert all("wave3" in r and "wave3_portfolio" in r for r in restored)
    assert restored[0]["wave3"]["verdict"] == rows[0]["wave3"]["verdict"]


def test_attach_does_not_change_existing_fields(fake_kline):
    """W69 只附加字段，不改综合评分 / 建议 / 三类信号。"""
    rows = _rows()
    before = [{k: r.get(k) for k in ("symbol", "score", "advice",
                                     "true_resonance", "chip_low")}
              for r in rows]
    _attach(rows)
    after = [{k: r.get(k) for k in ("symbol", "score", "advice",
                                    "true_resonance", "chip_low")}
             for r in rows]
    assert before == after


def test_attach_compact_payload_is_small(fake_kline):
    """精简字段必须显著小于完整 assessment（否则全市场 payload 膨胀）。"""
    rows = _rows(1)
    _attach(rows)
    compact_bytes = len(json.dumps(rows[0]["wave3"], ensure_ascii=False))
    a = wave3.evaluate(ws._records(_series(_closes())),
                       ws._records(_series(_closes())),
                       ws._records(_series(_closes())),
                       equity=1e6, risk_pct=0.01)
    full_bytes = len(json.dumps(a, ensure_ascii=False))
    assert compact_bytes < 1200
    assert compact_bytes < full_bytes / 2


def test_attach_empty_results_returns_disabled(fake_kline):
    assert _attach([])["enabled"] is False


# ---------------------------------------------------------------------------
# 开关 / 边界 / 降级
# ---------------------------------------------------------------------------

def test_attach_disabled_env(fake_kline, monkeypatch):
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3", "0")
    rows = _rows()
    summary = _attach(rows)
    assert summary["enabled"] is False
    assert all("wave3" not in r for r in rows)


def test_attach_max_limit_marks_unevaluated(fake_kline, monkeypatch):
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_MAX", "1")
    rows = _rows()
    summary = _attach(rows)
    assert summary["requested"] == 1 and summary["loaded"] == 1
    verdicts = [r["wave3"]["verdict"] for r in rows]
    assert verdicts.count("未评估") == 2
    assert all(r["wave3"]["data_ok"] is False
               for r in rows if r["wave3"]["verdict"] == "未评估")
    assert all(r["wave3_portfolio"]["enabled"] is True for r in rows)


def test_attach_single_symbol_failure_degrades(fake_kline, monkeypatch):
    s = _series(_closes())

    def _flaky(db, sym):
        if sym.endswith("001.SH"):
            raise RuntimeError("模拟取数失败")
        return s

    monkeypatch.setattr(ws, "_daily_from_db", _flaky)
    rows = _rows()
    summary = _attach(rows)
    assert summary["failed"] == 1
    bad = [r for r in rows if r["symbol"].endswith("001.SH")]
    assert bad and bad[0]["wave3"]["verdict"] == "回避"
    assert bad[0]["wave3"]["data_ok"] is False
    assert all("wave3" in r for r in rows)


def test_attach_no_kline_marks_data_insufficient(fake_kline, monkeypatch):
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: None)
    rows = _rows()
    summary = _attach(rows)
    assert summary["loaded"] == 0 and summary["failed"] == 3
    assert all(r["wave3"]["data_ok"] is False for r in rows)
    assert all(r["wave3_portfolio"]["enabled"] is True for r in rows)


def test_attach_global_failure_is_isolated(fake_kline, monkeypatch):
    """评估层整体抛异常时，既有扫描结果保持未附加状态、不外抛。"""
    monkeypatch.setattr(ws.wave3, "evaluate_portfolio",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("模拟整体失败")))
    rows = _rows()
    summary = _attach(rows)
    assert summary["enabled"] is False
    assert all("wave3" not in r for r in rows)


# ---------------------------------------------------------------------------
# 组合摘要文本（CLI / 日报 / Web 共用）
# ---------------------------------------------------------------------------

def test_summary_line_old_data_is_empty():
    assert ws.wave3_summary_line([{"symbol": "600150.SH"}]) == ""
    assert ws.wave3_summary_line([]) == ""


def test_summary_line_contains_risk_and_disclaimer(fake_kline):
    rows = _rows()
    _attach(rows)
    line = ws.wave3_summary_line(rows)
    assert "三浪主升" in line and "组合总风险" in line
    assert "占权益" in line and "仓位缩放" in line
    assert wave3.DISCLAIMER in line


def test_summary_line_survives_signal_filter(fake_kline):
    """摘要挂在每一行，按信号过滤后仍能读到。"""
    rows = _rows()
    _attach(rows)
    filtered = [r for r in rows if r.get("true_resonance")]
    assert ws.wave3_summary_line(filtered) == ws.wave3_summary_line(rows)


# ---------------------------------------------------------------------------
# 日报列（旧任务缺字段显示 '-'）
# ---------------------------------------------------------------------------

def test_excel_flat_wave3_columns_default():
    from mystery.apps.reports.excel_report import _flat
    row = _flat({"symbol": "600150.SH", "score": 70, "name": "X"})
    for key in ("w3_verdict", "w3_phase", "w3_position", "w3_resonance",
                "w3_hard", "w3_stage", "w3_entry", "w3_stop", "w3_lots",
                "w3_scale"):
        assert row[key] == "-"


def test_excel_flat_wave3_columns_filled():
    from mystery.apps.reports.excel_report import _flat
    row = _flat({"symbol": "600150.SH", "score": 70,
                 "wave3": {"verdict": "候选", "phase": "第三浪突破",
                           "position": "月线低位/周线中位/日线高位",
                           "resonance": "三周期同向向上", "hard": "7/7",
                           "stage": 7, "entry": 12.3, "stop": 11.1,
                           "lots": 30, "scale_down": 0.8}})
    assert row["w3_verdict"] == "候选" and row["w3_hard"] == "7/7"
    assert row["w3_stage"] == 7 and row["w3_entry"] == 12.3
    assert row["w3_lots"] == 30 and row["w3_scale"] == 0.8


# ---------------------------------------------------------------------------
# 接入点：写库前必须调用附加
# ---------------------------------------------------------------------------

def test_persist_or_return_calls_attach(monkeypatch):
    called = []
    monkeypatch.setattr(scan_mod, "_attach_wave3",
                        lambda rows, **kw: called.append(len(rows)))
    svc = SimpleNamespace(market=SimpleNamespace(db=object()))
    rows = _rows(2)
    out = scan_mod._persist_or_return(rows, 0, True, svc, None)
    assert called == [2]
    assert out is rows


def test_write_scan_batch_persists_wave3_in_payload(fake_kline):
    """W69 核心承诺：三浪字段随 payload_json 真正落库，重读即可见。

    用内存 SQLite，不触碰生产库；结束后表随连接销毁。
    """
    import sqlite3
    rows = _rows(2)
    _attach(rows)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE scan_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "trade_date TEXT, started_at TEXT, finished_at TEXT, "
                 "n_ok INTEGER, n_fail INTEGER, scan_type TEXT)")
    conn.execute("CREATE TABLE scan_results (job_id INTEGER, symbol TEXT, "
                 "trade_date TEXT, score REAL, true_resonance INTEGER, "
                 "vap_atr_break INTEGER, chip_low INTEGER, payload_json TEXT, "
                 "PRIMARY KEY (job_id, symbol))")

    class _NoClose:
        def __init__(self, c):
            self._c = c

        def __getattr__(self, name):
            return getattr(self._c, name)

        def close(self):
            pass  # _write_scan_batch 的 finally 会 close，这里保留连接供断言

    fake_db = SimpleNamespace(_connect=lambda: _NoClose(conn))
    job_id = scan_mod._write_scan_batch(fake_db, rows, 0, "2026-11-05",
                                        scan_type="w69_test")
    payloads = conn.execute(
        "SELECT payload_json FROM scan_results WHERE job_id=?",
        (job_id,)).fetchall()
    assert len(payloads) == 2
    for (p,) in payloads:
        d = json.loads(p)
        assert "wave3" in d and d["wave3"]["verdict"] in (
            "候选", "观察", "回避")
        assert d["wave3_portfolio"]["enabled"] is True
    conn.close()
