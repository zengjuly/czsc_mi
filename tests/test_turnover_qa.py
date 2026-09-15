"""W26b（007.md）：换手覆盖率可观测 + 策略 B 回填边界 离线回归。

覆盖：
- 结构化 QA 字段（turn_20_coverage / chip_low_unknown_rate / turn_source /
  n_symbols / n_with_shares / skipped_before_asof）。
- 默认策略 A 只填当日；策略 B（backfill_days）回填窗口内空 turn，
  但禁止越过各票快照 as_of（date < as_of 的行 → skipped_before_asof，不写）。
- B 不覆盖 official/legacy 已有值（复用 set_turn 的 turn IS NULL 保护）。
- turnover_qa_stats：覆盖率分母只含窗口内有 K 的票（无 K 不摊薄）。
不打 fuyao HTTP、不碰生产库。
"""
from __future__ import annotations

import sqlite3

import pytest

from mystery.services.sync_turnover import sync_turnover
from mystery.store.db import MysteryDB


@pytest.fixture()
def qa_db(tmp_path):
    """3 只票：
    600000 快照 as_of=09-08（窗口内 09-08~09-11 空 turn 可回填）
    600001 快照 as_of=09-11（更早的行 date<as_of → B 也不得回填）
    600002 无快照（unknown 保持）
    600003 有 legacy turn（任何路径不得覆盖）
    """
    db_path = str(tmp_path / 'qa.db')
    MysteryDB(db_path)  # 先建表
    dates = ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11']
    rows = []
    for code in ('sh.600000', 'sh.600001', 'sh.600002', 'sh.600003'):
        for d in dates:
            turn = 1.5 if code == 'sh.600003' else None
            rows.append((code, d, 'daily', 1, 1, 1, 1, 2e6, 1e7, turn))
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO stock_industry_info (code, code_name, type) VALUES (?,?,?)",
        [('sh.600000', '测试A', '1'), ('sh.600001', '测试B', '1'),
         ('sh.600002', '测试C', '1'), ('sh.600003', '测试D', '1')])
    conn.executemany(
        "INSERT INTO stock_kline_data (code,date,period,open,high,low,close,"
        "volume,amount,turn) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("UPDATE stock_kline_data SET turn_source='legacy' "
                 "WHERE code='sh.600003'")
    conn.commit()
    conn.close()
    db = MysteryDB(db_path)
    db.upsert_float_share('600000.SH', '2026-09-08',
                          float_market_cap=1e10, last_price=10.0,
                          float_shares=1e9)
    db.upsert_float_share('600001.SH', '2026-09-11',
                          float_market_cap=1e10, last_price=10.0,
                          float_shares=1e9)
    return db


def _turns(db, code):
    conn = sqlite3.connect(db.db_path)
    r = conn.execute("SELECT substr(date,1,10), turn, turn_source FROM "
                     "stock_kline_data WHERE code=? ORDER BY date",
                     (code,)).fetchall()
    conn.close()
    return dict((d, (t, s)) for d, t, s in r)


def test_default_only_today(qa_db):
    """策略 A（默认）：只填当日；历史空 turn 不动。"""
    qa = sync_turnover('2026-09-11', db=qa_db)
    t = _turns(qa_db, 'sh.600000')
    assert t['2026-09-11'][0] == 0.2          # 当日派生 2e6/1e9
    assert t['2026-09-08'][0] is None          # 无 ffill 锚前的洞保持（09-08 恰为 as_of 边界）
    # 09-09/09-10 是 ≤5 日中间洞? 600000 无更早值 → 前值锚只有 09-11 之后无值，
    # ffill 只向前传播：09-09/10 在 09-11 之前、之前无值 → 不填
    assert t['2026-09-09'][0] is None
    assert t['2026-09-10'][0] is None
    # 600001 快照 as_of=09-11：仅当日可派生
    t1 = _turns(qa_db, 'sh.600001')
    assert t1['2026-09-11'][0] == 0.2
    assert t1['2026-09-08'][0] is None


def test_backfill_never_before_as_of(qa_db):
    """策略 B：回填窗口内空 turn，但 date < 快照 as_of 的行必须跳过。"""
    qa = sync_turnover('2026-09-11', db=qa_db, backfill_days=20)
    t = _turns(qa_db, 'sh.600000')
    # as_of=09-08 → 09-08..09-11 全部可派生
    for d in ('2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11'):
        assert t[d][0] == 0.2, d
        assert t[d][1] == 'calc_float', d
    t1 = _turns(qa_db, 'sh.600001')
    # 唯一快照 as_of=09-11：更早三行 = skipped_before_asof，不得写入
    for d in ('2026-09-08', '2026-09-09', '2026-09-10'):
        assert t1[d][0] is None, d
    assert t1['2026-09-11'][0] == 0.2
    # 600003 legacy 1.5 未被任何路径覆盖
    t3 = _turns(qa_db, 'sh.600003')
    assert all(v[0] == 1.5 for v in t3.values())
    # QA 计数：3 行历史被 as_of 挡住（600001×3）+ 600002 无快照行同样计 skipped
    assert qa['skipped_before_asof'] >= 3
    assert qa['backfill_range'] > 0


def test_qa_fields_structured(qa_db):
    """QA 一行含覆盖率/来源分布/分母口径（无 K 票不进分母）。"""
    qa = sync_turnover('2026-09-11', db=qa_db)
    for k in ('turn_20_coverage', 'chip_low_unknown_rate', 'turn_source',
              'n_symbols', 'n_with_shares', 'turn_null_rows'):
        assert k in qa, k
    assert qa['n_symbols'] == 4          # 窗口内有 K 的票
    assert qa['n_with_shares'] == 2      # 有 as_of<=当日 快照
    assert qa['turn_20_coverage'] is not None
    # 来源分布：null 行（unknown 面）与 calc_float/legacy 并存
    assert 'null' in qa['turn_source']
    assert qa['turn_source'].get('legacy') == 4   # 600003 全部原值
    src = qa_db.turnover_qa_stats('2026-09-11',
                                  codes=['sh.600000', 'sh.999999'])
    # 分母只含有 K 的票：999999 无 K → rows 全来自 600000，n_symbols 计 universe
    assert src['n_symbols'] == 2 and src['rows'] == 4


def test_dual_scope_qa_line(qa_db, tmp_path, monkeypatch):
    """W27a P0：日报 QA 行分【自选】与【全市场】双口径，自选口径不被
    无快照票摊薄；两行 \\n 分隔。fixture 自选 = 600000/600001。"""
    import json

    from mystery.apps.cli import _turnover_qa_line
    import mystery.services.watchlist as wl_mod

    wl = tmp_path / 'watchlist.json'
    wl.write_text(json.dumps(
        [{"symbol": "600000.SH", "name": "测试A"},
         {"symbol": "600001.SH", "name": "测试B"}]), encoding='utf-8')
    # 直接 patch 模块路径变量（reload 会污染同进程其它测试）
    monkeypatch.setattr(wl_mod, '_DEFAULT_WATCHLIST', str(wl))

    line = _turnover_qa_line(qa_db, [], '2026-09-11')
    rows = line.split('\n')
    assert len(rows) == 2, line
    assert rows[0].startswith('自选 换手20日覆盖率')
    assert rows[1].startswith('全市场 换手20日覆盖率')
    # 自选口径分母 = 2 只有 K 的自选票（全市场 = 4 只）
    assert '2 只中' in rows[0] and '4 只中' in rows[1]
    # 双口径独立计算：自选分母只含自选票（本 fixture 里 with-turn 行都在
    # 600002/600003，自选覆盖率 0 < 全市场 0.25 属预期——证明口径确实拆开、
    # 全市场数字不再冒充自选健康度）
    w = qa_db.turnover_qa_stats('2026-09-11', codes=['sh.600000', 'sh.600001'])
    m = qa_db.turnover_qa_stats('2026-09-11')
    assert w['rows'] == 8 and m['rows'] == 16       # 分母口径拆开
    assert w['coverage'] == 0.0 and m['coverage'] == 0.25
    assert w['n_with_shares'] == 2 and m['n_with_shares'] == 2


def test_coverage_denominator_excludes_no_k(qa_db):
    """无 K 票不摊薄覆盖率（007.md 验收：分母不含「无 K」）。"""
    stats = qa_db.turnover_qa_stats('2026-09-11')
    # 4 票 × 4 行 = 16 行全在窗口
    assert stats['rows'] == 16
    before = stats['coverage']
    # 加一只完全无 K 行的票进 codes：分母行数不变，覆盖率不变
    stats2 = qa_db.turnover_qa_stats('2026-09-11',
                                     codes=['sh.600000', 'sh.600001',
                                            'sh.600002', 'sh.600003',
                                            'sh.999999'])
    assert stats2['rows'] == 16 and stats2['coverage'] == before
    assert stats2['n_symbols'] == 5      # universe 口径保留
