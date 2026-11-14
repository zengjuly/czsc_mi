"""W94/D：``--as-of`` 历史回放（K 线严格截断 + 台账隔离 + 零回归）。

背景：用户金标 1/14（大盘腿向上，本该出三振）、10/23（大资金进场那一刻，
严格腿滞后）都是三振日；但系统所有 job 都基于最新 K 线跑，从没按历史
日期回放过 → ``true_resonance`` 全库 0。D 让 K 线（个股/指数）严格截断到
as_of 当日及之前，杜绝未来函数，复现历史截面。

本测试覆盖：
- ``MarketDataClient`` 从 cfg / env 读 as_of；``_clamp_end`` 截断语义；
- as_of 模式 ``_fetch_daily`` / ``_fetch_index_uncached`` 短路在线源、
  ``_cache_online_bars`` 不回写、``_freshness_ref`` 返回 as_of；
- 报告层 ``_write_scan_report`` 回放文件名带 _回放_ 且不覆盖正式日报；
- ``_cmd_scan_body`` 注入 cfg/env、强制 replay 台账、关 auto_sync；
- 不带 --as-of 时零回归（cfg 无 as_of、scan_type=None、auto_sync 保持）。
"""
from __future__ import annotations

import argparse
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _clean_asof_env():
    """每个用例前后清 MYSTERY_AS_OF，避免污染其他测试/生产语义。"""
    old = os.environ.pop("MYSTERY_AS_OF", None)
    yield
    os.environ.pop("MYSTERY_AS_OF", None)
    if old is not None:
        os.environ["MYSTERY_AS_OF"] = old


def _client(cfg=None, monkeypatch=None):
    """构造一个不真正连库的 MarketDataClient（用现有 __init__，DB 惰性）。"""
    from mystery.adapters.market import MarketDataClient
    return MarketDataClient(cfg)


# ---------------------------------------------------------------------------
# as_of 注入与 _clamp_end
# ---------------------------------------------------------------------------
def test_as_of_from_cfg():
    c = _client({"as_of": "2026-10-23"})
    assert c.as_of == "2026-10-23"


def test_as_of_from_env(monkeypatch):
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-14")
    c = _client({})
    assert c.as_of == "2026-01-14"


def test_cfg_as_of_takes_priority_over_env(monkeypatch):
    monkeypatch.setenv("MYSTERY_AS_OF", "2026-01-14")
    c = _client({"as_of": "2026-10-23"})
    assert c.as_of == "2026-10-23"


def test_no_as_of_by_default():
    c = _client({})
    assert c.as_of == ""


def test_clamp_end_normal_mode_unchanged():
    c = _client({})
    assert c._clamp_end(None) is None
    assert c._clamp_end("2026-11-13") == "2026-11-13"


def test_clamp_end_replay_forces_upper_bound():
    c = _client({"as_of": "2026-10-23"})
    assert c._clamp_end(None) == "2026-10-23"           # None 也要截断
    assert c._clamp_end("2026-11-13") == "2026-10-23"   # 未来上界被钳
    assert c._clamp_end("2026-09-01") == "2026-09-01"   # 更早的切片保留


def test_freshness_ref_is_as_of_in_replay():
    c = _client({"as_of": "2026-10-23"})
    assert c._freshness_ref("sh.000001") == "2026-10-23"


# ---------------------------------------------------------------------------
# 回放绝不联网补数 / 回写（杜绝未来函数与生产库污染）
# ---------------------------------------------------------------------------
def test_cache_online_bars_noop_in_replay():
    import pandas as pd
    c = _client({"as_of": "2026-10-23"})
    c.db = MagicMock()
    df = pd.DataFrame({"日期": ["2026-11-13"], "收盘价": [1.0]})
    c._cache_online_bars("sh.600000", df, "ths_official")
    c.db.upsert_kline.assert_not_called()   # 回放绝不回写


def test_fetch_daily_replay_only_local_db(monkeypatch):
    import pandas as pd
    c = _client({"as_of": "2026-10-23"})
    local = pd.DataFrame({
        "date": ["2026-10-20", "2026-10-23"],
        "code": ["sh.600000"] * 2, "open": [1, 1], "high": [1, 1],
        "low": [1, 1], "close": [10, 11], "volume": [1, 1],
        "amount": [1, 1], "turn": [None, None], "pctChg": [None, None],
    })
    c.db = MagicMock()
    c.db.load_kline.return_value = local
    c.ths = MagicMock()   # 在线源：若被调用即说明短路失败
    c.tdx_api = MagicMock()
    c.tdx_local = MagicMock()
    cn, src = c._fetch_daily("sh.600000", None, "2026-10-23")
    assert src == "db"
    assert "收盘价" in cn.columns
    assert len(cn) == 2
    # 在线降级链完全没有被触碰
    c.ths.get_daily.assert_not_called()
    c.tdx_api.get_daily.assert_not_called()
    c.tdx_local.get_daily.assert_not_called()
    c.db.load_kline.assert_called_once()


def test_fetch_index_uncached_replay_only_local_db():
    import pandas as pd
    from mystery.core.models import BarSeries
    c = _client({"as_of": "2026-10-23"})
    local = pd.DataFrame({
        "date": ["2026-10-20", "2026-10-23"],
        "code": ["sh.000001"] * 2, "open": [1, 1], "high": [1, 1],
        "low": [1, 1], "close": [3690, 3691], "volume": [1, 1],
        "amount": [1, 1], "turn": [None, None], "pctChg": [None, None],
    })
    c.db = MagicMock()
    c.db.load_kline.return_value = local
    c.ths = MagicMock()
    c.tdx_local = MagicMock()
    s = c._fetch_index_uncached("sh.000001", "1d", None, "2026-10-23")
    assert isinstance(s, BarSeries)
    assert s.bars and str(s.bars[-1].dt)[:10] == "2026-10-23"
    c.ths.get_index_daily.assert_not_called()
    c.tdx_local.get_daily.assert_not_called()


# ---------------------------------------------------------------------------
# 流式分流：回放也走 W89 分批（不 OOM），独立 replay 台账
# ---------------------------------------------------------------------------
def test_streaming_enabled_allows_replay():
    from mystery.services import streaming_scan as ss
    kw = dict(scan_type="replay", watchlist=None, universe=None,
              no_persist=False, n_codes=6000)
    # 三浪口径可能触发限流回退，用 monkeypatch 固定为「全量」（MAX=0）
    import mystery.services.runtime_settings as rts
    orig = ss.scan_wave3_options_effective
    ss.scan_wave3_options_effective = lambda: {"MYSTERY_SCAN_WAVE3_MAX": "0"}
    try:
        assert ss.streaming_enabled(**kw) is True
    finally:
        ss.scan_wave3_options_effective = orig
    assert ss.streaming_enabled(scan_type="watchlist", watchlist=["x"],
                                universe=None, no_persist=False,
                                n_codes=6000) is False


# ---------------------------------------------------------------------------
# CLI 接线：注入 cfg/env + replay + 关同步；零回归
# ---------------------------------------------------------------------------
def _scan_args(**kw):
    base = dict(cfg={}, limit=10, watchlist=False, board=None, min_score=None,
                no_persist=True, force=False, no_auto_sync=False, resume=False,
                market=False, signal=None, report=False, as_of=None,
                bg_task_id=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_scan_body_asof_injects_cfg_env_and_replay(monkeypatch):
    import mystery.apps.cli as cli
    cap = {}

    def _fake_scan_market(**kw):
        cap.update(kw)
        return []

    import mystery.services.scan as scan
    monkeypatch.setattr(scan, "scan_market", _fake_scan_market)
    monkeypatch.setattr(cli, "_print_db_fingerprint", lambda: None)
    args = _scan_args(as_of="2026-10-23")
    cli._cmd_scan_body(args)
    assert cap["cfg"]["as_of"] == "2026-10-23"
    assert os.environ.get("MYSTERY_AS_OF") == "2026-10-23"
    assert cap["scan_type"] == "replay"
    assert cap["auto_sync"] is False      # 回放绝不联网同步
    # wave3_summary_line 对空结果应安全
    assert args.cfg["as_of"] == "2026-10-23"


def test_cmd_scan_body_without_asof_zero_regression(monkeypatch):
    import mystery.apps.cli as cli
    cap = {}
    import mystery.services.scan as scan
    monkeypatch.setattr(scan, "scan_market", lambda **kw: cap.update(kw) or [])
    monkeypatch.setattr(cli, "_print_db_fingerprint", lambda: None)
    args = _scan_args(as_of=None)
    cli._cmd_scan_body(args)
    assert "as_of" not in cap["cfg"]
    assert cap["scan_type"] is None
    assert cap["auto_sync"] is True       # no_auto_sync=False 时保持默认开
    assert os.environ.get("MYSTERY_AS_OF") is None


def test_cmd_scan_body_rejects_bad_asof(monkeypatch):
    import mystery.apps.cli as cli
    monkeypatch.setattr(cli, "_print_db_fingerprint", lambda: None)
    assert cli._cmd_scan_body(_scan_args(as_of="10/23")) == 2


def test_write_scan_report_replay_filename_uses_asof(monkeypatch, tmp_path):
    import mystery.apps.cli as cli
    import mystery.apps.reports.excel_report as er
    import mystery.apps.reports.html_report as hr
    cap = {}
    monkeypatch.setattr(cli, "output_dir", lambda cfg: str(tmp_path))
    monkeypatch.setattr(er, "write_excel",
                        lambda results, path, **kw: cap.update(xlsx=path) or path)
    monkeypatch.setattr(hr, "write_html",
                        lambda results, path, **kw: cap.update(html=path) or path)
    monkeypatch.setattr(cli, "_turnover_qa_line", lambda *a, **k: "")
    monkeypatch.setattr(cli, "_suffix_orphans_hard_fail", lambda: 0)
    import mystery.services.analyze as az
    monkeypatch.setattr(az, "market_env_summary", lambda *a, **k: {})
    monkeypatch.setattr(az, "market_env_line", lambda *a, **k: "")
    import mystery.services.index_analysis as ia
    monkeypatch.setattr(ia, "analyze_major_indexes", lambda *a, **k: [])
    import mystery.services.sector_analysis as sa
    monkeypatch.setattr(sa, "analyze_focus_sectors", lambda *a, **k: [])
    args = _scan_args(as_of="2026-10-23", report=True)
    cli._write_scan_report([{"symbol": "x", "trade_date": "2026-10-23",
                             "score": 1, "mystery": {}}], args)
    assert "回放_20261023" in cap["xlsx"]
    assert "回放_20261023" in cap["html"]
    assert "回放" in cap["html"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
