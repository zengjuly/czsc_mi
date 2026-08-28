"""mystery.core.pipeline — 纯计算流水线（零 IO，不 import adapters/czsc）。

把 AnalysisService 里的规则明细计算收口到 core：入参 BarSeries + MarketContext，
产出 MysteryBreakdown。Service 只负责组数据与上下文。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .indicators import enrich_indicators
from .models import BarSeries, MarketContext, MysteryBreakdown
from .mystery_rules import MysteryLogic
from .patterns import PatternRecognition
from .platform import analyze_adaptive_platform


def series_to_df(series: Optional[BarSeries]) -> Optional[pd.DataFrame]:
    """BarSeries → DataFrame（中文列，纯函数，无 IO）。"""
    if series is None or not series.bars:
        return None
    rows = [{'日期': str(b.dt)[:10], '开盘价': b.open, '最高价': b.high,
             '最低价': b.low, '收盘价': b.close, '成交量': b.volume,
             '成交额': b.amount, '换手率': b.turnover, '涨跌幅': b.pct_chg}
            for b in series.bars]
    return pd.DataFrame(rows)


def run_mystery(daily: BarSeries,
                weekly: Optional[BarSeries] = None,
                monthly: Optional[BarSeries] = None,
                ctx: Optional[MarketContext] = None,
                include_detail: bool = True,
                logic: Optional[MysteryLogic] = None,
                patterns: Optional[PatternRecognition] = None) -> MysteryBreakdown:
    """规则明细纯计算（与旧 stock_pipeline.analyze_one_stock 同构）。"""
    logic = logic or MysteryLogic()
    patterns = patterns or PatternRecognition()
    daily_df = enrich_indicators(series_to_df(daily))
    daily_df['代码'] = daily.symbol
    weekly_df = series_to_df(weekly)
    monthly_df = series_to_df(monthly)

    market_data = None
    ind_trend = None
    if ctx is not None:
        if ctx.index_bars is not None and ctx.index_bars.bars:
            market_data = {'上证指数': series_to_df(ctx.index_bars)}
        ind_trend = ctx.industry_up

    bd = MysteryBreakdown()
    bd.signal = logic.comprehensive_signal_analysis(
        daily_df, weekly_data=weekly_df, market_data=market_data,
        industry_data=None, industry_trend=ind_trend)
    bd.resonance = logic.three_resonance_analysis(
        daily_df, market_data=market_data, industry_trend=ind_trend,
        industry_data=None)
    bd.main_wave = logic.main_bull_wave_analysis(daily_df)
    bd.checklist8 = logic.main_bull_wave_checklist(daily_df, industry_trend=ind_trend)
    if include_detail:
        try:
            bd.platform = logic.platform_breakthrough_analysis(
                daily_df, stock_code=daily.symbol,
                weekly_data=weekly_df, monthly_data=monthly_df)
        except Exception as e:  # noqa: BLE001
            bd.platform = {'平台状态': '异常', '详情': [f'分析异常: {e}']}
        try:
            bd.vap_atr = analyze_adaptive_platform(daily_df, stock_code=daily.symbol,
                                                   latest_only=True)
        except Exception as e:  # noqa: BLE001
            bd.vap_atr = {'详情': [f'分析异常: {e}']}
        try:
            bd.patterns = patterns.recognize_all_patterns(daily_df)
        except Exception as e:  # noqa: BLE001
            bd.patterns = {'主要形态': '异常', '详情': [f'识别异常: {e}']}
    else:
        bd.platform = {'平台状态': '未知', '突破信号': False, '买横信号': False,
                       '平台范围': None, '详情': []}
    return bd
