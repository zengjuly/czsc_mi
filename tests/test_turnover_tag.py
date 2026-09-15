# -*- coding: utf-8 -*-
"""W32b 换手分档/风格标签 + W32c 行业覆盖统计（013.md）。均为只读纯逻辑。"""
import math

import pytest

from mystery.core.turnover_tag import (
    turnover_tag,
    style_tag,
    tags_from_result,
)


# ---------- turnover_tag ----------

def test_tag_unknown_on_missing():
    assert turnover_tag(None) == 'unknown'
    assert turnover_tag(float('nan')) == 'unknown'
    assert turnover_tag(-1) == 'unknown'
    assert turnover_tag(0) == 'unknown'
    assert turnover_tag('abc') == 'unknown'


def test_tag_thresholds():
    assert turnover_tag(3.2) == 'absorb'
    assert turnover_tag(3.0) == 'absorb'
    assert turnover_tag(5.0) == 'absorb'
    assert turnover_tag(8.5) == 'inflow'
    assert turnover_tag(15.0) == 'inflow'
    assert turnover_tag(25.0) == 'heavy'
    assert turnover_tag(26.0) == 'heavy'
    assert turnover_tag(70.0) == 'flee'
    assert turnover_tag(71.0) == 'flee'
    assert turnover_tag(1.5) == 'other'      # 有值但不在档
    assert turnover_tag(20.0) == 'other'     # 15-25 之间


# ---------- style_tag ----------

def test_style_trend_pass():
    assert style_tag(26.0, True, False) == 'mixed'   # 高换手+滤网过
    assert style_tag(3.2, True, False) == 'trend'    # 滤网过、换手不高
    assert style_tag(3.2, None, True) == 'trend'     # 主升信号也算趋势侧
    assert style_tag(9.0, False, True) == 'trend'    # 主升+滤网未过、turn<15 → 趋势侧


def test_style_sentiment():
    assert style_tag(16.0, False, False) == 'sentiment'
    assert style_tag(88.0, False, None) == 'sentiment'


def test_style_unknown():
    assert style_tag(None, None, None) == 'unknown'
    assert style_tag(2.0, False, False) == 'unknown'  # 无档、无滤网结论→不硬标
    assert style_tag(3.2, False, False) == 'unknown'


# ---------- tags_from_result ----------

def test_tags_empty_is_unknown_cn():
    tg = tags_from_result({})
    assert tg == {'turnover_tag': '未知', 'style_tag': '未知'}


def test_tags_absorb_3_2_trend():
    d = {'turnover_20': 3.2,
         'mystery': {'signal': {'年线滤网': True, '主升浪信号': False}}}
    tg = tags_from_result(d)
    assert tg['turnover_tag'] == '吸筹区(3-5)'
    assert tg['style_tag'] == '趋势'


def test_tags_nan_turn_no_crash():
    tg = tags_from_result({'turnover_20': float('nan'),
                           'mystery': {'signal': {}}})
    assert tg['turnover_tag'] == '未知'


def test_tags_never_says_非吸筹():
    for turn in (None, 1.0, 3.2, 9.9, 26.0, 88.0):
        tg = tags_from_result({'turnover_20': turn})
        assert '非' not in tg['turnover_tag']


def test_tags_missing_mystery_tolerated():
    tg = tags_from_result({'turnover_20': 9.0})   # 无 signal → 风格只能未知
    assert tg['turnover_tag'] == '流入(8-15)'
    assert tg['style_tag'] == '未知'


# ---------- W32c sector_coverage_stats（tmp db） ----------

@pytest.fixture()
def sector_db(tmp_path):
    from mystery.store.db import MysteryDB
    db_path = str(tmp_path / 's.db')
    MysteryDB(db_path)  # 建表
    import sqlite3
    conn = sqlite3.connect(db_path)
    # rel 存 881101.TI；kline 存 ths_881101（与生产格式一致）；表由 schema.sql 已建
    rel_cols = [c[1] for c in conn.execute(
        "PRAGMA table_info(stock_sector_rel)")]
    assert set(('stock_code', 'sector_code', 'is_primary')) <= set(rel_cols), rel_cols
    conn.executemany("INSERT INTO stock_sector_rel (stock_code, sector_code, is_primary) VALUES (?,?,?)",
                     [('sh.600001', '881101.TI', 1),
                      ('sh.600002', '881101.TI', 1),
                      ('sh.600003', '889999.TI', 1)])
    conn.executemany(
        "INSERT INTO sector_kline (sector_code, sector_name, trade_date, "
        "open, high, low, close, volume, amount) VALUES (?,?,?,?,?,?,?,?,?)",
        [('ths_881101', '煤炭', '2026-08-20', 1, 1, 1, 1, 1, 1),
         ('ths_881101', '煤炭', '2026-08-21', 1, 1, 1, 1, 1, 1)])
    conn.commit()
    conn.close()
    return MysteryDB(db_path)


def test_sector_coverage_stats(sector_db):
    s = sector_db.sector_coverage_stats()
    assert s['n_stocks'] == 3
    assert s['n_covered'] == 2
    assert abs(s['coverage'] - 66.7) < 0.1
    assert s['n_sectors'] == 2
    assert s['n_sectors_covered'] == 1
    assert s['kline_max'] == '2026-08-21'


def test_sector_coverage_codes_filter(sector_db):
    s = sector_db.sector_coverage_stats(codes=['sh.600003'])
    assert s['n_stocks'] == 1 and s['n_covered'] == 0
    assert s['coverage'] == 0.0
