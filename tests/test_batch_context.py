"""W63 验证：扫描上下文批量预取行业 + 财务，语义与逐票查库完全一致。

- get_primary_industries / get_financials 批量方法（IN 查询）正确性
- build_market_context 传 ctx_cache 的结果 == 不传（逐票查库）的结果
"""
import types

from mystery.services.analyze import AnalysisService, MarketContextCache
from mystery.store.db import MysteryDB


def _seed(db):
    c = db._connect()
    c.execute("INSERT INTO sector_meta (sector_code, sector_name) VALUES ('ths_1','银行')")
    c.execute("INSERT INTO sector_meta (sector_code, sector_name) VALUES ('ths_2','白酒')")
    c.execute("INSERT INTO stock_sector_rel (stock_code, sector_code, is_primary) "
              "VALUES ('sh.600000','ths_1',1)")
    c.execute("INSERT INTO stock_sector_rel (stock_code, sector_code, is_primary) "
              "VALUES ('sh.600519','ths_2',1)")
    # 同一票多报告期：批量方法应取 report_date 最新一条
    c.execute("INSERT INTO stock_financial_data (code, report_date, roe) "
              "VALUES ('sh.600000','2024-Q3',10.0)")
    c.execute("INSERT INTO stock_financial_data (code, report_date, roe) "
              "VALUES ('sh.600519','2024-Q2',20.0)")
    c.execute("INSERT INTO stock_financial_data (code, report_date, roe) "
              "VALUES ('sh.600519','2024-Q3',25.0)")
    c.commit()
    c.close()


def test_batch_industries_and_financials(tmp_path):
    db = MysteryDB(db_path=str(tmp_path / "b.db"))
    _seed(db)
    ind = db.get_primary_industries(['sh.600000', 'sh.600519', 'sh.000001'])
    assert ind == {'sh.600000': ('ths_1', '银行'),
                   'sh.600519': ('ths_2', '白酒')}
    fin = db.get_financials(['sh.600000', 'sh.600519', 'sh.000001'])
    assert fin['sh.600000']['roe'] == 10.0
    assert fin['sh.600519']['roe'] == 25.0  # 取最新报告期
    assert 'sh.000001' not in fin
    # 空列表防御
    assert db.get_primary_industries([]) == {}
    assert db.get_financials([]) == {}


def test_build_market_context_batched_matches_percall(tmp_path, monkeypatch):
    svc = AnalysisService(cfg={'db_path': str(tmp_path / "ctx.db")})
    _seed(svc.market.db)
    # 避免联网拉指数；返回一个 bars 为空的占位对象
    monkeypatch.setattr(
        svc.market, 'fetch_index',
        lambda *a, **k: types.SimpleNamespace(bars=[]))
    ind = svc.market.db.get_primary_industries(['sh.600000', 'sh.600519'])
    fin = svc.market.db.get_financials(['sh.600000', 'sh.600519'])
    cache = MarketContextCache(industries=ind, financials=fin)
    batched = svc.build_market_context('sh.600519', fill_financial=False,
                                       ctx_cache=cache)
    percall = svc.build_market_context('sh.600519', fill_financial=False)
    # 行业名来自批量预取 / 逐票查库应一致
    assert batched.industry_name == percall.industry_name == '白酒'
    # 财务来自批量预取 / 逐票查库应一致（均取最新报告期 roe=25）
    assert batched.financial == percall.financial
    assert percall.financial['roe'] == 25.0
