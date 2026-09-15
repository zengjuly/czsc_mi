"""W26c（007.md）：sync-shares --from-adjustments 离线回归。

覆盖：raw_adjustment_events 事件筛选（送转/增发口径、缺表 None、无事件 []）、
事件票跳过 3% 门槛仍写新 as_of 锚点（unchanged 计数）、since 默认取上次
成功对账日。不碰真实 DuckDB/不打 HTTP（临时库 + monkeypatch 竞价接口）。
"""
from __future__ import annotations


import pytest

duckdb = pytest.importorskip('duckdb')

from mystery.services import sync_shares as ss
from mystery.store.db import MysteryDB

ADJ_COLS = ("thscode TEXT, ticker TEXT, ex_date TEXT, "
            "dividend_per_share DOUBLE, per_share_bonus DOUBLE, "
            "allotment_ratio DOUBLE, allotment_price DOUBLE, "
            "currency TEXT, source_batch_id TEXT")


def _marketdb(tmp_path, rows):
    path = str(tmp_path / 'market.duckdb')
    con = duckdb.connect(path)
    con.execute(f"CREATE TABLE raw_adjustment_events ({ADJ_COLS})")
    for r in rows:
        con.execute("INSERT INTO raw_adjustment_events VALUES "
                    "(?,?,?,?,?,?,?,?,?)", r)
    con.close()
    return path


def _row(thscode, ex, bonus=None, allot=None):
    return (thscode, thscode.split('.')[0], ex, 0.5, bonus, allot, 1.0,
            'CNY', 'batch1')


def test_find_events_filters_bonus_and_allot(tmp_path):
    p = _marketdb(tmp_path, [
        _row('600519.SH', '2026-09-10', bonus=4.8),      # 送转 → 命中
        _row('000001.SZ', '2026-09-11', allot=0.3),      # 增发 → 命中
        _row('600000.SH', '2026-09-12'),                 # 仅分红 → 不命中
        _row('600001.SH', '2026-06-01', bonus=2.0),      # 早于 since → 不命中
    ])
    ev = ss.find_adjustment_events(p, '2026-09-01')
    assert ev == ['000001.SZ', '600519.SH']


def test_find_events_missing_returns_none(tmp_path):
    # 文件不存在 → None（调用方 warn 跳过，周对账兜底）
    assert ss.find_adjustment_events(str(tmp_path / 'nope.duckdb'),
                                     '2026-09-01') is None
    # 有库无表 → None
    p = str(tmp_path / 'empty.duckdb')
    duckdb.connect(p).close()
    assert ss.find_adjustment_events(p, '2026-09-01') is None
    # 有表无事件 → []（零 HTTP 路径）
    p2 = _marketdb(tmp_path, [_row('600000.SH', '2026-09-12')])
    assert ss.find_adjustment_events(p2, '2026-09-01') == []


def _fake_ths(capsys_payload):
    class FakeThs:
        marketdb_path = ''

        def get_auction_snapshot(self, thscodes, stage='final'):
            return [x for x in capsys_payload
                    if x['thscode'] in set(thscodes)]
    return FakeThs()


def test_event_vs_non_event_via_injection(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'shares.db')
    MysteryDB(db_path)
    db = MysteryDB(db_path)
    db.upsert_float_share('600519.SH', '2026-08-01',
                          1.6e12, 1280.0, 1.25e9)
    db.upsert_float_share('000001.SZ', '2026-08-01',
                          2.0e11, 12.0, 1.66e10)
    snaps = [
        {'thscode': '600519.SH', 'float_market_cap': 1.61e12,
         'last_price': 1280.0},
        {'thscode': '000001.SZ', 'float_market_cap': 2.01e11,
         'last_price': 12.0},
    ]
    monkeypatch.setattr(ss, 'ThsClient', lambda *a, **k: _fake_ths(snaps))
    out = ss.sync_shares(codes=['sh600519', 'sz000001'], db=db,
                         as_of='2026-09-15',
                         event_codes={'600519.SH'})
    assert out['written'] == 1                    # 只写事件票（新锚点）
    assert out['unchanged_event'] == 1
    assert out['skipped_small_change'] == 1       # 非事件票 ≤3% 仍跳过
    got = db.get_float_share('600519.SH')
    assert got['as_of'] == '2026-09-15'
    old = db.get_float_share('000001.SZ')
    assert old['as_of'] == '2026-08-01'           # 未覆盖
    # 状态文件记录成功对账日
    with open(ss._sync_state_path(db_path), encoding='utf-8') as f:
        import json
        assert json.load(f)['last_sync'] == '2026-09-15'


def test_from_adjustments_no_events_zero_http(tmp_path, monkeypatch):
    p = _marketdb(tmp_path, [_row('600000.SH', '2026-09-12')])

    class BoomThs:
        marketdb_path = p

        def get_auction_snapshot(self, thscodes, stage='final'):
            raise AssertionError('无事件路径不得打 HTTP')
    monkeypatch.setattr(ss, 'ThsClient', lambda *a, **k: BoomThs())
    db_path = str(tmp_path / 'shares.db')
    MysteryDB(db_path)
    db = MysteryDB(db_path)
    out = ss.sync_shares_from_adjustments(db=db, as_of='2026-09-15')
    assert out['events'] == 0 and out['requested'] == 0
    assert '零 HTTP' in out['note']


def test_from_adjustments_since_defaults_to_state(tmp_path, monkeypatch):
    """since 缺省 = 上次成功对账日（状态文件），事件票按目标批拉。"""
    p = _marketdb(tmp_path, [
        _row('600519.SH', '2026-09-05', bonus=4.8),   # 早于状态日 → 不算
        _row('000001.SZ', '2026-09-10', bonus=2.0),   # 晚于状态日 → 命中
    ])
    snaps = [{'thscode': '000001.SZ', 'float_market_cap': 2.1e11,
              'last_price': 12.0}]
    called = {}

    class Fake:
        marketdb_path = p

        def get_auction_snapshot(self, thscodes, stage='final'):
            called['thscodes'] = list(thscodes)
            return snaps
    monkeypatch.setattr(ss, 'ThsClient', lambda *a, **k: Fake())
    db_path = str(tmp_path / 'shares.db')
    MysteryDB(db_path)
    db = MysteryDB(db_path)
    ss._record_shares_sync(db_path, '2026-09-08')
    out = ss.sync_shares_from_adjustments(db=db, as_of='2026-09-15')
    assert out['since'] == '2026-09-08'
    assert out['events'] == 1 and out['requested'] == 1
    assert called['thscodes'] == ['000001.SZ']
    assert out['written'] == 1


def test_from_adjustments_missing_table_skips(tmp_path, monkeypatch):
    class BoomThs:
        marketdb_path = str(tmp_path / 'nope.duckdb')

        def get_auction_snapshot(self, thscodes, stage='final'):
            raise AssertionError('缺表路径不得打 HTTP')
    monkeypatch.setattr(ss, 'ThsClient', lambda *a, **k: BoomThs())
    db_path = str(tmp_path / 'shares.db')
    MysteryDB(db_path)
    out = ss.sync_shares_from_adjustments(db=MysteryDB(db_path),
                                          as_of='2026-09-15')
    assert out['events'] == 0 and '跳过' in out['note']
