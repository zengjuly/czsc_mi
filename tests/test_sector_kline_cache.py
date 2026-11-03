"""W优化⑦：板块指数 K 线记忆化——同行业被多只个股共享，只回源一次。

覆盖：同一行业多只个股只调用一次 db.get_sector_kline；不同行业各自回源一次；
ths_ 前缀归一后共享同一缓存；get_industry / get_sector 均走缓存。
"""
from __future__ import annotations

from types import SimpleNamespace

from mystery.adapters.sector import SectorClient


class _StubDB:
    def __init__(self, sector_of):
        self._sector_of = sector_of   # stock_code -> (sector_code, sector_name)
        self.kline_calls = []

    def get_primary_industry(self, stock_code):
        return self._sector_of.get(stock_code)

    def get_sector_kline(self, sector_code):
        self.kline_calls.append(sector_code)
        return None   # 跳过评分计算，聚焦回源次数


def _client(sector_of):
    c = SectorClient(cfg={})
    c.db = _StubDB(sector_of)
    return c


def test_sector_kline_cached_per_industry():
    # 600519 / 600999 同属白酒 881318；600036 属银行 881101
    sector_of = {
        'sh.600519': ('881318', '白酒'),
        'sh.600999': ('881318', '白酒'),
        'sh.600036': ('881101', '银行'),
    }
    c = _client(sector_of)
    assert c.get_industry('sh.600519')['code'] == '881318'
    # 同行业第二只个股：不应再回源
    assert c.get_industry('sh.600999')['code'] == '881318'
    # 不同行业：回源一次
    assert c.get_industry('sh.600036')['code'] == '881101'
    # 881318 只回源一次、881101 回源一次
    assert c.db.kline_calls == ['881318', '881101'], c.db.kline_calls
    # 重复查询已分析个股仍命中缓存
    c.get_industry('sh.600519')
    assert c.db.kline_calls == ['881318', '881101'], c.db.kline_calls


def test_sector_kline_normalization_shared():
    c = _client({})
    # ths_881318 与 881318 归一为同一 key，只回源一次
    assert c._sector_kline('ths_881318') is None
    assert c._sector_kline('881318') is None
    assert c.db.kline_calls == ['ths_881318'], c.db.kline_calls
    # get_sector 走同一缓存，无新增回源
    c.get_sector('881318')
    assert c.db.kline_calls == ['ths_881318'], c.db.kline_calls
