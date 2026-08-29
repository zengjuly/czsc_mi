"""mystery.core.pipeline — 纯计算流水线（零 IO，不 import adapters/czsc）。

把 AnalysisService 里的规则明细计算收口到 core：入参 BarSeries + MarketContext，
产出 MysteryBreakdown。Service 只负责组数据与上下文。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

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


# ---------------- W5 技术面明细快照（仅展示，不进评分） ----------------

def _f(v) -> Optional[float]:
    """数值转 float / None（numpy 标量安全）。"""
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, 3)
    except Exception:
        return None


def _arr_text(v) -> str:
    """均线排列 1/-1/0 → 文本。"""
    return {1: '多头排列', -1: '空头排列', 0: '混合整理'}.get(
        int(v) if v is not None and not pd.isna(v) else -9, '未知')


def _multi_period_trend(df: Optional[pd.DataFrame], kind: str) -> Dict[str, Any]:
    """周/月线趋势：多头排列/空头排列/震荡整理/数据不足（旧仓口径）。"""
    if df is None or df.empty:
        return {'趋势': '数据不足'}
    close = df['收盘价'].dropna()
    n = len(close)
    min_n = 20 if kind == 'weekly' else 10
    if n < min_n:
        return {'趋势': '数据不足'}
    latest = float(close.iloc[-1])
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())
    if kind == 'weekly':
        if ma5 > ma10 > ma20 and latest > ma20:
            trend = '多头排列'
        elif ma5 < ma10 < ma20:
            trend = '空头排列'
        else:
            trend = '震荡整理'
        return {'趋势': trend, '最新价': round(latest, 2),
                'MA5': round(ma5, 2), 'MA10': round(ma10, 2),
                'MA20': round(ma20, 2)}
    if ma5 > ma10 and latest > ma10:
        trend = '多头排列'
    elif ma5 < ma10:
        trend = '空头排列'
    else:
        trend = '震荡整理'
    return {'趋势': trend, '最新价': round(latest, 2),
            'MA5': round(ma5, 2), 'MA10': round(ma10, 2)}


def build_technical_snapshot(daily_df: pd.DataFrame,
                             weekly_df: Optional[pd.DataFrame],
                             monthly_df: Optional[pd.DataFrame],
                             logic: MysteryLogic,
                             platform: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """最新交易日技术面快照（纯计算，供 Web/报表展示，不改评分）。

    - ma: 均线排列（多头/空头/混合 + 强度 + MA5/10/20/60/250 + 斜率 + 信号）
    - po5: 破五反五（check_po5_fan5 完整判定）
    - chip: 筹码分析（集中度/数值/趋势，technical_detail_capture 口径）
    - volume_price: 量价分析（量比/量价配合度/OBV/成交量信号）
    - turnover: 换手率（当前/区域/MA5/10/20/相对位置）
    - multi_period: 多周期分析（周/月趋势 + 箱体 + 周线锚定）
    """
    tech: Dict[str, Any] = {'latest_date': '', 'ma': {}, 'po5': {},
                            'chip': {}, 'volume_price': {}, 'turnover': {},
                            'multi_period': {}}
    if daily_df is None or daily_df.empty:
        return tech
    last = daily_df.iloc[-1]
    tech['latest_date'] = str(last.get('日期', ''))[:10]

    # 均线排列
    tech['ma'] = {
        '排列状态': _arr_text(_f(last.get('均线排列'))),
        '多头排列强度': _f(last.get('多头排列强度')),
        'MA5': _f(last.get('MA5')), 'MA10': _f(last.get('MA10')),
        'MA20': _f(last.get('MA20')), 'MA60': _f(last.get('MA60')),
        'MA250': _f(last.get('MA250')),
        'MA5斜率': _f(last.get('MA5_斜率')),
        '均线信号': int(last['均线信号']) if pd.notna(last.get('均线信号')) else 0,
        '突破信号': int(last['突破信号']) if pd.notna(last.get('突破信号')) else 0,
    }

    # 破五反五（signal 级判定详情）
    tech['po5'] = logic.check_po5_fan5(daily_df)

    # 筹码分析
    tech['chip'] = logic.technical_detail_capture(daily_df)

    # 量价分析
    tech['volume_price'] = {
        '量比': _f(last.get('量比')),
        '量价配合度': int(last['量价配合度'])
        if pd.notna(last.get('量价配合度')) else 0,
        'OBV信号': _f(last.get('OBV信号')),
        '成交量信号': int(last['成交量信号'])
        if pd.notna(last.get('成交量信号')) else 0,
        '成交量突破信号': int(last['成交量突破信号'])
        if pd.notna(last.get('成交量突破信号')) else 0,
        '动能状态': str(last.get('动能状态', '未知')),
        '价格变化率5日': _f(last.get('价格变化率5日')),
        '价格变化率20日': _f(last.get('价格变化率20日')),
    }

    # 换手率
    tech['turnover'] = {
        '换手率': _f(last.get('换手率')),
        '换手率区域': str(last.get('换手率区域', '未知')),
        '换手率MA5': _f(last.get('换手率MA5')),
        '换手率MA10': _f(last.get('换手率MA10')),
        '换手率MA20': _f(last.get('换手率MA20')),
        '换手率相对位置': _f(last.get('换手率相对位置')),
    }

    # 多周期分析
    mp: Dict[str, Any] = {
        '周线': _multi_period_trend(weekly_df, 'weekly'),
        '月线': _multi_period_trend(monthly_df, 'monthly'),
    }
    if platform:
        if platform.get('周线箱体'):
            mp['周线箱体'] = platform['周线箱体']
        if platform.get('月线箱体'):
            mp['月线箱体'] = platform['月线箱体']
    try:
        mp['周线锚定'] = logic.weekly_anchor_check(weekly_df)
    except Exception:
        mp['周线锚定'] = {'锚定': False, '原因': '周线锚定分析异常'}
    tech['multi_period'] = mp
    return tech


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
        # W5 技术面明细快照（仅展示，不进评分）
        try:
            bd.technical = build_technical_snapshot(
                daily_df, weekly_df, monthly_df, logic,
                platform=bd.platform)
        except Exception as e:  # noqa: BLE001
            bd.technical = {'latest_date': '', 'detail': [f'分析异常: {e}']}
    else:
        bd.platform = {'平台状态': '未知', '突破信号': False, '买横信号': False,
                       '平台范围': None, '详情': []}
    return bd
