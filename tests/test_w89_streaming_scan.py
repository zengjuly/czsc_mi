"""W89：阶段化流式全市场扫描（大盘→板块→个股→组合，分批落盘、内存有界）。

全部用合成 K 线 / monkeypatch / 临时 SQLite，零网络、零生产库改动。
核心承诺：
1. 严格顺序：大盘 → 板块 → 个股 → 组合层；
2. 每 10 只一批 ``INSERT OR REPLACE`` + COMMIT + JSONL fsync，批后释放；
3. **执行期不累积全量完整结果**（批对象清空、内存只留 KB 级 assessment）；
4. 组合层最终落库结论与旧 ``attach_wave3_to_results`` 逐字段一致；
5. 断点续跑按已落盘 symbol 幂等跳过，不重复写库、不重复分析。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from mystery.core import wave3
from mystery.services import streaming_scan as ss
from mystery.services import wave3_scan as ws
from mystery.services import scan as scan_mod


# ---------------------------------------------------------------------------
# 合成数据（与 W69/W75/W88 回归同口径，80 根 > MIN_BARS=60）
# ---------------------------------------------------------------------------

def _series(closes, symbol="600150.SH"):
    from mystery.core.models import Bar, BarSeries
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


def _rows(n):
    return [{"symbol": f"{600000 + i}.SH", "name": f"票{i}",
             "score": 100.0 - i * 0.01, "advice": "持有",
             "trade_date": "2026-11-13", "true_resonance": False,
             "chip_low": False}
            for i in range(n)]


DDL = """
CREATE TABLE scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT, started_at TEXT, finished_at TEXT,
  n_ok INTEGER DEFAULT 0, n_fail INTEGER DEFAULT 0, scan_type TEXT,
  scope TEXT, universe_n INTEGER, limit_n INTEGER, duration_sec REAL,
  wave3_enabled INTEGER, wave3_mode TEXT
);
CREATE TABLE scan_results (
  job_id INTEGER, symbol TEXT, trade_date TEXT, score REAL,
  true_resonance INTEGER, vap_atr_break INTEGER, chip_low INTEGER,
  payload_json TEXT, PRIMARY KEY (job_id, symbol)
);
"""


class _FakeDB:
    """最小 MysteryDB 替身：真实临时 SQLite，零生产库改动。"""

    def __init__(self, path):
        self.db_path = str(path)
        c = sqlite3.connect(self.db_path)
        c.executescript(DDL)
        c.commit()
        c.close()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=30)

    def get_primary_industries(self, codes):
        return {}

    def get_financials(self, codes):
        return {}


class _FakeClient:
    def __init__(self, db):
        self.db = db

    def resample_bars(self, daily, freq):
        return daily


def _make_svc(db):
    market = _FakeClient(db)
    return SimpleNamespace(market=market)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """隔离线上网页覆盖层 + env，保证基线不受生产配置污染。"""
    from mystery.services import runtime_settings as rts
    monkeypatch.setattr(rts, "SCAN_WAVE3_OVERRIDE_PATH",
                        tmp_path / "scan_wave3_override.json")
    monkeypatch.setattr(rts, "SECTOR_OVERRIDE_PATH",
                        tmp_path / "sector_override.json", raising=False)
    monkeypatch.setenv("MYSTERY_SCAN_WORKERS", "1")
    monkeypatch.setenv("MYSTERY_SCAN_STREAM_BATCH", "10")
    monkeypatch.setenv("MYSTERY_SCAN_STREAM_MIN", "1")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3", "1")
    # 线上 W88 起的显式全量口径（MAX=0）：流式全量三浪的前提。
    # 若未显式设置，600 只会触发默认自动限流而回退旧路径（见下方用例）。
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_MAX", "0")
    for k in ("MYSTERY_SCAN_WAVE3_STRICT",
              "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "MYSTERY_SCAN_WAVE3_AUTO_MAX",
              "MYSTERY_SCAN_STREAM_SHARD", "MYSTERY_SCAN_SHARD_DIR",
              "MYSTERY_SCAN_STREAMING"):
        monkeypatch.delenv(k, raising=False)


def _patch_common(monkeypatch, db, tmp_path, n):
    """装配：合成逐票分析、合成日K、空指数/板块阶段、真实临时库。"""
    monkeypatch.setattr(ss._scan, "_scan_thread_worker",
                        lambda code, svc, det, ms, cache=None: (
                            {**_rows(1)[0], "symbol": code,
                             "name": f"票{code}"}, False))
    monkeypatch.setattr(ws, "_daily_from_db",
                        lambda db_, sym: _series(_closes(), sym))
    monkeypatch.setattr(ss._Shards, "_prune", staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("MYSTERY_SCAN_SHARD_DIR", str(tmp_path / "shards"))
    # 阶段①②由独立用例验证顺序；此处隔离，专注个股+组合层
    monkeypatch.setattr(ss.StreamingScan, "stage_indexes",
                        lambda self: self._order.append("index"))
    monkeypatch.setattr(ss.StreamingScan, "stage_sectors",
                        lambda self: self._order.append("sector"))
    # 展示读回：直接查临时库，绕过 AnalysisService（避免连生产库）
    def _read(job_id, cfg=None, signal=None):
        conn = db._connect()
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM scan_results WHERE job_id=?",
                (job_id,)).fetchall()]
        finally:
            conn.close()
    monkeypatch.setattr(ss._scan, "scan_results_of", _read)
    # run() 内是延迟 import，必须 patch 源模块
    from mystery.adapters import calendar as _cal
    monkeypatch.setattr(_cal, "get_latest_trade_date",
                        lambda: "2026-11-13", raising=False)


def _pipeline(db, n, *, resume=False):
    codes = [f"{600000 + i}.SH" for i in range(n)]
    p = ss.StreamingScan(
        svc=_make_svc(db), codes=codes, scan_type="market", cfg={},
        meta=None, progress_cb=None, include_detail=True, min_score=None,
        resume=resume, job_holder=[])
    p._order = []
    return p


# ---------------------------------------------------------------------------
# 1. 分流条件：只有「全市场 + 持久化 + 大池 + 三浪全量」走流式
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw,expected", [
    (dict(scan_type="market", watchlist=None, universe=None,
          no_persist=False, n_codes=600), True),
    (dict(scan_type="full", watchlist=None, universe=None,
          no_persist=False, n_codes=600), True),
    (dict(scan_type="market", watchlist=None, universe=None,
          no_persist=True, n_codes=600), False),     # 调试路径不落库
    (dict(scan_type="watchlist", watchlist=["600519.SH"], universe=None,
          no_persist=False, n_codes=600), False),   # 自选保持旧路径
    (dict(scan_type="sector", watchlist=None, universe=["x"],
          no_persist=False, n_codes=600), False),   # 板块池保持旧路径
    (dict(scan_type="market", watchlist=None, universe=None,
          no_persist=False, n_codes=0), False),
])
def test_streaming_enabled_gates(kw, expected, monkeypatch):
    assert ss.streaming_enabled(**kw) is expected


def test_streaming_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MYSTERY_SCAN_STREAMING", "0")
    assert ss.streaming_enabled(
        scan_type="market", watchlist=None, universe=None,
        no_persist=False, n_codes=600) is False


def test_streaming_disabled_when_wave3_capped(monkeypatch):
    """显式三浪上限（按分降序前 N）无法流式判定 → 回退旧路径。"""
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_MAX", "200")
    assert ss.streaming_enabled(
        scan_type="market", watchlist=None, universe=None,
        no_persist=False, n_codes=600) is False


def test_streaming_disabled_when_auto_limit_applies(monkeypatch):
    """MAX 未显式设置且池超过 trigger → 默认自动限流，回退旧路径。

    「按分降序取前 200」必须等全池分数齐了才知道前 200 是谁，与流式逐票
    落盘语义冲突；此时保持旧路径，不改变原有结论口径。
    """
    monkeypatch.delenv("MYSTERY_SCAN_WAVE3_MAX", raising=False)
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "500")
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_MAX", "200")
    assert ss.streaming_enabled(
        scan_type="market", watchlist=None, universe=None,
        no_persist=False, n_codes=600) is False
    # 小池不触发自动限流 → 仍可流式
    assert ss.streaming_enabled(
        scan_type="market", watchlist=None, universe=None,
        no_persist=False, n_codes=100) is True


# ---------------------------------------------------------------------------
# 2. 每批 ≤10 落盘 + 分片文件每文件 ≤10 行 + 批后释放
# ---------------------------------------------------------------------------

def test_flush_batches_of_at_most_10(tmp_path, monkeypatch):
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 25)
    sizes, refs = [], []

    real_append = ss._scan._append_scan_results

    def _spy(db_, job_id, rows, failed_inc):
        sizes.append(len(rows))
        refs.append(rows)
        return real_append(db_, job_id, rows, failed_inc)

    monkeypatch.setattr(ss._scan, "_append_scan_results", _spy)
    p = _pipeline(db, 25)
    p.run()

    assert sizes, "必须有分批落盘"
    assert max(sizes) <= 10, f"存在超过 10 只的批次: {sizes}"
    assert sum(sizes) == 25
    # 每批落盘后 batch 引用被清空（不在内存里累积完整结果）
    assert all(len(r) == 0 for r in refs), "批对象未释放，results 仍在累积"


def test_shard_files_every_10_lines(tmp_path, monkeypatch):
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 25)
    _pipeline(db, 25).run()
    files = sorted((tmp_path / "shards").rglob("03_stock_*.jsonl"))
    assert files, "个股分片文件未写出"
    for f in files:
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln]
        assert 0 < len(lines) <= 10, f"{f.name} 有 {len(lines)} 行"
    total = sum(len([ln for ln in f.read_text(encoding="utf-8").splitlines()
                     if ln]) for f in files)
    assert total == 25


def test_no_full_results_attribute_on_pipeline(tmp_path, monkeypatch):
    """流水线不持有全量完整结果 list；跨票状态只有 KB 级 assessment。"""
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 23)
    p = _pipeline(db, 23)
    p.run()
    assert not hasattr(p, "results")
    assert len(p.assessments) == 23
    for a in p.assessments:
        # assessment 不含完整日/周/月 K（那是 GB 级爆点）
        assert "daily" not in a and "weekly" not in a and "monthly" not in a


# ---------------------------------------------------------------------------
# 3. 阶段顺序：大盘 → 板块 → 个股 → 组合层
# ---------------------------------------------------------------------------

def test_stage_order_is_index_sector_stocks_portfolio(tmp_path, monkeypatch):
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 23)
    order = []
    real_stock = ss.StreamingScan.stage_stocks
    real_pf = ss.StreamingScan.stage_portfolio

    def _stocks(self, codes):
        self._order.append("stocks")
        return real_stock(self, codes)

    def _pf(self):
        self._order.append("portfolio")
        return real_pf(self)

    monkeypatch.setattr(ss.StreamingScan, "stage_stocks", _stocks)
    monkeypatch.setattr(ss.StreamingScan, "stage_portfolio", _pf)
    p = _pipeline(db, 23)
    p.run()
    assert p._order == ["index", "sector", "stocks", "portfolio"]


# ---------------------------------------------------------------------------
# 4. 口径对拍：最终落库 wave3 / wave3_portfolio 与旧路径逐字段一致
# ---------------------------------------------------------------------------

def test_final_payload_matches_legacy_attach(tmp_path, monkeypatch):
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 6)
    n = 6

    # 旧路径：同一批 rows 原地 attach
    legacy_rows = _rows(n)
    monkeypatch.setattr(ws, "_daily_from_db",
                        lambda db_, sym: _series(_closes(), sym))
    ws.attach_wave3_to_results(legacy_rows, db=db, client=_FakeClient(db))
    legacy = {r["symbol"]: r.get("wave3") for r in legacy_rows}
    legacy_pf = legacy_rows[0].get("wave3_portfolio")

    # 新路径：流式分批落盘 + 组合层回填
    p = _pipeline(db, n)
    p.run()
    conn = db._connect()
    try:
        got = {r[0]: json.loads(r[1]) for r in conn.execute(
            "SELECT symbol, payload_json FROM scan_results WHERE job_id=?",
            (p.job_id,)).fetchall()}
    finally:
        conn.close()

    for sym, lw in legacy.items():
        assert sym in got, f"{sym} 未落库"
        assert json.dumps(got[sym].get("wave3"), sort_keys=True,
                          ensure_ascii=False) == \
            json.dumps(lw, sort_keys=True, ensure_ascii=False), \
            f"{sym} 三浪结论与旧路径不一致"
        pf = got[sym].get("wave3_portfolio")
        assert pf and pf.get("enabled"), f"{sym} 缺少组合摘要"
        # 组合摘要的核心口径逐字段一致
        for k in ("scanned", "candidates", "portfolio_risk", "scale_down",
                  "sum_risk", "total_risk_pct"):
            assert pf.get(k) == legacy_pf.get(k), \
                f"{sym} wave3_portfolio.{k} 与旧路径不一致"


def test_every_stock_has_complete_wave3_before_portfolio(tmp_path, monkeypatch):
    """阶段④之前中断，每只已落盘票仍有完整个股级三浪结论（非半截记录）。"""
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 12)
    # 模拟组合层未执行
    monkeypatch.setattr(ss.StreamingScan, "stage_portfolio", lambda self: None)
    p = _pipeline(db, 12)
    p.run()
    conn = db._connect()
    try:
        rows = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM scan_results WHERE job_id=?",
            (p.job_id,)).fetchall()]
    finally:
        conn.close()
    assert len(rows) == 12
    for r in rows:
        w3 = r.get("wave3")
        assert isinstance(w3, dict) and w3.get("verdict"), \
            f"{r['symbol']} 缺少完整个股级三浪结论"
        # 个股级仓位建议已补齐（不是 None）
        assert w3.get("shares") is not None


# ---------------------------------------------------------------------------
# 5. 断点续跑：按已落盘 symbol 幂等跳过，不重复写库
# ---------------------------------------------------------------------------

def test_resume_skips_already_persisted_symbols(tmp_path, monkeypatch):
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 15)
    p1 = _pipeline(db, 15)
    p1.run()
    conn = db._connect()
    try:
        first_count = conn.execute(
            "SELECT COUNT(*) FROM scan_results WHERE job_id=?",
            (p1.job_id,)).fetchone()[0]
    finally:
        conn.close()
    assert first_count == 15

    # 二次 resume：同交易日同类型复用 job，已落盘个股全部跳过
    p2 = _pipeline(db, 15, resume=True)
    p2.run()
    assert p2.job_id == p1.job_id
    assert p2.n_ok == 0, "resume 不应重复写入已完成个股"
    conn = db._connect()
    try:
        second_count = conn.execute(
            "SELECT COUNT(*) FROM scan_results WHERE job_id=?",
            (p1.job_id,)).fetchone()[0]
    finally:
        conn.close()
    assert second_count == 15, "PRIMARY KEY(job_id,symbol) 幂等，不应增行"


def test_crash_mid_scan_keeps_completed_results(tmp_path, monkeypatch):
    """阶段③中途抛异常：已落盘批次仍在库里（不再 n_results=0）。"""
    db = _FakeDB(tmp_path / "t.db")
    _patch_common(monkeypatch, db, tmp_path, 30)
    real = ss.StreamingScan.stage_stocks
    calls = {"n": 0}

    def _half(self, codes):
        # 只跑前 20 只，模拟进程被杀
        return real(self, codes[:20])

    monkeypatch.setattr(ss.StreamingScan, "stage_stocks", _half)
    p = _pipeline(db, 30)
    p.run()
    conn = db._connect()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM scan_results WHERE job_id=?",
            (p.job_id,)).fetchone()[0]
    finally:
        conn.close()
    assert cnt == 20, "崩溃不应丢掉已完成批次（旧路径会全废）"


# ---------------------------------------------------------------------------
# 6. 端口：run_streaming_scan 由 scan_market 在大池场景下调用
# ---------------------------------------------------------------------------

def test_scan_market_is_wired_to_streaming(monkeypatch, tmp_path):
    """``_scan_market_impl`` 分流接线：全市场大池 → W89 流水线。

    只验证分流决策，不触碰生产库、不真的分析股票、不实例化 MysteryDB。
    小池 / 自选 / no_persist 回退由上方 ``streaming_enabled`` 用例覆盖。
    """
    db = _FakeDB(tmp_path / "wire.db")
    codes = [{"code": f"{600000 + i}.SH"} for i in range(12)]

    def _svc_factory(cfg=None):
        s = _make_svc(db)
        s.market.fetch_stock_list = lambda: codes
        return s

    monkeypatch.setenv("MYSTERY_SCAN_STREAM_MIN", "5")
    monkeypatch.setattr(scan_mod, "AnalysisService", _svc_factory)
    monkeypatch.setattr(scan_mod, "_ensure_scan_data", lambda *a, **k: "")

    seen = {"stream": 0, "codes": 0}
    monkeypatch.setattr(ss, "run_streaming_scan",
                        lambda **kw: (seen.update(stream=1,
                                                  codes=len(kw.get("codes", [])))
                                      or []))

    out = scan_mod._scan_market_impl(no_persist=False, auto_sync=False)
    assert out == []
    assert seen["stream"] == 1, "全市场 12 只（> min=5）应分流到 W89"
    assert seen["codes"] == 12
