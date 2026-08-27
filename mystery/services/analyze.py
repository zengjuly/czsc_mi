"""mystery.services.analyze — 唯一分析入口。

对外只暴露 analyze_one_stock() → AnalysisResult。
Web / CLI / scan / 板块钻取全部走这里，保证同股同分（误差 ≤ 1）。
一期 MYSTERY_CHAN_ENABLED=0：评分与 stock_analyzer 1.22.30 完全兼容。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

import pandas as pd

from ..adapters import market as _market
from ..adapters import sector as _sector
from ..core.indicators import enrich_indicators
from ..core.models import AnalysisResult, BarSeries, ChanStructure, MarketContext, MysteryBreakdown
from ..core.mystery_rules import MysteryLogic
from ..core.patterns import PatternRecognition
from ..core.platform import analyze_adaptive_platform
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

    # ---------------- 规则明细 ----------------
    def run_rules(self, daily: pd.DataFrame, weekly: Optional[pd.DataFrame],
                  monthly: Optional[pd.DataFrame], ctx: MarketContext,
                  chan_1d: Optional[ChanStructure],
                  include_detail: bool) -> MysteryBreakdown:
        """规则明细（与 stock_pipeline.analyze_one_stock 同构）。"""
        code = str(daily['代码'].iloc[-1]) if '代码' in daily.columns else ''
        market_data = None
        if ctx.index_bars is not None:
            market_data = {'上证指数': self.market.to_df(ctx.index_bars)}
        ind_trend = ctx.industry_up

        bd = MysteryBreakdown()
        bd.signal = self.logic.comprehensive_signal_analysis(
            daily, weekly_data=weekly, market_data=market_data,
            industry_data=None, industry_trend=ind_trend)
        bd.resonance = self.logic.three_resonance_analysis(
            daily, market_data=market_data, industry_trend=ind_trend,
            industry_data=None)
        bd.main_wave = self.logic.main_bull_wave_analysis(daily)
        bd.checklist8 = self.logic.main_bull_wave_checklist(daily, industry_trend=ind_trend)
        if include_detail:
            try:
                bd.platform = self.logic.platform_breakthrough_analysis(
                    daily, stock_code=code, weekly_data=weekly, monthly_data=monthly)
            except Exception as e:
                logger.debug(f"platform: {str(e)[:60]}")
            try:
                bd.vap_atr = analyze_adaptive_platform(daily, stock_code=code,
                                                       latest_only=True)
            except Exception as e:
                logger.debug(f"vap_atr: {str(e)[:60]}")
            try:
                bd.patterns = self.patterns.recognize_all_patterns(daily)
            except Exception as e:
                logger.debug(f"patterns: {str(e)[:60]}")
        else:
            bd.platform = {'平台状态': '未知', '突破信号': False, '买横信号': False,
                           '平台范围': None, '详情': []}
        return bd

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

        daily_df = enrich_indicators(self.market.to_df(daily))
        internal = daily.symbol
        daily_df['代码'] = internal
        weekly_df = self.market.to_df(weekly) if weekly.bars else None
        monthly_df = self.market.to_df(monthly) if monthly.bars else None

        ctx = self.build_market_context(internal, daily)

        # 缠论（P2：只展示不进评分；MYSTERY_CHAN_ENABLED=0 时 Service 不调用 Adapter）
        chan: Dict[str, ChanStructure] = {}
        if chan_enabled():
            chan = self._analyze_chan(daily)

        bd = self.run_rules(daily_df, weekly_df, monthly_df, ctx,
                            chan.get('1d'), include_detail)
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
