"""W36 审查修复回归（P1-1/2/3）：
1. bars_fingerprint 加重：中段/首根变化必须 miss（旧末根版不变）。
2. set_financial 列级合并：部分列 upsert 不洗 net_profit/eps_ttm/divid_cash。
3. 扫描链 fill_financial=False：build_market_context 绝不外呼 ths 财务。
4. F 位入缓存键：同票 F=0/F=1 缓存互不命中。
"""
from __future__ import annotations

import datetime as dt

import pytest

from mystery.core.models import Bar
from mystery.store.cache import bars_fingerprint, make_cache_key
from mystery.store.db import MysteryDB


def _bars(n=6, first_close=10.0, mid_close=None, last_close=15.0,
          first_vol=100.0, mid_vol=50.0, last_vol=200.0):
    bars = []
    for i in range(n):
        mid = n // 2
        if i == 0:
            c, v = first_close, first_vol
        elif i == n - 1:
            c, v = last_close, last_vol
        else:
            c, v = (mid_close if mid_close is not None else 12.0), mid_vol
        bars.append(Bar(dt=dt.datetime(2026, 1, 1) + dt.timedelta(days=i),
                        open=c, high=c + 1, low=c - 1, close=c, volume=v))
    return bars


# ---------- 1. 指纹加重（P1-3） ----------

def test_fingerprint_detects_middle_and_first_changes():
    fp = bars_fingerprint(_bars())
    assert bars_fingerprint(_bars(mid_close=99.0)) != fp     # 中段 close
    assert bars_fingerprint(_bars(mid_vol=999.0)) != fp      # 中段 volume（校验和）
    assert bars_fingerprint(_bars(n=7)) != fp                # 根数
    assert bars_fingerprint(_bars()) == fp                   # 稳定


def test_fingerprint_first_dt_participates():
    a = _bars()
    b = [x for x in _bars(n=7)][1:]  # 去首根 → 根数+首根 dt 都变
    assert bars_fingerprint(b) != bars_fingerprint(a)


# ---------- 2. set_financial 列级合并（P1-2） ----------

def test_set_financial_partial_columns_keep_others(tmp_path):
    db = MysteryDB(str(tmp_path / "t.db"))
    db.set_financial("600519", "2026-03-31", roe=10.5, eps_ttm=40.0,
                     divid_cash=2.0)
    # 模拟旧洗列路径：只传 PE/PB 的部分列 upsert
    db.set_financial("600519", "2026-03-31", pe=25.0, pb=8.0)
    fin = db.get_financial("600519")
    assert fin["roe"] == 10.5, "部分列 upsert 不得洗掉 roe（旧 REPLACE 会）"
    assert fin.get("eps_ttm") == 40.0
    assert fin.get("divid_cash") == 2.0
    assert fin["PE"] == 25.0 and fin["PB"] == 8.0, "新值必须覆盖"


# ---------- 3+4. 扫描链禁外呼 + F 位键（P1-1） ----------

class _CountThs:
    def __init__(self):
        self.calls = 0

    def get_financial(self, symbol):
        self.calls += 1
        return {}

    def get_indicators(self, symbol):
        self.calls += 1
        return {}


class _FakeDB:
    def get_financial(self, code):
        return {}          # 库内缺 ROE → unknown，不外呼


class _FakeMarket:
    def __init__(self):
        self.ths = _CountThs()
    db = _FakeDB()

    def fetch_index(self, *a, **k):
        raise RuntimeError("offline")


@pytest.fixture()
def _svc():
    from mystery.services.analyze import AnalysisService
    svc = AnalysisService.__new__(AnalysisService)
    svc.market = _FakeMarket()

    class _S:
        def get_industry(self, code):
            return {"name": "未知", "score": None, "up": None}
    svc.sector = _S()
    return svc


def test_fill_financial_false_skips_online(_svc):
    ctx = _svc.build_market_context("600519.SH", fill_financial=False)
    assert ctx.financial in (None, {})       # 缺 → 保持 unknown
    assert _svc.market.ths.calls == 0        # 且绝不外呼


def test_fill_financial_true_tries_online(_svc):
    _svc.build_market_context("600519.SH", fill_financial=True)
    assert _svc.market.ths.calls == 2        # 兜底路径仍外呼（get_financial+get_indicators）


def test_cache_key_differs_by_fill_flag():
    fp = bars_fingerprint(_bars())
    k0 = make_cache_key("qfq", "mystery-1.22.30-compat", "D1|C0|S0|F0", "", fp)
    k1 = make_cache_key("qfq", "mystery-1.22.30-compat", "D1|C0|S0|F1", "", fp)
    assert k0 != k1
