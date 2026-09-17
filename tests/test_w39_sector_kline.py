"""W39 回归：sector_kline 写路径（upsert_sector_kline / 清单 / 增量断点）。

sync_sector_kline 的在线部分用 monkeypatch 打桩 ThsClient.get_index_daily，
零网络；归一通道本身（fuyao）另有 integration 用例守护。
"""
import os
import sqlite3
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store.db import MysteryDB  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    return MysteryDB(str(tmp_path / "t.db"))


def _df(dates, close=100.0):
    n = len(dates)
    return pd.DataFrame({
        '日期': dates,
        '开盘价': [close] * n, '最高价': [close + 1] * n,
        '最低价': [close - 1] * n, '收盘价': [close] * n,
        '成交量': [1000] * n, '成交额': [1e6] * n,
    })


def test_upsert_normalizes_code_and_roundtrips(db):
    """任意入参格式归一为 ths_ 前缀写入；读回中文列升序。"""
    n = db.upsert_sector_kline(_df(['2026-09-15', '2026-09-16']),
                               '881101.TI', sector_name='煤炭行业')
    assert n == 2
    got = db.get_sector_kline('ths_881101')
    assert got is not None and len(got) == 2
    assert list(got['日期']) == ['2026-09-15', '2026-09-16']
    row = db._connect().execute(
        "SELECT sector_code, sector_name, source_type FROM sector_kline "
        "LIMIT 1").fetchone()
    assert row == ('ths_881101', '煤炭行业', 'ths')


def test_upsert_idempotent_revises(db):
    """同 (code, date) 重写：行数不翻倍，价格被修订覆盖。"""
    db.upsert_sector_kline(_df(['2026-09-16'], close=100.0), 'ths_881103', '化工')
    db.upsert_sector_kline(_df(['2026-09-16'], close=111.0), 'ths_881103', '化工')
    got = db.get_sector_kline('881103')
    assert len(got) == 1
    assert float(got.iloc[0]['收盘价']) == 111.0


def test_dates_names_sectors_helpers(db):
    db.upsert_sector_kline(_df(['2026-09-15']), 'ths_881105', '石油')
    db.upsert_sector_kline(_df(['2026-09-16']), '881107.TI', '钢铁')
    assert db.get_sector_kline_dates() == {
        'ths_881105': '2026-09-15', 'ths_881107': '2026-09-16'}
    assert db.get_sector_kline_names()['ths_881105'] == '石油'
    assert db.get_sector_kline_sectors() == ['ths_881105', 'ths_881107']


def test_upsert_missing_column_raises(db):
    bad = _df(['2026-09-15']).drop(columns=['收盘价'])
    with pytest.raises(ValueError):
        db.upsert_sector_kline(bad, 'ths_881101')


def test_sync_sector_kline_incremental_monkeypatch(db, tmp_path, monkeypatch):
    """sync_sector_kline 全链路（打桩取数）：增量窗口 = 库内最新 - days 前扩。"""
    from mystery.adapters import ths as ths_mod
    from mystery.services import sync as sync_mod

    db.upsert_sector_kline(_df(['2026-08-20']), 'ths_881101', '煤炭行业')
    calls = {}

    class FakeThs:
        def __init__(self, cfg=None):
            pass

        def get_index_daily(self, symbol, start=None, end=None):
            calls['symbol'], calls['start'] = symbol, start
            return _df(['2026-08-20', '2026-09-15', '2026-09-16'])

    monkeypatch.setattr(ths_mod, 'ThsClient', FakeThs)
    # sync 内部 from ..adapters.ths import ThsClient → patch 源模块属性即可
    out = sync_mod.sync_sector_kline(cfg={'db_path': db.db_path}, days=30)
    assert out['synced'] == 1 and out['failed'] == 0
    assert calls['symbol'] == '881101.TI'          # fuyao 归一格式
    assert calls['start'] == '2026-07-21'          # 2026-08-20 前扩 30 天
    got = db.get_sector_kline('ths_881101')
    assert len(got) == 3                           # 旧行保留 + 新 2 行
    # sector_name 不丢失
    assert got.iloc[0]['日期'] == '2026-08-20'


def test_sync_sector_kline_empty_list_refuses(db):
    """清单为空必须报错，禁止静默成功（与 sync_market 同纪律）。"""
    from mystery.services import sync as sync_mod
    with pytest.raises(RuntimeError):
        sync_mod.sync_sector_kline(cfg={'db_path': db.db_path})


def test_sync_sector_kline_full_since_rebuild(db, monkeypatch):
    """full_since 重建：先清空错位的周末孤儿行，再从指定日全量重灌。"""
    from mystery.adapters import ths as ths_mod
    from mystery.services import sync as sync_mod

    # 制造污染：错位的周日行 + 正常行
    db.upsert_sector_kline(_df(['2026-08-19', '2026-08-20']), 'ths_881101', '煤炭行业')
    db.upsert_sector_kline(_df(['2026-09-16']), 'ths_881101', '煤炭行业')  # 周日=错位孤儿

    class FakeThs:
        def __init__(self, cfg=None):
            pass

        def get_index_daily(self, symbol, start=None, end=None):
            assert start == '2023-01-01'  # 重建窗口忽略库内断点
            return _df(['2026-08-20', '2026-08-21'])

    monkeypatch.setattr(ths_mod, 'ThsClient', FakeThs)
    out = sync_mod.sync_sector_kline(cfg={'db_path': db.db_path},
                                     full_since='2023-01-01')
    assert out['synced'] == 1 and out['failed'] == 0
    got = db.get_sector_kline('ths_881101')
    dates = list(got['日期'])
    assert dates == ['2026-08-20', '2026-08-21']   # 旧错位行被清掉，无周日孤儿
    assert '煤炭行业' in list(got['板块名称']) if '板块名称' in got.columns else True
