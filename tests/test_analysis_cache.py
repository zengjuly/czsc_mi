"""W23：分析结果缓存（006.md 阶段 3）离线回归。

- 指纹：末根 dt/close/volume + 根数任一变化 → 指纹变化。
- cache_key：include_detail / chan 开关 / epoch 任一变化 → 键变化（必 miss）。
- AnalysisCache put/get/删 往返；payload 还原 to_dict 字段一致。
- analyze_one_stock 集成（stub market/rules）：第二次命中不重算、改开关必 miss、
  sync 写 K 线（epoch+1）必 miss。不打网络、不打生产库。
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from mystery.core.models import Bar, BarSeries, MarketContext, \
    AnalysisResult, MysteryBreakdown
from mystery.services import analyze as A
from mystery.store.cache import AnalysisCache, bars_fingerprint, \
    make_cache_key, scan_cache_enabled
from mystery.store.db import MysteryDB


def _bars(n=3, close=10.0, vol=1000.0):
    base = dt.datetime(2026, 9, 11)
    return [Bar(dt=base + dt.timedelta(days=i), open=close, high=close,
                low=close, close=close, volume=vol)
            for i in range(n)]


# ---------------- 指纹 / 键 ----------------

def test_fingerprint_sensitivity():
    fp = bars_fingerprint(_bars())
    assert bars_fingerprint(_bars()) == fp
    assert bars_fingerprint(_bars(close=10.1)) != fp       # 末根 close
    assert bars_fingerprint(_bars(vol=1001)) != fp          # 末根 volume
    assert bars_fingerprint(_bars(n=4)) != fp               # 根数
    assert bars_fingerprint([]) == 'empty'


def test_cache_key_attribution():
    fp = bars_fingerprint(_bars())
    k = lambda adj, flags, ver: make_cache_key(adj, 'mystery-1.22.30-compat',
                                               flags, ver, fp)
    assert k('qfq', 'D1|C0|S0|E0', '') == k('qfq', 'D1|C0|S0|E0', '')
    assert k('qfq', 'D1|C0|S0|E0', '') != k('qfq', 'D0|C0|S0|E0', '')  # detail
    assert k('qfq', 'D1|C0|S0|E0', '') != k('qfq', 'D1|C1|S0|E0', '')  # chan
    assert k('qfq', 'D1|C0|S0|E0', '') != k('qfq', 'D1|C0|S1|E0', '')  # score
    assert k('qfq', 'D1|C0|S0|E0', '') != k('qfq', 'D1|C0|S0|E1', '')  # epoch
    assert k('qfq', 'D1|C0|S0|E0', '') != k('hfq', 'D1|C0|S0|E0', '')  # adjust


# ---------------- 表往返 ----------------

@pytest.fixture()
def db(tmp_path):
    return MysteryDB(db_path=str(tmp_path / 'test.db'))


def test_cache_roundtrip_and_delete(db):
    c = AnalysisCache(db)
    payload = {'symbol': '600519.SH', 'score': 66.0, 'advice': '关注'}
    c.put('600519.SH', '2026-09-11', 'k1', payload)
    assert c.get('600519.SH', '2026-09-11', 'k1') == payload
    assert c.get('600519.SH', '2026-09-11', 'k2') is None
    assert c.invalidate_symbol('600519.SH', '2026-09-11') == 1
    assert c.get('600519.SH', '2026-09-11', 'k1') is None


def test_result_payload_restore_equivalent():
    """payload → AnalysisResult → to_dict 字段一致（含 chan 还原）。"""
    from mystery.adapters.czsc_adapter import chan_from_dict
    r = AnalysisResult(
        symbol='600519.SH', name='贵州茅台', trade_date='2026-09-11',
        price=1400.0, score=71.5, advice='持有', true_resonance=True,
        turnover_20=0.42, high_120=1500.0,
        mystery=MysteryBreakdown(signal={'综合评分': 71.5},
                                 resonance={'x': 1}),
        sector={'行业名称': '白酒'}, financial={'PE': 25.0},
        rule_ver='mystery-1.22.30-compat', czsc_ver='test')
    d = r.to_dict()
    r2 = A._result_from_payload(d)
    assert r2.to_dict() == d
    assert r2.true_resonance is True and r2.score == 71.5


# ---------------- analyze_one_stock 集成（stub） ----------------

def _patch_env(monkeypatch):
    """chan 关闭 + 固定开关，避免环境干扰。"""
    monkeypatch.setenv('MYSTERY_CHAN_ENABLED', '0')
    monkeypatch.setenv('MYSTERY_CHAN_SCORE', '0')


def _make_service(monkeypatch, db, bars=None):
    """构造 stub AnalysisService：market 读 fixture bars，run_rules 计数。"""
    svc = A.AnalysisService.__new__(A.AnalysisService)
    svc.cfg = {}
    daily = BarSeries(symbol='600519.SH', freq='1d', adjust='qfq',
                      bars=bars if bars is not None else _bars(),
                      source='db')
    svc.market = SimpleNamespace(
        db=db,
        fetch_bars=lambda sym, freq='1d', **kw: daily,
        resample_bars=lambda s, f: BarSeries(symbol=s.symbol, freq=f,
                                             adjust=s.adjust, bars=[],
                                             source=s.source),
    )
    svc.sector = None
    svc.build_market_context = lambda internal, **kw: MarketContext()
    calls = {'n': 0}

    def fake_run_rules(daily, weekly, monthly, ctx, include_detail):
        calls['n'] += 1
        return MysteryBreakdown(
            signal={'综合评分': 55.0, '操作建议': '观察',
                    '真三振': False, '年线滤网': True})
    svc.run_rules = fake_run_rules
    return svc, calls


def test_second_call_hits_cache(db, monkeypatch):
    _patch_env(monkeypatch)
    svc, calls = _make_service(monkeypatch, db)
    r1 = svc.analyze_one_stock('600519.SH')
    assert calls['n'] == 1
    r2 = svc.analyze_one_stock('600519.SH')
    assert calls['n'] == 1, '第二次应命中缓存，不重算'
    assert r1.to_dict() == r2.to_dict(), '命中字段必须一致'


def test_include_detail_change_misses(db, monkeypatch):
    _patch_env(monkeypatch)
    svc, calls = _make_service(monkeypatch, db)
    svc.analyze_one_stock('600519.SH', include_detail=True)
    svc.analyze_one_stock('600519.SH', include_detail=False)
    assert calls['n'] == 2, 'include_detail 变化必 miss'


def test_chan_toggle_changes_key(db, monkeypatch):
    _patch_env(monkeypatch)
    svc, calls = _make_service(monkeypatch, db)
    svc.analyze_one_stock('600519.SH')
    # 结构展示开、混合分关：chan_enabled 进键 → miss（chan 走 chan_cache，
    # czsc 不可用时 _analyze_chan 内部吞异常返回空结构，不影响本测试）
    monkeypatch.setenv('MYSTERY_CHAN_ENABLED', '1')
    svc.analyze_one_stock('600519.SH')
    assert calls['n'] == 2


def test_kline_update_invalidates_rows(db, monkeypatch):
    """W23：upsert 该票 K 线 → 同事务删除 analysis_cache 行（sync 失效）。"""
    _patch_env(monkeypatch)
    import pandas as pd
    from mystery.store.cache import AnalysisCache
    svc, calls = _make_service(monkeypatch, db)
    svc.analyze_one_stock('600519.SH')
    conn = db._connect()
    n = conn.execute("SELECT COUNT(*) FROM analysis_cache WHERE symbol='600519.SH'").fetchone()[0]
    conn.close()
    assert n == 1, '写缓存成功'
    # 模拟 sync 写该票日 K → 缓存行应被删除
    df = pd.DataFrame({
        '代码': ['sh.600519'], '日期': ['2026-09-11'],
        '开盘价': [10.0], '最高价': [10.5], '最低价': [9.8],
        '收盘价': [10.2], '成交量': [1100], '成交额': [11000],
        '换手率': [None], '涨跌幅': [None]})
    db.upsert_kline(df, 'sh.600519', 'daily')
    conn = db._connect()
    n = conn.execute("SELECT COUNT(*) FROM analysis_cache WHERE symbol='600519.SH'").fetchone()[0]
    conn.close()
    assert n == 0, 'K 线更新后该票缓存应删（点号格式归一）'


def test_use_cache_false_never_hits(db, monkeypatch):
    _patch_env(monkeypatch)
    svc, calls = _make_service(monkeypatch, db)
    svc.analyze_one_stock('600519.SH', use_cache=False)
    svc.analyze_one_stock('600519.SH', use_cache=False)
    assert calls['n'] == 2
    # 且未写入缓存
    assert AnalysisCache(db).get('600519.SH', '2026-09-11', 'anything') is None \
        or True  # 键未知，重点是不影响下条断言
    conn = db._connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_online_source_not_cached(db, monkeypatch):
    """盘中在线源（source=ths_official）不写缓存（006：收盘后日级有效）。"""
    _patch_env(monkeypatch)
    svc, calls = _make_service(monkeypatch, db)
    svc.market.fetch_bars = lambda sym, freq='1d', **kw: BarSeries(
        symbol='600519.SH', freq='1d', adjust='qfq', bars=_bars(),
        source='ths_official')
    svc.analyze_one_stock('600519.SH')
    svc.analyze_one_stock('600519.SH')
    assert calls['n'] == 2


def test_scan_cache_env_gate(monkeypatch):
    monkeypatch.delenv('MYSTERY_SCAN_CACHE', raising=False)
    assert scan_cache_enabled() is True
    monkeypatch.setenv('MYSTERY_SCAN_CACHE', '0')
    assert scan_cache_enabled() is False
    monkeypatch.setenv('MYSTERY_SCAN_CACHE', '1')
    assert scan_cache_enabled() is True
