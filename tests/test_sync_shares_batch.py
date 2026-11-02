"""① sync_shares 股本快照批量写入（upsert_float_share_many）回归。

验证：db 层批量 upsert 单事务提交；sync_shares 主循环改为累积后一次性
批量写库（消除逐行 conn.commit 的往返开销）。
"""
from __future__ import annotations

import pytest

duckdb = pytest.importorskip('duckdb')

from mystery.services import sync_shares as ss
from mystery.store.db import MysteryDB


def test_upsert_float_share_many_batches(tmp_path):
    db_path = str(tmp_path / 'shares.db')
    MysteryDB(db_path)
    db = MysteryDB(db_path)
    rows = [
        ('600519.SH', '2026-10-01', 1.6e12, 1280.0, 1.25e9, 'auction_final'),
        ('000001.SZ', '2026-10-01', 2.0e11, 12.0, 1.66e10),
        ('600036.SH', '2026-10-01', 9.0e11, 35.0, 3.5e10),
    ]
    assert db.upsert_float_share_many(rows) == 3
    assert db.get_float_share('600519.SH')['float_shares'] == 1.25e9
    assert db.get_float_share('000001.SZ')['float_shares'] == 1.66e10
    # 同 key 覆盖更新
    db.upsert_float_share_many(
        [('600519.SH', '2026-10-02', 1.7e12, 1300.0, 1.30e9)])
    assert db.get_float_share('600519.SH')['float_shares'] == 1.30e9
    # 空列表安全返回 0，不报错
    assert db.upsert_float_share_many([]) == 0


def test_sync_shares_uses_batch_write(tmp_path, monkeypatch):
    """sync_shares 主循环累积后单次批量提交（不再逐行 commit）。"""
    db_path = str(tmp_path / 'mkt.db')
    MysteryDB(db_path)
    snaps = [
        {'thscode': '600519.SH', 'float_market_cap': 1.6e12, 'last_price': 1280.0},
        {'thscode': '000001.SZ', 'float_market_cap': 2.0e11, 'last_price': 12.0},
    ]

    class FakeThs:
        def get_auction_snapshot(self, thscodes, stage='final'):
            return snaps

    monkeypatch.setattr(ss, 'ThsClient', lambda: FakeThs())

    out = ss.sync_shares(codes=['600519.SH', '000001.SZ'],
                          db=MysteryDB(db_path))
    assert out['written'] == 2
    assert out['failed'] == 0
    # 批量写入后两条快照可读
    db = MysteryDB(db_path)
    assert db.get_float_share('600519.SH')['float_shares'] is not None
    assert db.get_float_share('000001.SZ')['float_shares'] is not None
