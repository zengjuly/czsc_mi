"""mystery.services.analyze — 唯一分析入口。

对外只暴露 analyze_one_stock() → AnalysisResult。
Web / CLI / scan / 板块钻取全部走这里，保证同股同分（误差 ≤ 1）。
一期 MYSTERY_CHAN_ENABLED=0：评分与 stock_analyzer 1.22.30 完全兼容。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from ..adapters import market as _market
from ..adapters import sector as _sector
from ..core.models import AnalysisResult, BarSeries, ChanStructure, MarketContext, MysteryBreakdown
from ..core.mystery_rules import MysteryLogic
from ..core.patterns import PatternRecognition
from ..core import scorer as _scorer

logger = logging.getLogger(__name__)

_INDEX_CODE = 'sh.000001'   # 上证指数


def chan_enabled() -> bool:
    """环境变量 MYSTERY_CHAN_ENABLED 覆盖 config（一期默认关）。"""
    v = os.environ.get('MYSTERY_CHAN_ENABLED')
    if v is not None:
        return v.strip().lower() not in ('0', 'false', 'off', '')
    return False


class AnalysisService:
    """分析服务（持有客户端与规则实例，线程安全可复用）。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        self.market = _market.MarketDataClient(self.cfg)
        self.sector = _sector.SectorClient(self.cfg)
        self.logic = MysteryLogic()
        self.patterns = PatternRecognition()

    # ---------------- 市场上下文 ----------------
    def build_market_context(self, symbol: str,
                             daily: BarSeries) -> MarketContext:
        """指数 + 行业（名称/强度分/趋势）+ 财务。"""
        ctx = MarketContext()
        try:
            index_series = self.market.fetch_index(_INDEX_CODE, '1d')
            if index_series.bars:
                ctx.index_bars = index_series
        except Exception as e:
            logger.debug(f"指数获取失败: {str(e)[:60]}")
        try:
            ind = self.sector.get_industry(symbol)
            ctx.industry_name = ind.get('name') or '未知'
            ctx.industry_score = ind.get('score')
            ctx.industry_up = ind.get('up')
        except Exception as e:
            logger.debug(f"行业获取失败: {str(e)[:60]}")
        try:
            ctx.financial = self.market.db.get_financial(symbol)
        except Exception as e:
            logger.debug(f"财务获取失败: {str(e)[:60]}")
        return ctx

    # ---------------- 规则明细（收口到 core.pipeline 纯计算） ----------------
    def run_rules(self, daily: BarSeries, weekly: Optional[BarSeries],
                  monthly: Optional[BarSeries], ctx: MarketContext,
                  include_detail: bool) -> MysteryBreakdown:
        from ..core import pipeline as _pipe

        return _pipe.run_mystery(daily, weekly, monthly, ctx,
                                 include_detail=include_detail, logic=self.logic,
                                 patterns=self.patterns)

    def _analyze_chan(self, daily: BarSeries) -> Dict[str, ChanStructure]:
        """缠论多周期分析（带 chan_cache：行情日/版本变化才失效）。"""
        from ..adapters.czsc_adapter import CzscAdapter, chan_from_dict, czsc_version
        import json as _json

        ver = czsc_version()
        trade_date = str(daily.bars[-1].dt)[:10] if daily.bars else ''
        adapter = CzscAdapter()
        out: Dict[str, ChanStructure] = {}
        try:
            cached_raw = self.market.db.get_chan_cache(daily.symbol, '1d',
                                                       trade_date, ver)
            if cached_raw:
                out['1d'] = chan_from_dict(_json.loads(cached_raw))
            else:
                s = adapter.analyze(daily)
                out['1d'] = s
                if s.engine_ver == "unavailable":
                    logger.error(
                        f"MYSTERY_CHAN_ENABLED=1 但 czsc 未安装："
                        f"pip install -e '.[chan]' 后重启（{daily.symbol}）")
                self.market.db.set_chan_cache(daily.symbol, '1d', trade_date, ver,
                                              _json.dumps(s.to_dict(), ensure_ascii=False))
            weekly = self.market.fetch_bars(daily.symbol, '1w')
            if weekly.bars:
                cw = adapter.analyze(weekly)
                out['1w'] = cw
        except Exception as e:
            logger.warning(f"缠论分析失败({daily.symbol}): {str(e)[:100]}")
        return out

    def analyze_one_stock(self, symbol: str,
                          include_detail: bool = True) -> AnalysisResult:
        """单票完整分析（CLAUDE.md §7.4 伪代码）。"""
        daily = self.market.fetch_bars(symbol, '1d')
        if not daily.bars:
            raise RuntimeError(f"[{symbol}] 无日K数据（本地库/在线源均失败）")
        weekly = self.market.fetch_bars(symbol, '1w')
        monthly = self.market.fetch_bars(symbol, '1M')

        internal = daily.symbol
        ctx = self.build_market_context(internal, daily)

        # 缠论（P2：只展示不进评分；MYSTERY_CHAN_ENABLED=0 时 Service 不调用 Adapter）
        chan: Dict[str, ChanStructure] = {}
        if chan_enabled():
            chan = self._analyze_chan(daily)

        bd = self.run_rules(daily, weekly, monthly, ctx, include_detail)
        score, advice, true_res = _scorer.combine(bd, chan,
                                                  chan_enabled=chan_enabled())
        last = daily.bars[-1]
        name = self.market.db.get_stock_name(internal) or ''
        czsc_ver = ''
        if chan:
            from ..adapters.czsc_adapter import czsc_version
            czsc_ver = czsc_version()
        result = AnalysisResult(
            symbol=internal,
            name=name,
            trade_date=str(last.dt)[:10],
            price=float(last.close),
            score=score,
            advice=advice,
            true_resonance=true_res,
            mystery=bd,
            chan=chan,
            sector={'行业名称': ctx.industry_name, '行业趋势分': ctx.industry_score,
                    '行业趋势': ctx.industry_up},
            financial=ctx.financial,
            rule_ver='mystery-1.22.30-compat',
            czsc_ver=czsc_ver,
        )
        return result


def analyze_one_stock(symbol: str, include_detail: bool = True,
                      cfg: Optional[Dict] = None) -> AnalysisResult:
    """唯一分析入口（模块级便捷函数）。"""
    return AnalysisService(cfg).analyze_one_stock(symbol, include_detail=include_detail)
